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

    BX_TP_TRACKING_FILE = "bx_tp_tracking.json"

    def _init_bitunix_monitor(self):
        """Call from GMXBot.__init__ to initialize Bitunix monitor state."""
        # TP order tracking: pos_id -> [{orderId, price, pct, hit}, ...]
        self._bx_tp_tracking: Dict[str, List[Dict[str, Any]]] = self._load_bx_tp_tracking()
        # Missing position counter for reconciliation
        self._bx_missing_count: Dict[str, int] = {}
        # Reconciliation cycle counter
        self._bx_reconcile_counter: int = 0

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

    # ──────────────────────────────────────────────────────────────────────
    # TP Order Registration
    # ──────────────────────────────────────────────────────────────────────

    def register_bitunix_tp_orders(self, pos_id: str, tp_results: list):
        """Register TP order IDs for tracking after position open.

        tp_results: [{orderId, price, pct}, ...]
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
            self._bx_tp_tracking[pos_id] = tracked
            self._save_bx_tp_tracking()
            logger.info(f"Registered {len(tracked)} TP orders for {pos_id}")

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
            tracked = self._bx_tp_tracking.get(pos.id)
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
                    pos.verified_decreases = [
                        {
                            "execution_price": t["price"],
                            "matched_tp_price": t["price"],
                            "size_delta_usd": pos.original_size_usd * t.get("pct", 0),
                            "net_pnl_usd": 0,  # Not available from Bitunix TP tracking
                            "timestamp": time.time(),
                            "source": "bitunix",
                        }
                        for t in tracked if t["hit"]
                    ]

                    # Notify each TP hit
                    for tp_info in new_hits:
                        await self.notify(
                            f"**TP Hit** [{pos.exchange.upper()}] {pos.symbol} {pos.side}\n"
                            f"TP @ ${tp_info['price']:,.2f} ({tp_info['pct']:.0%})\n"
                            f"Hits: {total_hits}/{len(tracked)}"
                        )

                    # Persist position state with updated verified_decreases
                    self._save_position_state()

                    # Move SL after TP hit
                    sorted_tp_prices = sorted(t["price"] for t in tracked)
                    new_sl, sl_label = determine_new_sl_target(
                        total_hits, pos.entry_price, sorted_tp_prices, pos.leverage
                    )
                    if new_sl and new_sl != pos.stop_loss:
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
                f"**SL Moved** [{pos.exchange.upper()}] {pos.symbol} {pos.side}\n"
                f"${old_sl:,.2f} -> ${new_sl:,.2f} ({label})"
            )
            logger.info(f"SL moved for {pos.symbol}: ${old_sl:,.2f} -> ${new_sl:,.2f} ({label})")
        except Exception as e:
            logger.error(f"Failed to move SL for {pos.symbol}: {e}")
            pos.sl_move_failed = True
            self._save_position_state()
            await self.notify(
                f"**SL Move FAILED** [{pos.exchange.upper()}] {pos.symbol} {pos.side}\n"
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

        # Build set of (symbol, side) that exist on exchange
        exchange_set = set()
        for ep in exchange_positions:
            sym = (ep.get("symbol") or "").replace("USDT", "")
            # Handle special names like 1000PEPE
            if sym.startswith("1000"):
                sym = sym[4:]
            raw_side = (ep.get("side") or "").upper()
            side = "LONG" if raw_side in ("BUY", "LONG") else "SHORT"
            exchange_set.add((sym, side))

        for pos in bx_positions:
            key = (pos.symbol, pos.side)
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

                    # Determine exit reason BEFORE removing tracking data
                    tp_hits = sum(1 for t in self._bx_tp_tracking.get(pos.id, []) if t.get("hit"))
                    self._bx_tp_tracking.pop(pos.id, None)
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

                    pnl_sign = "+" if pos.unrealized_pnl >= 0 else ""
                    await self.notify(
                        f"**Position Closed** [BITUNIX] {pos.symbol} {pos.side}\n"
                        f"Entry: ${pos.entry_price:,.2f}\n"
                        f"PnL: {pnl_sign}${pos.unrealized_pnl:,.2f}\n"
                        f"Reason: {pos.exit_reason}\n"
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
                                            f"(Bitunix {pos.exit_reason})\nTX: {tx}"
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
