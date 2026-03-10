"""
Bitunix position monitor for GMXBot.

Replicates the intl-trading-bot's TP/SL tracking flow:
  - Polls Bitunix pending TP/SL orders to detect TP hits
  - Verifies triggered orders via history API
  - Moves SL after TP hits (trailing SL logic)
  - Reconciles positions with exchange (detect closes/liquidations)

Designed as a mixin that coexists with the GMX SLTPMixin.
"""

import time
import asyncio
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger("GMXBot.bitunix_monitor")


class BitunixMonitorMixin:
    """Mixin providing Bitunix-specific TP/SL monitoring and position reconciliation.

    Expected attributes on the host class (GMXBot):
        bitunix_client: BitunixClient
        positions: Dict[str, Position]
        cfg: Config
        exchange_mode: str
        logger: Logger
        notify(): async method
    """

    BX_TP_TRACKING_FILE = "json/bx_tp_tracking.json"

    def _init_bitunix_monitor(self):
        """Call from GMXBot.__init__ to initialize Bitunix monitor state."""
        # TP order tracking: keyed by bitunix_position_id (stable across restarts)
        # Format: {bitunix_position_id -> [{orderId, price, pct, hit}, ...]}
        self._bx_tp_tracking: Dict[str, List[Dict[str, Any]]] = self._load_bx_tp_tracking()
        # Missing position counter for reconciliation
        self._bx_missing_count: Dict[str, int] = {}
        # Reconciliation cycle counter
        self._bx_reconcile_counter: int = 0
        # Flag: startup catch-up done
        self._bx_startup_done: bool = False

    def _save_bx_tp_tracking(self):
        """Persist Bitunix TP tracking to disk so it survives restart."""
        from state_io import atomic_json_write
        try:
            atomic_json_write(self.BX_TP_TRACKING_FILE, self._bx_tp_tracking)
        except Exception as e:
            logger.warning(f"Failed to save bx_tp_tracking: {e}")

    def _load_bx_tp_tracking(self) -> Dict[str, List[Dict[str, Any]]]:
        """Load Bitunix TP tracking from disk."""
        from state_io import safe_json_read
        return safe_json_read(self.BX_TP_TRACKING_FILE, default={})

    def _get_tp_tracking(self, pos) -> list:
        """Look up TP tracking — try bitunix_position_id first, then UUID."""
        bpid = getattr(pos, 'bitunix_position_id', None)
        if bpid and bpid in self._bx_tp_tracking:
            return self._bx_tp_tracking[bpid]
        return self._bx_tp_tracking.get(pos.id, [])

    def _pop_tp_tracking(self, pos):
        """Remove TP tracking for a position."""
        bpid = getattr(pos, 'bitunix_position_id', None)
        if bpid and bpid in self._bx_tp_tracking:
            return self._bx_tp_tracking.pop(bpid, None)
        return self._bx_tp_tracking.pop(pos.id, None)

    def _migrate_tp_tracking_keys(self):
        """One-time migration: re-key any UUID-keyed entries to bitunix_position_id."""
        migrated = 0
        for pos in self.positions.values():
            if not pos.is_open or getattr(pos, 'exchange', 'gmx') != 'bitunix':
                continue
            bpid = getattr(pos, 'bitunix_position_id', None)
            if not bpid:
                continue
            # If tracking exists under UUID but not under bitunix_position_id
            if pos.id in self._bx_tp_tracking and bpid not in self._bx_tp_tracking:
                self._bx_tp_tracking[bpid] = self._bx_tp_tracking.pop(pos.id)
                migrated += 1
        if migrated:
            self._save_bx_tp_tracking()
            logger.info(f"Migrated {migrated} TP tracking key(s) from UUID to bitunix_position_id")

    # ──────────────────────────────────────────────────────────────────────
    # TP Order Registration
    # ──────────────────────────────────────────────────────────────────────

    def register_bitunix_tp_orders(self, pos_id: str, tp_results: list, bitunix_position_id: str = None):
        """Register TP order IDs for tracking after position open.

        tp_results: [{orderId, price, pct}, ...]
        Uses bitunix_position_id as key (stable across restarts), falls back to UUID.
        """
        tracked = []
        for tp in tp_results:
            oid = tp.get("orderId")
            if oid and oid != "dry-run":
                tracked.append({
                    "orderId": oid,
                    "price": tp.get("price", 0),
                    "pct": tp.get("pct", 0),
                    "hit": False,
                })
        if tracked:
            key = bitunix_position_id or pos_id
            self._bx_tp_tracking[key] = tracked
            self._save_bx_tp_tracking()
            logger.info(f"Registered {len(tracked)} TP orders for {key}")

    # ──────────────────────────────────────────────────────────────────────
    # Main Monitor Loop
    # ──────────────────────────────────────────────────────────────────────

    async def bitunix_monitor_loop(self):
        """Background loop: check Bitunix TP hits and reconcile positions every 5s."""
        while True:
            try:
                if not self.bitunix_client:
                    await asyncio.sleep(30)
                    continue

                # Only monitor if we have Bitunix positions
                bx_positions = [
                    p for p in self.positions.values()
                    if p.is_open and p.exchange == "bitunix"
                ]
                if not bx_positions:
                    await asyncio.sleep(10)
                    continue

                # Startup catch-up: migrate keys, re-discover TPs, detect missed hits
                if not self._bx_startup_done:
                    self._migrate_tp_tracking_keys()
                    await self._startup_bitunix_catchup(bx_positions)
                    self._bx_startup_done = True

                # Update prices and PnL for Bitunix positions
                await self._update_bitunix_prices(bx_positions)

                # Check TP order state
                await self._check_bitunix_tp_state(bx_positions)

                # Reconcile every ~60s (12 cycles of 5s)
                self._bx_reconcile_counter += 1
                if self._bx_reconcile_counter >= 12:
                    self._bx_reconcile_counter = 0
                    await self._reconcile_bitunix_positions(bx_positions)

                await asyncio.sleep(5)
            except asyncio.CancelledError:
                logger.info("Bitunix monitor loop cancelled")
                return
            except Exception as e:
                logger.error(f"Bitunix monitor error: {e}")
                await asyncio.sleep(10)

    # ──────────────────────────────────────────────────────────────────────
    # Price Updates
    # ──────────────────────────────────────────────────────────────────────

    async def _update_bitunix_prices(self, bx_positions: list):
        """Fetch current prices from Bitunix and update position PnL."""
        from bitunix_executor import to_bitunix_symbol

        for pos in bx_positions:
            try:
                bitunix_sym = to_bitunix_symbol(pos.symbol)
                price = await asyncio.to_thread(
                    self.bitunix_client.get_current_price, bitunix_sym
                )
                if price > 0:
                    pos.current_price = price
                    # Calculate unrealized PnL on REMAINING size (after partial TP fills)
                    total_decreased = sum(
                        d.get("size_delta_usd", 0) for d in pos.verified_decreases
                    )
                    remaining_size = max(pos.size_usd - total_decreased, 0)
                    if pos.entry_price > 0 and remaining_size > 0:
                        if pos.side == "LONG":
                            pnl = (price - pos.entry_price) / pos.entry_price * remaining_size
                        else:
                            pnl = (pos.entry_price - price) / pos.entry_price * remaining_size
                        pos.unrealized_pnl = pnl
            except Exception as e:
                logger.debug(f"Price update failed for {pos.symbol}: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # TP Hit Detection (mirrors intl-trading-bot/sl_tp.py)
    # ──────────────────────────────────────────────────────────────────────

    async def _check_bitunix_tp_state(self, bx_positions: list):
        """Poll Bitunix TP/SL orders to detect triggered TPs."""
        from bitunix_executor import to_bitunix_symbol
        from risk import determine_new_sl_target

        for pos in bx_positions:
            tracked = self._get_tp_tracking(pos)
            if not tracked:
                continue

            unfilled = [t for t in tracked if not t["hit"]]
            if not unfilled:
                continue

            bitunix_sym = to_bitunix_symbol(pos.symbol)

            try:
                # Fetch pending TP/SL orders from exchange
                pending = await asyncio.to_thread(
                    self.bitunix_client.get_pending_tpsl_orders, bitunix_sym
                )
                pending_ids = set()
                for o in pending:
                    oid = o.get("id") or o.get("orderId")
                    if oid:
                        pending_ids.add(oid)

                new_hits = []
                for tp_info in unfilled:
                    oid = tp_info["orderId"]
                    if oid not in pending_ids:
                        # Order gone from pending — check if triggered
                        was_triggered = await self._verify_bitunix_tp_triggered(
                            bitunix_sym, oid
                        )
                        if was_triggered:
                            tp_info["hit"] = True
                            new_hits.append(tp_info)
                            logger.info(
                                f"TP HIT: {pos.symbol} {pos.side} "
                                f"TP @ ${tp_info['price']:,.2f}"
                            )

                if new_hits:
                    self._save_bx_tp_tracking()  # Persist hit status to disk
                    total_hits = sum(1 for t in tracked if t["hit"])
                    # Normalize to same format as GMX verified_decreases so
                    # downstream code (PnL calc, TP display) works correctly
                    pos.verified_decreases = []
                    for t in tracked:
                        if not t["hit"]:
                            continue
                        tp_size = pos.original_size_usd * t.get("pct", 0)
                        if pos.entry_price and pos.entry_price > 0:
                            if pos.side == "LONG":
                                tp_pnl = (t["price"] - pos.entry_price) / pos.entry_price * tp_size
                            else:
                                tp_pnl = (pos.entry_price - t["price"]) / pos.entry_price * tp_size
                        else:
                            tp_pnl = 0
                        pos.verified_decreases.append({
                            "execution_price": t["price"],
                            "matched_tp_price": t["price"],
                            "size_delta_usd": tp_size,
                            "pnl_usd": tp_pnl,
                            "net_pnl_usd": tp_pnl,
                            "order_type": 5,  # TP (matches GMX convention)
                            "timestamp": time.time(),
                            "source": "bitunix",
                        })

                    # Calculate PnL for notification
                    realized_pnl = sum(d.get("net_pnl_usd", 0) for d in pos.verified_decreases)
                    total_decreased = sum(d.get("size_delta_usd", 0) for d in pos.verified_decreases)
                    remaining_size = max(pos.size_usd - total_decreased, 0)
                    unrealized_pnl = 0.0
                    if pos.entry_price > 0 and remaining_size > 0 and pos.current_price:
                        if pos.side == "LONG":
                            unrealized_pnl = (pos.current_price - pos.entry_price) / pos.entry_price * remaining_size
                        else:
                            unrealized_pnl = (pos.entry_price - pos.current_price) / pos.entry_price * remaining_size
                    total_pnl = realized_pnl + unrealized_pnl
                    collateral = pos.size_usd / pos.leverage if pos.leverage else pos.size_usd
                    pnl_pct_str = f" ({total_pnl / collateral * 100:+.1f}%)" if collateral > 0 else ""
                    r_sign = "+" if realized_pnl >= 0 else "-"
                    u_sign = "+" if unrealized_pnl >= 0 else "-"
                    t_sign = "+" if total_pnl >= 0 else "-"

                    # Notify each TP hit
                    for tp_info in new_hits:
                        await self.notify(
                            f"BITUNIX {pos.symbol} {pos.side} {pos.leverage:.1f}x: Target {total_hits} Hit ✅\n"
                            f"Realized: {r_sign}${abs(realized_pnl):,.2f} @ ${tp_info['price']:,.2f}\n"
                            f"Unrealized: {u_sign}${abs(unrealized_pnl):,.2f}\n"
                            f"Total PnL: {t_sign}${abs(total_pnl):,.2f}{pnl_pct_str}"
                        )

                    # Persist position state with updated verified_decreases
                    self._save_position_state()

                    # Move SL after TP hit — but only if current SL is wrong
                    sorted_tp_prices = sorted(t["price"] for t in tracked)
                    new_sl, sl_label = determine_new_sl_target(
                        total_hits, pos.entry_price, sorted_tp_prices, pos.leverage
                    )
                    if new_sl is not None and new_sl != pos.stop_loss:
                        should_move = True
                        if pos.stop_loss is not None:
                            tolerance = pos.entry_price * 0.003 if pos.entry_price else 1.0
                            sl_already_better = (
                                (pos.side == "LONG" and pos.stop_loss > new_sl + tolerance)
                                or (pos.side == "SHORT" and pos.stop_loss < new_sl - tolerance)
                            )
                            if sl_already_better:
                                logger.info(
                                    f"{pos.symbol} SL already at better level "
                                    f"(${pos.stop_loss:,.2f}) than trailing target "
                                    f"(${new_sl:,.2f} {sl_label}) — keeping current"
                                )
                                should_move = False
                        if should_move:
                            await self._move_bitunix_sl(pos, new_sl, sl_label)

            except Exception as e:
                logger.debug(f"TP check failed for {pos.symbol}: {e}")

    async def _verify_bitunix_tp_triggered(self, symbol: str, order_id: str) -> bool:
        """Check TP/SL history to verify an order was actually triggered (not cancelled)."""
        try:
            history = await asyncio.to_thread(
                self.bitunix_client.get_history_tpsl_orders, symbol, 200
            )
            for o in history:
                oid = o.get("id") or o.get("orderId")
                if oid == order_id:
                    status = (o.get("status") or "").upper()
                    if status == "SYSTEM_CANCELED":
                        return False  # Position closed, TP didn't trigger
                    return True  # TRIGGERED or FILLED
            return False  # Not in history yet — wait
        except Exception as e:
            logger.debug(f"TP history check failed: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────
    # SL Movement
    # ──────────────────────────────────────────────────────────────────────

    async def _move_bitunix_sl(self, pos, new_sl: float, label: str = ""):
        """Move stop loss on a Bitunix position."""
        from bitunix_executor import to_bitunix_symbol

        bitunix_sym = to_bitunix_symbol(pos.symbol)
        position_id = pos.bitunix_position_id

        if not position_id:
            # Try to find position on exchange
            position_id = await self._find_bitunix_position_id(pos)
            if not position_id:
                logger.warning(f"Cannot move SL: {pos.symbol} position not found on Bitunix")
                pos.sl_move_failed = True
                return

        try:
            try:
                await asyncio.to_thread(
                    self.bitunix_client.modify_position_tpsl,
                    symbol=bitunix_sym,
                    position_id=position_id,
                    sl_price=str(new_sl),
                )
            except RuntimeError:
                # modify may fail if no position-level SL exists yet — fall back to place
                logger.info(f"SL modify failed for {pos.symbol}, trying place instead")
                await asyncio.to_thread(
                    self.bitunix_client.place_position_tpsl,
                    symbol=bitunix_sym,
                    position_id=position_id,
                    sl_price=str(new_sl),
                )
            old_sl = pos.stop_loss or 0
            pos.stop_loss = new_sl
            pos.sl_moved_to_entry = (label == "Entry")
            pos.sl_move_label = label
            pos.sl_move_failed = False
            self._save_position_state()

            await self.notify(
                f"SL Moved BITUNIX {pos.symbol} {pos.side} {pos.leverage:.1f}x\n"
                f"${old_sl:,.2f} -> ${new_sl:,.2f} ({label}) ✅"
            )
            logger.info(f"SL moved for {pos.symbol}: ${old_sl:,.2f} -> ${new_sl:,.2f} ({label})")
        except Exception as e:
            logger.error(f"Failed to move SL for {pos.symbol}: {e}")
            pos.sl_move_failed = True
            self._save_position_state()
            await self.notify(
                f"⚠️ SL Move FAILED BITUNIX {pos.symbol} {pos.side} {pos.leverage:.1f}x\n"
                f"Tried: ${new_sl:,.2f} ({label})\nError: {e}"
            )

    async def _find_bitunix_position_id(self, pos) -> Optional[str]:
        """Find a Bitunix position ID by matching symbol and side."""
        from bitunix_executor import to_bitunix_symbol

        bitunix_sym = to_bitunix_symbol(pos.symbol)
        expected_side = "LONG" if pos.side == "LONG" else "SHORT"

        try:
            positions = await asyncio.to_thread(
                self.bitunix_client.get_pending_positions, bitunix_sym
            )
            for p in positions:
                raw_side = (p.get("side") or "").upper()
                pos_side = "LONG" if raw_side in ("BUY", "LONG") else "SHORT"
                if pos_side == expected_side:
                    pid = p.get("positionId")
                    pos.bitunix_position_id = pid
                    return pid
        except Exception as e:
            logger.debug(f"Position lookup failed for {pos.symbol}: {e}")
        return None

    # ──────────────────────────────────────────────────────────────────────
    # Position Reconciliation
    # ──────────────────────────────────────────────────────────────────────

    async def _reconcile_bitunix_positions(self, bx_positions: list):
        """Check if Bitunix positions still exist on exchange. Close if gone for 3 checks."""
        if not self.bitunix_client:
            return

        try:
            exchange_positions = await asyncio.to_thread(
                self.bitunix_client.get_pending_positions
            )
        except Exception as e:
            logger.debug(f"Bitunix reconciliation fetch failed: {e}")
            return

        # Build set of (symbol, side) and positionIds that exist on exchange
        exchange_set = set()
        exchange_position_ids = set()
        for ep in exchange_positions:
            sym = (ep.get("symbol") or "").replace("USDT", "")
            # Handle special names like 1000PEPE
            if sym.startswith("1000"):
                sym = sym[4:]
            raw_side = (ep.get("side") or "").upper()
            side = "LONG" if raw_side in ("BUY", "LONG") else "SHORT"
            exchange_set.add((sym, side))
            pid = ep.get("positionId")
            if pid:
                exchange_position_ids.add(pid)

        for pos in bx_positions:
            # Primary match: by positionId (most reliable, immune to side corruption)
            if pos.bitunix_position_id and pos.bitunix_position_id in exchange_position_ids:
                self._bx_missing_count.pop(pos.id, None)
                continue
            # Fallback: by (symbol, clean_side)
            clean_side = pos.side.split(":")[-1].upper() if ":" in pos.side else pos.side
            key = (pos.symbol, clean_side)
            if key in exchange_set:
                # Position exists — reset missing counter
                self._bx_missing_count.pop(pos.id, None)
            else:
                # Position not found — increment missing counter
                count = self._bx_missing_count.get(pos.id, 0) + 1
                self._bx_missing_count[pos.id] = count
                logger.debug(f"{pos.symbol} {pos.side} missing from Bitunix ({count}/3)")

                if count >= 3:
                    # Position gone for 3 consecutive checks — mark closed
                    logger.info(f"Bitunix position gone: {pos.symbol} {pos.side}")
                    pos.is_open = False
                    pos.closed_at = time.time()
                    pos.exit_reason = "exchange_closed"
                    self._bx_missing_count.pop(pos.id, None)

                    # Rebuild verified_decreases from tracking data BEFORE removing it
                    # (ensures unfilled target detection works even after bot restart)
                    tracked = self._get_tp_tracking(pos)
                    tp_hits = sum(1 for t in tracked if t.get("hit"))
                    if tracked and not getattr(pos, 'verified_decreases', None):
                        pos.verified_decreases = []
                        for t in tracked:
                            if not t["hit"]:
                                continue
                            tp_size = pos.original_size_usd * t.get("pct", 0)
                            if pos.entry_price and pos.entry_price > 0:
                                if pos.side == "LONG":
                                    tp_pnl = (t["price"] - pos.entry_price) / pos.entry_price * tp_size
                                else:
                                    tp_pnl = (pos.entry_price - t["price"]) / pos.entry_price * tp_size
                            else:
                                tp_pnl = 0
                            pos.verified_decreases.append({
                                "execution_price": t["price"],
                                "matched_tp_price": t["price"],
                                "size_delta_usd": tp_size,
                                "pnl_usd": tp_pnl,
                                "net_pnl_usd": tp_pnl,
                                "order_type": 5,
                                "timestamp": time.time(),
                                "source": "bitunix",
                            })
                    self._pop_tp_tracking(pos)
                    self._save_bx_tp_tracking()
                    if tp_hits > 0:
                        pos.exit_reason = f"tp_hit_x{tp_hits}"
                    elif pos.sl_moved_to_entry:
                        pos.exit_reason = "sl_triggered"
                    else:
                        pos.exit_reason = "sl_or_liquidation"

                    # Record trade
                    await self._record_trade(pos, pos.exit_reason)
                    self._save_position_state()

                    total_pnl = pos.unrealized_pnl or 0.0
                    collateral = pos.size_usd / pos.leverage if pos.leverage else pos.size_usd
                    pnl_pct = (total_pnl / collateral * 100) if collateral > 0 else 0.0
                    pnl_sign = "+" if total_pnl >= 0 else "-"
                    if tp_hits > 0:
                        close_label = f"Position Closed (TP x{tp_hits}) BITUNIX"
                    elif pos.exit_reason == "sl_triggered":
                        close_label = "Position Closed (SL) BITUNIX"
                    else:
                        close_label = "Position Closed BITUNIX"
                    await self.notify(
                        f"{close_label}\n\n"
                        f"{pos.symbol} {pos.side} {pos.leverage:.1f}x\n"
                        f"Entry: ${pos.entry_price:,.2f}\n"
                        f"Exit: ${pos.current_price:,.2f}\n"
                        f"PnL: {pnl_sign}${abs(total_pnl):,.2f} ({pnl_pct:+.1f}%)\n"
                        f"Duration: {pos.duration_hours:.1f}h"
                    )

                    # Mirror mode: auto-close the paired GMX position
                    if getattr(self, 'exchange_mode', '') == 'mirror':
                        try:
                            gmx_pos_obj = next(
                                (p for p in self.positions.values()
                                 if p.is_open
                                 and getattr(p, 'exchange', 'gmx') == 'gmx'
                                 and p.symbol == pos.symbol
                                 and p.side == pos.side),
                                None,
                            )
                            if gmx_pos_obj and gmx_pos_obj.market_addr:
                                logger.info(
                                    f"[MIRROR] Bitunix {pos.symbol} closed — "
                                    f"auto-closing GMX mirror"
                                )
                                acct = self._get_account(gmx_pos_obj.wallet_id)
                                # Fetch on-chain position for execute_close
                                from open import fetch_open_positions
                                on_chain = await asyncio.to_thread(
                                    fetch_open_positions,
                                    self.w3, acct.address,
                                )
                                gmx_match = next(
                                    (p for p in on_chain
                                     if p.market.lower() == gmx_pos_obj.market_addr.lower()
                                     and p.is_long == (gmx_pos_obj.side == "LONG")),
                                    None,
                                )
                                if gmx_match:
                                    tx = await self.execute_close(gmx_match, 1.0, acct=acct)
                                    if tx:
                                        gmx_pos_obj.is_open = False
                                        gmx_pos_obj.closed_at = time.time()
                                        gmx_pos_obj.exit_reason = f"mirror_close:{pos.exit_reason}"
                                        await self._record_trade(gmx_pos_obj, exit_reason=gmx_pos_obj.exit_reason)
                                        self._clear_position_state(gmx_pos_obj)
                                        await self.notify(
                                            f"[MIRROR] GMX {pos.symbol} {pos.side} auto-closed "
                                            f"(BITUNIX {pos.exit_reason})\nTX: {tx}"
                                        )
                                    else:
                                        await self.notify(
                                            f"⚠️ [MIRROR] Failed to auto-close GMX {pos.symbol}. "
                                            f"Use /close {pos.symbol} manually."
                                        )
                                else:
                                    # GMX position already closed on-chain
                                    logger.info(f"[MIRROR] GMX {pos.symbol} already closed on-chain")
                        except Exception as me:
                            logger.warning(f"[MIRROR] Failed to auto-close GMX: {me}")

    # ──────────────────────────────────────────────────────────────────────
    # Startup Catch-up (runs once after restart)
    # ──────────────────────────────────────────────────────────────────────

    async def _startup_bitunix_catchup(self, bx_positions: list):
        """After restart: re-discover TP orders, detect missed hits, correct SL.

        For each Bitunix position:
          1. If no TP tracking exists: re-discover pending TP orders from exchange
          2. If TP tracking exists: check history for TPs that fired while offline
          3. Correct SL level based on detected TP hits
        """
        from bitunix_executor import to_bitunix_symbol
        from risk import determine_new_sl_target

        logger.info(f"Startup catch-up: checking {len(bx_positions)} Bitunix position(s)")

        for pos in bx_positions:
            bitunix_sym = to_bitunix_symbol(pos.symbol)
            tracked = self._get_tp_tracking(pos)

            # ── Step 1: Rebuild complete TP state from exchange ──
            # Query BOTH pending and history to build a full picture of all TPs
            # (handles: all pending, all fired, or mix of both)
            if not tracked and pos.bitunix_position_id:
                try:
                    # Fetch pending TP orders (still active on exchange)
                    pending = await asyncio.to_thread(
                        self.bitunix_client.get_pending_tpsl_orders, bitunix_sym
                    )
                    # Fetch historical TP orders (already triggered/cancelled)
                    history = await asyncio.to_thread(
                        self.bitunix_client.get_history_tpsl_orders, bitunix_sym, 200
                    )

                    def _match_pct(tp_price):
                        """Match a TP price to pos.take_profits to get close percentage."""
                        if not pos.take_profits or tp_price <= 0:
                            return 0
                        closest = min(pos.take_profits, key=lambda tp: abs(tp.price - tp_price), default=None)
                        if closest and closest.price > 0 and abs(closest.price - tp_price) / closest.price < 0.01:
                            return closest.percentage
                        return 0

                    rebuilt = []
                    seen_prices = set()

                    # Add still-pending TP orders (not yet hit)
                    for o in pending:
                        o_pid = o.get("positionId")
                        if o_pid and o_pid != pos.bitunix_position_id:
                            continue
                        tp_price = float(o.get("tpPrice") or o.get("triggerPrice") or 0)
                        if tp_price > 0 and round(tp_price, 2) not in seen_prices:
                            oid = o.get("id") or o.get("orderId")
                            rebuilt.append({
                                "orderId": oid,
                                "price": tp_price,
                                "pct": _match_pct(tp_price),
                                "hit": False,
                            })
                            seen_prices.add(round(tp_price, 2))

                    # Add already-triggered TP orders from history
                    for o in history:
                        if o.get("positionId") != pos.bitunix_position_id:
                            continue
                        status = (o.get("status") or "").upper()
                        if status in ("SYSTEM_CANCELED", "CANCELED"):
                            continue
                        tp_price = float(o.get("tpPrice") or o.get("triggerPrice") or 0)
                        if tp_price > 0 and round(tp_price, 2) not in seen_prices:
                            oid = o.get("id") or o.get("orderId")
                            rebuilt.append({
                                "orderId": oid or f"history_{tp_price}",
                                "price": tp_price,
                                "pct": _match_pct(tp_price),
                                "hit": True,  # Already triggered
                            })
                            seen_prices.add(round(tp_price, 2))

                    if rebuilt:
                        key = pos.bitunix_position_id
                        self._bx_tp_tracking[key] = rebuilt
                        self._save_bx_tp_tracking()
                        tracked = rebuilt
                        n_pending = sum(1 for t in rebuilt if not t["hit"])
                        n_hit = sum(1 for t in rebuilt if t["hit"])
                        logger.info(
                            f"Startup: rebuilt TP tracking for {pos.symbol} {pos.side} "
                            f"from exchange ({n_pending} pending, {n_hit} already hit)"
                        )
                    else:
                        logger.info(f"Startup: no TP orders found for {pos.symbol} {pos.side}")
                except Exception as e:
                    logger.warning(f"Startup: failed to rebuild TPs for {pos.symbol}: {e}")

            # ── Step 2: For existing tracking, check if unfilled TPs fired while offline ──
            if tracked:
                unfilled = [t for t in tracked if not t["hit"]]
                if unfilled:
                    try:
                        pending = await asyncio.to_thread(
                            self.bitunix_client.get_pending_tpsl_orders, bitunix_sym
                        )
                        pending_ids = set()
                        for o in pending:
                            oid = o.get("id") or o.get("orderId")
                            if oid:
                                pending_ids.add(oid)

                        new_hits = []
                        for tp_info in unfilled:
                            oid = tp_info["orderId"]
                            if oid in pending_ids:
                                continue  # Still pending — not triggered
                            was_triggered = await self._verify_bitunix_tp_triggered(
                                bitunix_sym, oid
                            )
                            if was_triggered:
                                tp_info["hit"] = True
                                new_hits.append(tp_info)
                                logger.info(
                                    f"Startup catch-up: {pos.symbol} {pos.side} "
                                    f"TP @ ${tp_info['price']:,.2f} was hit while offline"
                                )
                    except Exception as e:
                        logger.warning(f"Startup catch-up TP check failed for {pos.symbol}: {e}")
                        new_hits = []
                else:
                    new_hits = []

                # ── Step 3: Rebuild verified_decreases from all hits ──
                total_hits = sum(1 for t in tracked if t["hit"])
                if total_hits > 0:
                    # Save tracking state if any new hits found
                    if new_hits:
                        self._save_bx_tp_tracking()

                    # Rebuild verified_decreases from ALL hit entries
                    pos.verified_decreases = []
                    for t in tracked:
                        if not t["hit"]:
                            continue
                        tp_size = pos.original_size_usd * t.get("pct", 0)
                        if pos.entry_price and pos.entry_price > 0:
                            if pos.side == "LONG":
                                tp_pnl = (t["price"] - pos.entry_price) / pos.entry_price * tp_size
                            else:
                                tp_pnl = (pos.entry_price - t["price"]) / pos.entry_price * tp_size
                        else:
                            tp_pnl = 0
                        pos.verified_decreases.append({
                            "execution_price": t["price"],
                            "matched_tp_price": t["price"],
                            "size_delta_usd": tp_size,
                            "pnl_usd": tp_pnl,
                            "net_pnl_usd": tp_pnl,
                            "order_type": 5,
                            "timestamp": time.time(),
                            "source": "bitunix",
                        })
                    pos.realized_pnl = sum(
                        d.get("net_pnl_usd", 0) for d in pos.verified_decreases
                    )
                    self._save_position_state()

                    # Notify if any new hits were found (skip if just restoring known state)
                    if new_hits or not getattr(pos, '_startup_hits_notified', False):
                        n_pending = sum(1 for t in tracked if not t["hit"])
                        collateral = pos.size_usd / pos.leverage if pos.leverage else pos.size_usd
                        pnl_pct_str = f" ({pos.realized_pnl / collateral * 100:+.1f}%)" if collateral > 0 else ""
                        r_sign = "+" if pos.realized_pnl >= 0 else "-"
                        status_parts = []
                        if total_hits:
                            status_parts.append(f"{total_hits} hit")
                        if n_pending:
                            status_parts.append(f"{n_pending} pending")
                        await self.notify(
                            f"STARTUP CATCH-UP: BITUNIX {pos.symbol} {pos.side} {pos.leverage:.1f}x\n"
                            f"TPs: {' / '.join(status_parts)}\n"
                            f"Realized: {r_sign}${abs(pos.realized_pnl):,.2f}{pnl_pct_str}"
                        )
                        pos._startup_hits_notified = True

                    # Correct SL level based on TP hits — only if current SL is wrong
                    sorted_tp_prices = sorted(t["price"] for t in tracked)
                    new_sl, sl_label = determine_new_sl_target(
                        total_hits, pos.entry_price, sorted_tp_prices, pos.leverage
                    )
                    if new_sl is not None and pos.stop_loss is not None:
                        tolerance = pos.entry_price * 0.003 if pos.entry_price else 1.0
                        sl_diff = abs(pos.stop_loss - new_sl)
                        sl_already_better = (
                            (pos.side == "LONG" and pos.stop_loss > new_sl + tolerance)
                            or (pos.side == "SHORT" and pos.stop_loss < new_sl - tolerance)
                        )
                        if sl_already_better:
                            logger.info(
                                f"Startup: {pos.symbol} SL already at better level "
                                f"(${pos.stop_loss:,.2f}) than trailing target "
                                f"(${new_sl:,.2f} {sl_label}) — keeping current"
                            )
                        elif sl_diff > tolerance:
                            await self._move_bitunix_sl(pos, new_sl, sl_label)
                    elif new_sl is not None and pos.stop_loss is None:
                        await self._move_bitunix_sl(pos, new_sl, sl_label)

            # ── Step 3 (no new hits): Still verify SL is at correct level ──
            if pos.tp_hits_count > 0 and pos.take_profits:
                sorted_tp_prices = sorted(tp.price for tp in pos.take_profits)
                new_sl, sl_label = determine_new_sl_target(
                    pos.tp_hits_count, pos.entry_price, sorted_tp_prices, pos.leverage
                )
                if new_sl is not None and pos.stop_loss is not None:
                    tolerance = pos.entry_price * 0.003 if pos.entry_price else 1.0
                    sl_diff = abs(pos.stop_loss - new_sl)
                    # Don't downgrade SL if user manually moved it to a better level
                    # For LONG: higher SL = more protective; for SHORT: lower SL = more protective
                    sl_already_better = (
                        (pos.side == "LONG" and pos.stop_loss > new_sl + tolerance)
                        or (pos.side == "SHORT" and pos.stop_loss < new_sl - tolerance)
                    )
                    if sl_already_better:
                        logger.info(
                            f"Startup: {pos.symbol} SL already at better level "
                            f"(${pos.stop_loss:,.2f}) than trailing target "
                            f"(${new_sl:,.2f} {sl_label}) — keeping current"
                        )
                    elif sl_diff > tolerance:
                        logger.info(
                            f"Startup: {pos.symbol} SL stale — "
                            f"${pos.stop_loss:,.2f} should be ${new_sl:,.2f} ({sl_label})"
                        )
                        await self._move_bitunix_sl(pos, new_sl, sl_label)

        logger.info("Startup catch-up complete")

    # ──────────────────────────────────────────────────────────────────────
    # Bitunix Close (manual)
    # ──────────────────────────────────────────────────────────────────────

    async def close_bitunix_position(self, pos) -> bool:
        """Close a Bitunix position via flash close.

        Returns True if closed successfully.
        """
        from bitunix_executor import to_bitunix_symbol

        if not self.bitunix_client:
            return False

        position_id = pos.bitunix_position_id
        if not position_id:
            position_id = await self._find_bitunix_position_id(pos)

        if not position_id:
            logger.warning(f"Cannot close {pos.symbol}: no Bitunix position found")
            return False

        if self.cfg.dry_run:
            logger.info(f"[DRY RUN] Would close Bitunix {pos.symbol} {pos.side}")
            return True

        try:
            result = await asyncio.to_thread(
                self.bitunix_client.flash_close_position, position_id
            )
            logger.info(f"Closed Bitunix {pos.symbol} {pos.side} ({position_id})")
            # Verify close by checking if position still exists
            try:
                bx_sym = to_bitunix_symbol(pos.symbol)
                remaining = await asyncio.to_thread(
                    self.bitunix_client.get_pending_positions, bx_sym
                )
                still_open = any(
                    p.get("positionId") == position_id for p in remaining
                )
                if still_open:
                    logger.warning(f"Bitunix {pos.symbol} still open after flash close — may need manual close")
                    return False
            except Exception as ve:
                logger.debug(f"Could not verify Bitunix close: {ve}")
            return True
        except Exception as e:
            logger.error(f"Failed to close Bitunix position {pos.symbol}: {e}")
            return False
