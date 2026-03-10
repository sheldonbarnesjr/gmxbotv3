"""
Stop Loss & Take Profit (SL/TP) Mixin for GMX V2 Trading Bot.

Contains methods for:
  - Moving stop loss orders after TP hits
  - Monitoring TP price triggers
  - Manual SL/TP management via Telegram
  - Order creation/cancellation

SLTPMixin is designed to be mixed into GMXBot.

Expected host class attributes:
  cfg, logger, client, notify, send_message, w3, account, account2, account3, account4,
  positions, trade_history, health_stats,
  _all_wallets, _get_account, get_current_price, _fetch_all_positions_and_orders,
  send_message
"""

import asyncio
import time
import logging
import traceback
from typing import Dict, Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from gmx import Position
from web3 import Web3

from open import (
    fetch_open_orders, create_sl_order, create_tp_order, TakeProfit,
    EXCHANGE_ROUTER_ABI, ORDER_TYPE_STOP_LOSS_DECREASE, ORDER_TYPE_LIMIT_DECREASE,
)
import open as _open_mod
from close import fetch_positions as chain_fetch_positions, fetch_position_pnl, GMXPosition
from history import fetch_recent_position_decreases
from risk import verify_tp_hit_by_price, determine_new_sl_target, calculate_unrealized_pnl


logger = logging.getLogger("GMXBot.sl_tp")


class SLTPMixin:
    """Mixin providing SL/TP management for GMXBot."""

    # ──────────────────────────────────────────────────────────────────────
    # TP Monitor: check_tp_hits (run in background loop)
    # ──────────────────────────────────────────────────────────────────────

    # Counter for stale order checks (runs every 12th cycle ≈ 60s)
    _stale_check_counter: int = 0

    # Per-position asyncio locks to prevent concurrent state mutation
    _position_locks: Dict[str, asyncio.Lock] = {}

    def _get_pos_lock(self, pos_id: str) -> asyncio.Lock:
        """Return (creating if needed) an asyncio.Lock for the given position."""
        if pos_id not in self._position_locks:
            self._position_locks[pos_id] = asyncio.Lock()
        return self._position_locks[pos_id]

    def _get_stale_tp_misses(self):
        """Get per-instance stale TP miss tracker (lazy init to avoid mutable class var)."""
        if not hasattr(self, '_stale_tp_misses_inst'):
            self._stale_tp_misses_inst = {}
        return self._stale_tp_misses_inst

    def _tp_already_verified(self, pos, tp_price, tolerance_pct=0.01):
        """Check if a TP price already has a matching entry in verified_decreases."""
        if tp_price <= 0:
            return False
        return any(
            abs(d.get("matched_tp_price", 0) - tp_price) / tp_price < tolerance_pct
            for d in pos.verified_decreases
        )

    async def check_tp_hits(self, lookback_override: int = None):
        """Monitor PositionDecrease events to detect and process TP hits.

        Called from tp_monitor_loop every 5 seconds.
        For each open position:
          1. Fetch recent PositionDecrease events (with rate-limit retry)
          2. Skip already-processed events (dedup by tx_hash:log_index)
          3. Match execution price to unexecuted TPs via verify_tp_hit_by_price
          4. Update state, calculate PnL, move SL, notify admin
          5. Every ~60s: cross-check on-chain orders for stale/missing TPs

        Args:
            lookback_override: If set, override the default 600s lookback window.
                               Used on startup for catch-up (e.g. 3600 = 1 hour).
        """
        # Skip during cooldown (e.g. after manual order changes or sync)
        if self._in_orders_cooldown():
            return

        for pos_id, pos in list(self.positions.items()):
            if not pos.is_open or not pos.take_profits:
                # Clean up lock for closed positions
                self._position_locks.pop(pos_id, None)
                continue
            if not pos.market_addr:
                continue
            # Skip Bitunix positions — they're monitored by BitunixMonitorMixin
            if getattr(pos, 'exchange', 'gmx') == 'bitunix':
                continue

            try:
                acct = self._get_account(pos.wallet_id)
                is_long = pos.side == "LONG"

                # Step 1: Fetch PositionDecrease events with rate-limit retry
                lookback = lookback_override or 600
                decreases = await self._fetch_decreases_with_retry(
                    acct.address, pos.market_addr, is_long,
                    lookback_seconds=lookback,
                )
                if not decreases:
                    continue

                # Acquire per-position lock to prevent concurrent state mutation
                async with self._get_pos_lock(pos_id):

                    # Step 2: Filter out already-processed events
                    new_events = [
                        d for d in decreases
                        if d.get("tx_hash")
                        and f"{d['tx_hash']}:{d.get('log_index', 0)}" not in pos.processed_tx_hashes
                    ]
                    if not new_events:
                        continue

                    # Step 3: Match events to un-verified TPs using on-chain order_type
                    # order_type: 5=TP (LimitDecrease), 6=SL (StopLossDecrease),
                    #             4=MarketDecrease (manual), None=unknown (fallback)
                    if is_long:
                        sorted_tps = sorted(pos.take_profits, key=lambda tp: tp.price)
                    else:
                        sorted_tps = sorted(pos.take_profits, key=lambda tp: tp.price, reverse=True)

                    new_hits = 0

                    for event in new_events:
                        event_key = f"{event['tx_hash']}:{event.get('log_index', 0)}"
                        exec_price = event.get("execution_price", 0)
                        if not exec_price:
                            pos.processed_tx_hashes.add(event_key)
                            continue

                        evt_order_type = event.get("order_type")
                        evt_trigger = event.get("trigger_price", 0)

                        # SL execution: order_type=6, or trigger_price matches SL
                        is_sl = evt_order_type == ORDER_TYPE_STOP_LOSS_DECREASE
                        if not is_sl and evt_trigger > 0 and pos.stop_loss:
                            if abs(evt_trigger - pos.stop_loss) / pos.stop_loss < 0.005:
                                is_sl = True
                        if is_sl:
                            self.logger.info(
                                f"{pos.symbol}: SL hit @ ${exec_price:,.0f} "
                                f"(order_type={evt_order_type}, trigger=${evt_trigger:,.0f}, "
                                f"sl_level={pos.sl_move_label or 'original'}, "
                                f"tx={event['tx_hash'][:16]}...)"
                            )
                            pos.processed_tx_hashes.add(event_key)
                            continue

                        # Manual close (order_type=4) — not a TP
                        if evt_order_type == 4:  # MarketDecrease
                            self.logger.info(
                                f"{pos.symbol}: Manual close @ ${exec_price:,.0f} "
                                f"(order_type=4, tx={event['tx_hash'][:16]}...)"
                            )
                            pos.processed_tx_hashes.add(event_key)
                            continue

                        # TP execution (order_type=5) or unknown (None → fallback to price matching)
                        # Use trigger_price (from OrderCreated) as primary match — more reliable
                        # than execution_price which is the actual fill price and may differ
                        trigger_price = event.get("trigger_price", 0)
                        matched = False
                        for i, tp in enumerate(sorted_tps):
                            if self._tp_already_verified(pos, tp.price):
                                continue
                            # Try trigger_price first (exact match from OrderCreated event)
                            trigger_match = (
                                trigger_price > 0
                                and verify_tp_hit_by_price(is_long, tp.price, trigger_price, tolerance_pct=0.01)
                            )
                            # Fallback to execution_price (actual fill price)
                            exec_match = verify_tp_hit_by_price(is_long, tp.price, exec_price, tolerance_pct=0.01)
                            if trigger_match or exec_match:
                                match_price = trigger_price if trigger_match else exec_price
                                pos.verified_decreases.append({
                                    "execution_price": exec_price,
                                    "net_pnl_usd": event.get("net_pnl_usd", 0),
                                    "timestamp": event.get("timestamp", time.time()),
                                    "tx_hash": event.get("tx_hash", ""),
                                    "log_index": event.get("log_index", 0),
                                    "size_delta_usd": event.get("size_delta_usd", 0),
                                    "matched_tp_price": tp.price,
                                    "order_type": evt_order_type,
                                })
                                new_hits += 1
                                matched = True
                                source = "trigger" if trigger_match else ("on-chain" if evt_order_type == ORDER_TYPE_LIMIT_DECREASE else "price-match")
                                self.logger.info(
                                    f"{pos.symbol} TP{i+1} HIT @ ${match_price:,.0f} "
                                    f"({source}, order_type={evt_order_type}, "
                                    f"pnl=${event.get('net_pnl_usd', 0):,.2f}, "
                                    f"tx={event['tx_hash'][:16]}...)"
                                )
                                break

                        if not matched:
                            if evt_order_type == ORDER_TYPE_LIMIT_DECREASE:
                                # Confirmed TP from chain but no price match — log warning
                                self.logger.warning(
                                    f"{pos.symbol}: TP execution (order_type=5) @ ${exec_price:,.0f} "
                                    f"did not match any unverified TP price"
                                )
                            else:
                                self.logger.debug(
                                    f"{pos.symbol}: PositionDecrease @ ${exec_price:,.0f} "
                                    f"order_type={evt_order_type} — no TP match"
                                )
                        # Mark as processed regardless of match
                        pos.processed_tx_hashes.add(event_key)

                    # Step 4: Process hits — PnL, notify, move SL
                    if new_hits > 0:
                        # tp_hits_count is now a @property = len(verified_decreases)
                        self.logger.info(
                            f"{pos.symbol} {pos.side}: {new_hits} new TP hits, "
                            f"total={pos.tp_hits_count}"
                        )

                        # Calculate realized PnL from verified_decreases (on-chain source of truth)
                        realized_pnl = sum(d.get("net_pnl_usd", 0) for d in pos.verified_decreases)
                        pos.realized_pnl = realized_pnl
                        self._save_position_state()

                        # Calculate unrealized PnL on remaining position
                        current_price = await self.get_current_price(pos.symbol)
                        if current_price:
                            pos.current_price = current_price
                        total_decreased = sum(d.get("size_delta_usd", 0) for d in pos.verified_decreases)
                        base_size = pos.original_size_usd if pos.original_size_usd > 0 else pos.size_usd
                        remaining_size = max(base_size - total_decreased, 0.0)
                        unrealized_pnl = calculate_unrealized_pnl(
                            pos.side, pos.entry_price, pos.current_price, remaining_size
                        )

                        # Try fee-inclusive PnL from Reader contract (includes
                        # borrowing, funding, and closing fees)
                        try:
                            collateral_token = getattr(pos, 'collateral_token', None) or self.cfg.collateral_token
                            pnl_data = await asyncio.to_thread(
                                fetch_position_pnl,
                                self.w3, acct.address, pos.market_addr,
                                collateral_token, is_long, pos.current_price,
                            )
                            if pnl_data.get("success") and pnl_data.get("net_pnl_usd") is not None:
                                unrealized_pnl = pnl_data["net_pnl_usd"]
                        except Exception as e:
                            self.logger.debug(f"Reader PnL unavailable, using price-delta: {e}")
                        total_pnl = realized_pnl + unrealized_pnl
                        r_sign = "+" if realized_pnl >= 0 else ""
                        u_sign = "+" if unrealized_pnl >= 0 else ""
                        t_sign = "+" if total_pnl >= 0 else ""

                        col = pos.collateral_usd
                        pnl_pct_str = f" ({total_pnl / col * 100:+.1f}%)" if col > 0 else ""
                        # Get execution price from the latest verified decrease
                        latest_vd = pos.verified_decreases[-1] if pos.verified_decreases else {}
                        exec_price = latest_vd.get("execution_price", pos.current_price or 0)
                        try:
                            await self.notify(
                                f"GMX {pos.symbol} {pos.side} {pos.leverage:.1f}x: Target {pos.tp_hits_count} Hit ✅\n"
                                f"Realized: {r_sign}${realized_pnl:,.2f} @ ${exec_price:,.2f}\n"
                                f"Unrealized: {u_sign}${unrealized_pnl:,.2f}\n"
                                f"Total PnL: {t_sign}${total_pnl:,.2f}{pnl_pct_str}"
                            )
                        except Exception:
                            pass

                        # Fetch orders (needed by move_sl for SL cancellation) and move SL
                        try:
                            orders = await asyncio.to_thread(
                                fetch_open_orders, self.w3, acct.address
                            )
                            await self.move_sl(pos, orders, _lock_held=True)
                            self._save_position_state()
                        except Exception as e:
                            self.logger.warning(f"Failed to move SL after TP hit: {e}")
                            await self.notify(
                                f"⚠️ {pos.symbol} {pos.side} [W{pos.wallet_id}]: "
                                f"TP hit but failed to move SL: {e}"
                            )

            except Exception as e:
                self.logger.debug(f"Error checking TPs for {pos.symbol}: {e}")

        # ── Stale order check (every ~60s) ──
        # Cross-check on-chain TP orders against internal state.
        # If a TP order disappeared from chain but no PositionDecrease event was
        # found, it may be a missed hit or cancellation.
        self._stale_check_counter = getattr(self, '_stale_check_counter', 0) + 1
        if self._stale_check_counter >= 12:  # every 12 cycles ≈ 60s
            self._stale_check_counter = 0
            await self._check_stale_orders()

    async def _check_stale_orders(self):
        """Cross-check on-chain orders with internal TP tracking.

        Detects TPs that disappeared from chain without a matching
        PositionDecrease event (e.g., event older than lookback window).
        Uses 2-consecutive-miss guard to avoid false positives from RPC lag.
        """
        for pos_id, pos in list(self.positions.items()):
            if not pos.is_open or not pos.take_profits or not pos.market_addr:
                continue
            if getattr(pos, 'exchange', 'gmx') == 'bitunix':
                continue

            try:
                acct = self._get_account(pos.wallet_id)
                is_long = pos.side == "LONG"

                # Fetch current on-chain orders
                orders = await asyncio.to_thread(
                    fetch_open_orders, self.w3, acct.address
                )
                market_lower = pos.market_addr.lower()

                # Build set of on-chain TP prices
                on_chain_tp_prices = set()
                tp_order_count = 0
                for o in orders:
                    if (o["market"].lower() == market_lower
                            and o["order_type"] == ORDER_TYPE_LIMIT_DECREASE):
                        on_chain_tp_prices.add(round(o["trigger_price"], 2))
                        tp_order_count += 1

                # Check each un-verified internal TP against on-chain
                newly_marked = 0
                for tp in pos.take_profits:
                    if self._tp_already_verified(pos, tp.price):
                        continue
                    miss_key = f"{pos.id}:{tp.price}"

                    # Check if this TP matches any on-chain order (1% tolerance)
                    matched_on_chain = any(
                        abs(tp.price - otp) / tp.price < 0.01
                        for otp in on_chain_tp_prices
                    ) if tp.price > 0 else False

                    if matched_on_chain:
                        # TP still on-chain — clear any miss counter
                        self._get_stale_tp_misses().pop(miss_key, None)
                        continue

                    # TP missing from chain — track consecutive misses
                    miss_count = self._get_stale_tp_misses().get(miss_key, 0) + 1
                    self._get_stale_tp_misses()[miss_key] = miss_count

                    if miss_count < 2:
                        self.logger.info(
                            f"Stale check: {pos.symbol} TP @ ${tp.price:,.2f} "
                            f"not on-chain (miss {miss_count}/2) — waiting for confirmation"
                        )
                        continue

                    # 2nd miss — try to verify via PositionDecrease events
                    # Use longer lookback since this is catching older events
                    try:
                        decreases = await self._fetch_decreases_with_retry(
                            acct.address, pos.market_addr, is_long,
                            lookback_seconds=1800,  # 30-min lookback for stale check
                        )
                    except Exception:
                        decreases = []

                    verified = False
                    for evt in decreases:
                        event_key = f"{evt.get('tx_hash', '')}:{evt.get('log_index', 0)}"
                        if event_key in pos.processed_tx_hashes:
                            continue
                        # Skip SL/manual close events — only match confirmed TPs or unknowns
                        evt_order_type = evt.get("order_type")
                        if evt_order_type == ORDER_TYPE_STOP_LOSS_DECREASE:
                            pos.processed_tx_hashes.add(event_key)
                            continue
                        if evt_order_type == 4:  # MarketDecrease
                            pos.processed_tx_hashes.add(event_key)
                            continue
                        exec_price = evt.get("execution_price", 0)
                        if exec_price and tp.price > 0:
                            if verify_tp_hit_by_price(is_long, tp.price, exec_price, tolerance_pct=0.01):
                                pos.verified_decreases.append({
                                    "execution_price": exec_price,
                                    "net_pnl_usd": evt.get("net_pnl_usd", 0),
                                    "timestamp": evt.get("timestamp", time.time()),
                                    "tx_hash": evt.get("tx_hash", ""),
                                    "log_index": evt.get("log_index", 0),
                                    "size_delta_usd": evt.get("size_delta_usd", 0),
                                    "matched_tp_price": tp.price,
                                    "order_type": evt_order_type,
                                })
                                pos.processed_tx_hashes.add(event_key)
                                newly_marked += 1
                                verified = True
                                self._get_stale_tp_misses().pop(miss_key, None)
                                source = "on-chain" if evt_order_type == ORDER_TYPE_LIMIT_DECREASE else "price-match"
                                self.logger.info(
                                    f"Stale check: {pos.symbol} TP @ ${tp.price:,.2f} "
                                    f"verified via PositionDecrease event @ ${exec_price:,.0f} ({source})"
                                )
                                break

                    if not verified:
                        self.logger.warning(
                            f"Stale check: {pos.symbol} TP @ ${tp.price:,.2f} "
                            f"not on-chain (2 cycles) and no PositionDecrease event found. "
                            f"NOT marking as hit (likely cancellation/resync)."
                        )
                        self._get_stale_tp_misses().pop(miss_key, None)

                # If stale check found new hits, process them
                if newly_marked > 0:
                    self.logger.info(
                        f"Stale check: {pos.symbol} {pos.side}: "
                        f"{newly_marked} stale TP(s) recovered, total={pos.tp_hits_count}"
                    )

                    # Recalculate realized PnL from verified_decreases
                    pos.realized_pnl = sum(d.get("net_pnl_usd", 0) for d in pos.verified_decreases)
                    self._save_position_state()

                    # Move SL if needed
                    try:
                        await self.move_sl(pos, orders)
                        self._save_position_state()
                    except Exception as e:
                        self.logger.warning(
                            f"Stale check: failed to move SL for {pos.symbol}: {e}"
                        )

                    self._set_orders_cooldown(30)

            except Exception as e:
                self.logger.debug(f"Stale order check error for {pos.symbol}: {e}")

    async def _fetch_decreases_with_retry(
        self,
        wallet_address: str,
        market_addr: str,
        is_long: bool,
        lookback_seconds: int = 600,
        max_retries: int = 3,
        base_delay: float = 5.0,
    ) -> list:
        """Fetch PositionDecrease events with retry on rate-limit errors.

        On rate-limit / too-many-calls errors: waits with exponential
        backoff (5s, 10s, 20s) and retries up to max_retries times.
        """
        for attempt in range(1, max_retries + 1):
            try:
                decreases = await asyncio.to_thread(
                    fetch_recent_position_decreases,
                    self.w3, wallet_address, market_addr,
                    is_long, lookback_seconds,
                )
                return decreases
            except Exception as e:
                err_str = str(e).lower()
                is_rate_limit = any(kw in err_str for kw in [
                    "too many", "rate limit", "429", "throttl",
                ])
                if is_rate_limit and attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))
                    self.logger.warning(
                        f"Rate limited fetching PositionDecrease events "
                        f"(attempt {attempt}/{max_retries}), "
                        f"retrying in {delay:.0f}s: {e}"
                    )
                    await asyncio.sleep(delay)
                else:
                    if is_rate_limit:
                        self.logger.warning(
                            f"Rate limited fetching PositionDecrease events "
                            f"(attempt {attempt}/{max_retries}), giving up: {e}"
                        )
                    else:
                        self.logger.warning(
                            f"Failed to fetch position decreases: {e}"
                        )
                    return []
        return []

    # ──────────────────────────────────────────────────────────────────────
    # Move SL after TP hits or manual command
    # ──────────────────────────────────────────────────────────────────────

    async def move_sl(self, pos: "Position", orders: list, new_sl_price: Optional[float] = None, sl_label: Optional[str] = None, *, manual: bool = False, _lock_held: bool = False):
        """Move SL to entry (breakeven) or previous TP after TP hit(s).

        Called in two ways:
          1. Auto mode (from check_tp_hits): new_sl_price=None, sl_label=None
             → Auto-compute target using determine_new_sl_target()
          2. Manual mode (from cmd_sl): new_sl_price and sl_label provided, manual=True
             → Use them directly; skip the auto notification (cmd_sl sends its own)

        On-chain flow:
          1. Cancel existing SL order(s) for this market
          2. Create new SL order at the new price
          3. Update in-memory state
          4. Notify admin (auto mode only)

        Args:
            pos: Internal Position object
            orders: List of open orders (for this wallet)
            new_sl_price: (Optional) Manual SL price (from cmd_sl). If None, auto-compute.
            sl_label: (Optional) Label for manual SL (e.g., "Entry", "TP1", "TP2")
            manual: If True, suppress the notification (caller handles its own).
            _lock_held: If True, skip acquiring the position lock (caller already holds it).
        """
        if not _lock_held:
            await self._get_pos_lock(pos.id).acquire()
        try:
            if pos.tp_hits_count == 0 and new_sl_price is None:
                return

            # Auto-compute target if not provided manually
            if new_sl_price is None:
                sorted_tps = sorted(
                    pos.take_profits,
                    key=lambda tp: tp.price,
                    reverse=(pos.side == "SHORT")
                )
                new_sl_price, sl_label = determine_new_sl_target(
                    pos.tp_hits_count, pos.entry_price, sorted_tps,
                    leverage=pos.leverage,
                )

            # None means no SL move needed for this TP hit (e.g. TP2 → stay at Entry)
            if new_sl_price is None:
                self.logger.info(f"{pos.symbol} {pos.side}: TP{pos.tp_hits_count} hit — no SL move (trailing strategy)")
                return

            # Don't downgrade SL if already at a better (more protective) level
            if not manual and pos.stop_loss is not None:
                tolerance = pos.entry_price * 0.003 if pos.entry_price else 1.0
                sl_diff = abs(pos.stop_loss - new_sl_price)
                if sl_diff < tolerance:
                    self.logger.info(
                        f"{pos.symbol} {pos.side}: SL already at target "
                        f"(${pos.stop_loss:,.2f} ≈ ${new_sl_price:,.2f}) — skip"
                    )
                    return
                sl_already_better = (
                    (pos.side == "LONG" and pos.stop_loss > new_sl_price + tolerance)
                    or (pos.side == "SHORT" and pos.stop_loss < new_sl_price - tolerance)
                )
                if sl_already_better:
                    self.logger.info(
                        f"{pos.symbol} {pos.side}: SL already at better level "
                        f"(${pos.stop_loss:,.2f}) than trailing target "
                        f"(${new_sl_price:,.2f} {sl_label}) — keeping current"
                    )
                    return

            self.logger.info(f"{pos.symbol} {pos.side}: Moving SL to {sl_label} (${new_sl_price:,.0f})")

            cfg = self.cfg
            acct = self._get_account(pos.wallet_id)
            exchange = self.w3.eth.contract(
                address=Web3.to_checksum_address(cfg.exchange_router),
                abi=EXCHANGE_ROUTER_ABI,
            )
            wallet = Web3.to_checksum_address(acct.address)

            # 1. Cancel existing SL orders for this market
            # Re-fetch orders fresh to get accurate keys (avoids stale key misalignment)
            market_lower = pos.market_addr.lower() if pos.market_addr else ""
            cancelled = 0
            for cleanup_round in range(3):
                fresh_orders = await asyncio.to_thread(fetch_open_orders, self.w3, acct.address)
                all_sl_orders = [
                    o for o in fresh_orders
                    if o["market"].lower() == market_lower
                    and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
                ]
                keyless = [o for o in all_sl_orders if not o.get("key_hex")]
                if keyless:
                    self.logger.warning(
                        f"{pos.symbol}: {len(keyless)} SL order(s) missing key_hex — "
                        f"cannot cancel. May leave orphaned SL on-chain."
                    )
                    await self.notify(
                        f"⚠️ {pos.symbol} {pos.side} [W{pos.wallet_id}]: "
                        f"{len(keyless)} SL order(s) missing key_hex — "
                        f"cannot cancel old SL. Skipping new SL placement to prevent double SL execution."
                    )
                    return
                sl_orders = [o for o in all_sl_orders if o.get("key_hex")]
                if not sl_orders:
                    break
                for sl_order in sl_orders:
                    try:
                        key_bytes = bytes.fromhex(sl_order["key_hex"])
                        data = exchange.encode_abi("cancelOrder", [key_bytes])
                        tx = _open_mod.build_tx(self.w3, wallet, exchange.address, data, value=0)
                        txh = _open_mod.sign_send(self.w3, acct, tx, dry_run=cfg.dry_run)
                        if not cfg.dry_run:
                            _open_mod.wait_receipt(self.w3, txh)
                        cancelled += 1
                        self.logger.info(f"Cancelled old SL order for {pos.symbol}: {txh}")
                    except Exception as e:
                        self.logger.warning(f"Cancel SL failed for {pos.symbol}: {e}")
                await asyncio.sleep(2)

            order_vault = Web3.to_checksum_address(cfg.order_vault)
            collateral_token = Web3.to_checksum_address(cfg.collateral_token)

            # Calculate remaining position size after verified TP decreases.
            # Use size_delta_usd from verified_decreases (on-chain source of truth).
            # After a restart pos.size_usd is synced from on-chain (already reduced).
            total_decreased = sum(d.get("size_delta_usd", 0) for d in pos.verified_decreases)
            if total_decreased > 0:
                base_size = pos.original_size_usd if pos.original_size_usd > 0 else pos.size_usd
                remaining_size = max(base_size - total_decreased, 0.0)
            else:
                remaining_size = pos.size_usd

            # Skip SL placement if remaining size is dust (below min position)
            if remaining_size < cfg.min_position_usd:
                self.logger.info(
                    f"{pos.symbol} {pos.side}: remaining size ${remaining_size:.2f} below "
                    f"min ${cfg.min_position_usd:.0f} — skipping SL placement"
                )
                # Position effectively closed — clean up lock
                self._position_locks.pop(pos.id, None)
                return

            # 2. Create new SL order at new price (with retry)
            MAX_SL_ATTEMPTS = 3
            SL_RETRY_DELAY = 5  # seconds
            new_sl_txh = None
            last_sl_error = None

            for attempt in range(1, MAX_SL_ATTEMPTS + 1):
                try:
                    if attempt > 1:
                        self.logger.info(
                            f"{pos.symbol} {pos.side}: SL move attempt {attempt}/{MAX_SL_ATTEMPTS}..."
                        )
                    new_sl_txh = await asyncio.to_thread(
                        create_sl_order,
                        self.w3, acct, exchange, acct.address,
                        pos.market_addr, collateral_token, order_vault,
                        new_sl_price, remaining_size, pos.symbol, pos.side == "LONG",
                        cfg.slippage_bps, cfg.execution_fee_wei, cfg.dry_run,
                    )
                    self.logger.info(
                        f"New SL order created for {pos.symbol} at ${new_sl_price:,.0f} "
                        f"size=${remaining_size:,.2f}: {new_sl_txh}"
                    )
                    pos.sl_tx_hash = new_sl_txh
                    pos.order_history.append({
                        "order_type": "sl_move",
                        "tx_hash": new_sl_txh,
                        "price": new_sl_price,
                        "label": sl_label,
                        "status": "placed",
                        "timestamp": time.time(),
                    })
                    break  # success
                except RuntimeError as e:
                    # Reverted on-chain — no point retrying
                    last_sl_error = e
                    self.logger.error(
                        f"SL move REVERTED for {pos.symbol} (attempt {attempt}): {e}"
                    )
                    break  # exit retry loop immediately
                except (ConnectionError, TimeoutError, OSError) as e:
                    last_sl_error = e
                    self.logger.warning(
                        f"SL move network error {attempt}/{MAX_SL_ATTEMPTS} for {pos.symbol}: {e}"
                    )
                    if attempt < MAX_SL_ATTEMPTS:
                        await asyncio.sleep(SL_RETRY_DELAY)
                except Exception as e:
                    last_sl_error = e
                    self.logger.warning(
                        f"SL move attempt {attempt}/{MAX_SL_ATTEMPTS} failed for "
                        f"{pos.symbol}: {e}"
                    )
                    if attempt < MAX_SL_ATTEMPTS:
                        await asyncio.sleep(SL_RETRY_DELAY)

            if new_sl_txh is None:
                self.logger.error(
                    f"Failed to create new SL order for {pos.symbol} after "
                    f"{MAX_SL_ATTEMPTS} attempts: {last_sl_error}"
                )
                pos.sl_move_failed = True
                self._save_position_state()

                # Queue for automatic retry via order_retry_loop
                from gmx import FailedOrder  # late import to avoid circular dep
                self.failed_order_queue.append(FailedOrder(
                    position_id=pos.id,
                    symbol=pos.symbol,
                    side=pos.side,
                    market_addr=pos.market_addr,
                    wallet_id=pos.wallet_id,
                    order_kind="sl",
                    price=new_sl_price,
                    size_usd=remaining_size,
                    close_pct=1.0,
                    is_long=(pos.side == "LONG"),
                    error=str(last_sl_error),
                ))
                self.logger.info(
                    f"Queued failed SL for retry: {pos.symbol} {pos.side} "
                    f"@ ${new_sl_price:,.2f} size=${remaining_size:,.2f}"
                )
                self._save_failed_orders()

                await self.notify(
                    f"⚠️ {pos.symbol} {pos.side} [W{pos.wallet_id}]: "
                    f"Failed to move SL to {sl_label} (${new_sl_price:,.2f}) "
                    f"after {MAX_SL_ATTEMPTS} attempts\n"
                    f"Error: {last_sl_error}\n"
                    f"Old SL cancelled ({cancelled}). Queued for automatic retry."
                )
                return

            # 3. Post-creation duplicate check: verify only 1 SL remains
            await asyncio.sleep(1)
            try:
                verify_orders = await asyncio.to_thread(fetch_open_orders, self.w3, acct.address)
                remaining_sls = [
                    o for o in verify_orders
                    if o["market"].lower() == market_lower
                    and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
                    and o.get("key_hex")
                ]
                if len(remaining_sls) > 1:
                    self.logger.warning(
                        f"{pos.symbol}: {len(remaining_sls)} SL orders after move — "
                        f"cleaning up {len(remaining_sls) - 1} stale order(s)"
                    )
                    # Keep the newest (highest updated_at_block), cancel others
                    remaining_sls.sort(key=lambda o: o.get("updated_at_block", 0))
                    for stale_sl in remaining_sls[:-1]:
                        try:
                            key_bytes = bytes.fromhex(stale_sl["key_hex"])
                            data = exchange.encode_abi("cancelOrder", [key_bytes])
                            tx = _open_mod.build_tx(self.w3, wallet, exchange.address, data, value=0)
                            txh = _open_mod.sign_send(self.w3, acct, tx, dry_run=cfg.dry_run)
                            if not cfg.dry_run:
                                _open_mod.wait_receipt(self.w3, txh)
                            self.logger.info(f"Cleaned up stale SL: {txh}")
                        except Exception as e:
                            self.logger.warning(f"Failed to clean up stale SL: {e}")
            except Exception as e:
                self.logger.debug(f"Post-creation SL verify failed: {e}")

            # 4. Update in-memory state
            old_sl = pos.stop_loss
            pos.sl_moved_to_entry = True
            pos.sl_move_label = sl_label
            pos.stop_loss = new_sl_price
            pos.sl_move_failed = False

            # 4b. Set cooldown to prevent TP monitor from interfering
            self._set_orders_cooldown(30)

            # 5. Notify admin (auto mode only — manual callers send their own)
            if not manual:
                old_sl_str = f"${old_sl:,.2f}" if old_sl else "None"
                await self.notify(
                    f"SL Moved GMX {pos.symbol} {pos.side} {pos.leverage:.1f}x\n"
                    f"{old_sl_str} -> ${new_sl_price:,.2f} ({sl_label}) ✅"
                )

        except Exception as e:
            self.logger.error(f"Error in move_sl: {e}")
            pos.sl_move_failed = True
            self._save_position_state()
            try:
                await self.notify(
                    f"⚠️ {pos.symbol} {pos.side} [W{pos.wallet_id}]: "
                    f"SL move failed unexpectedly: {e}\n"
                    f"Position may be UNPROTECTED — check manually."
                )
            except Exception:
                pass
        finally:
            if not _lock_held:
                self._get_pos_lock(pos.id).release()

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /sl <#> <target>
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_sl(self, chat_id: int, arg: Optional[str]):
        """Telegram /sl command handler.

        Manually move SL to entry (breakeven), a TP level, or a custom price.

        Usage:
            /sl                   — show open positions & available SL targets
            /sl 1 entry           — move position #1 SL to entry price
            /sl 1 tp2             — move position #1 SL to TP2 price
            /sl 1 72500           — move position #1 SL to $72,500

        Args:
            chat_id: Telegram chat ID
            arg: "<position_number> <target>" where target is "entry", "tp1"-"tp8", or a price
        """
        try:
            positions, orders = await self._fetch_all_positions_and_orders()
            if not positions:
                await self.send_message(chat_id, "No open positions.")
                return

            if not arg or not arg.strip():
                msg = "**Move Stop Loss**\n\nUsage: `/sl <#> <target>`\n\n"
                for i, pos in enumerate(positions, 1):
                    side = "LONG" if pos.is_long else "SHORT"
                    wid_label = f" [W{pos._wallet_id}]" if hasattr(pos, '_wallet_id') else ""
                    market_lower = pos.market.lower()
                    pos_wid = getattr(pos, '_wallet_id', 1)
                    sl_orders = [o for o in orders
                                 if o["market"].lower() == market_lower
                                 and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
                                 and o.get("_wallet_id", 1) == pos_wid]
                    current_sl = f"${sl_orders[0]['trigger_price']:,.2f}" if sl_orders else "None"

                    internal_pos = None
                    for p in self.positions.values():
                        if (p.is_open and p.market_addr
                                and p.market_addr.lower() == market_lower
                                and p.side == side
                                and p.wallet_id == pos_wid):
                            internal_pos = p
                            break

                    targets = ["entry"]
                    if internal_pos and internal_pos.take_profits:
                        sorted_tps = sorted(internal_pos.take_profits,
                                            key=lambda t: t.price,
                                            reverse=(side == "SHORT"))
                        for j, tp in enumerate(sorted_tps, 1):
                            targets.append(f"tp{j} (${tp.price:,.2f})")

                    msg += (
                        f"**#{i} {pos.symbol} {side}{wid_label}**\n"
                        f"  Entry: ${pos.entry_price:,.2f} | SL: {current_sl}\n"
                        f"  Targets: {', '.join(targets)}\n\n"
                    )
                msg += "Example: `/sl 1 entry` or `/sl 1 tp2` or `/sl 1 72500`"
                await self.send_message(chat_id, msg)
                return

            parts = arg.strip().split()
            if len(parts) < 2:
                await self.send_message(chat_id, "Usage: `/sl <#> <target>`\nExample: `/sl 1 entry` or `/sl 1 tp2` or `/sl 1 72500`")
                return

            try:
                pos_num = int(parts[0])
            except ValueError:
                await self.send_message(chat_id, f"Invalid position number: {parts[0]}")
                return

            target = parts[1].lower().strip()

            if pos_num < 1 or pos_num > len(positions):
                await self.send_message(chat_id, f"Position #{pos_num} not found. Use /sl to see available positions.")
                return

            chain_pos = positions[pos_num - 1]
            side = "LONG" if chain_pos.is_long else "SHORT"
            market_lower = chain_pos.market.lower()

            internal_pos = None
            wid = getattr(chain_pos, '_wallet_id', 0)
            for p in self.positions.values():
                if (p.is_open and p.market_addr
                        and p.market_addr.lower() == market_lower
                        and p.side == side
                        and p.wallet_id == wid):
                    internal_pos = p
                    break

            if not internal_pos:
                await self.send_message(
                    chat_id,
                    f"Position #{pos_num} ({chain_pos.symbol} {side}) not tracked internally. "
                    "Cannot resolve TP prices — try using /addorder to set TP levels first."
                )
                return

            if target == "entry":
                new_sl_price = internal_pos.entry_price
                sl_label = "Entry"
            elif target.startswith("tp"):
                try:
                    tp_num = int(target[2:])
                except ValueError:
                    await self.send_message(chat_id, f"Invalid target: {target}. Use 'entry', 'tp1'-'tp8', or a price.")
                    return
                sorted_tps = sorted(internal_pos.take_profits,
                                    key=lambda t: t.price,
                                    reverse=(side == "SHORT"))
                if tp_num < 1 or tp_num > len(sorted_tps):
                    await self.send_message(chat_id, f"TP{tp_num} not found. Position has {len(sorted_tps)} TP(s).")
                    return
                new_sl_price = sorted_tps[tp_num - 1].price
                sl_label = f"Target {tp_num}"
            else:
                # Try parsing as a raw price
                try:
                    new_sl_price = float(target.replace(",", "").replace("$", ""))
                except ValueError:
                    await self.send_message(chat_id, f"Invalid target: {target}. Use 'entry', 'tp1'-'tp8', or a price.")
                    return
                if new_sl_price <= 0:
                    await self.send_message(chat_id, "Price must be positive.")
                    return
                sl_label = f"${new_sl_price:,.2f}"

            current_price = chain_pos.current_price or await self.get_current_price(chain_pos.symbol)
            if current_price:
                if side == "LONG" and new_sl_price >= current_price:
                    await self.send_message(
                        chat_id,
                        f"⚠️ Warning: SL ${new_sl_price:,.2f} is above current price "
                        f"${current_price:,.2f} for a LONG. This would close the position immediately.\n"
                        "Proceeding anyway..."
                    )
                elif side == "SHORT" and new_sl_price <= current_price:
                    await self.send_message(
                        chat_id,
                        f"⚠️ Warning: SL ${new_sl_price:,.2f} is below current price "
                        f"${current_price:,.2f} for a SHORT. This would close the position immediately.\n"
                        "Proceeding anyway..."
                    )

            pos_acct = self._get_account(internal_pos.wallet_id)
            try:
                fresh_orders = await asyncio.to_thread(fetch_open_orders, self.w3, pos_acct.address)
            except Exception as e:
                await self.send_message(chat_id, f"Error fetching orders: {e}")
                return

            # Call move_sl with manual=True so it skips its own notification
            await self.move_sl(internal_pos, fresh_orders, new_sl_price, sl_label, manual=True)

            if internal_pos.sl_move_failed:
                # move_sl already sent a failure notification via notify()
                await self.send_message(
                    chat_id,
                    f"❌ Failed to move SL for #{pos_num} {internal_pos.symbol} {side}. Check logs."
                )
            else:
                await self.send_message(
                    chat_id,
                    f"✅ SL moved for #{pos_num} {internal_pos.symbol} {side} [W{internal_pos.wallet_id}] "
                    f"→ {sl_label} (${new_sl_price:,.2f})"
                )
                self._save_position_state()

        except Exception as e:
            self.logger.error(f"cmd_sl error: {e}\n{traceback.format_exc()}")
            await self.send_message(chat_id, f"Error: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /addorder <sl|tp> <#> <prices...>
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_addorder(self, chat_id: int, arg: Optional[str]):
        """Telegram /addorder command handler.

        Manually add SL or TP orders to an open position.

        Usage:
            /addorder                           — show available positions
            /addorder sl 1 95000                — add SL to position #1 at $95k
            /addorder tp 1 100000               — add single TP to position #1 at $100k
            /addorder tp 1 98000 100000 103000  — add multiple TPs (split remaining size evenly)

        TPs are allocated by weight based on how many already exist:
          - Existing TPs are ranked 1, 2, ...
          - New TPs get higher weight numbers
          - Size = remaining_size * (weight / total_weight_sum)

        Args:
            chat_id: Telegram chat ID
            arg: "<sl|tp> <position_number> <price1> [price2] [price3] ..."
        """
        cfg = self.cfg
        await self.send_message(chat_id, "Fetching positions...")
        try:
            positions, orders = await self._fetch_all_positions_and_orders()
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching positions: {e}")
            return

        if not positions:
            await self.send_message(chat_id, "No open positions on-chain.")
            return

        if arg is None:
            msg = "**Open Positions**\n\n"
            for i, pos in enumerate(positions, 1):
                side = "LONG" if pos.is_long else "SHORT"
                wid = ""
                if hasattr(pos, '_wallet_id'):
                    wid = f" [W{pos._wallet_id}]"
                pos_orders = [o for o in orders if o["market"].lower() == pos.market.lower()]
                sl_orders = [o for o in pos_orders if o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE]
                tp_orders = sorted([o for o in pos_orders if o["order_type"] == ORDER_TYPE_LIMIT_DECREASE], key=lambda o: o["trigger_price"])
                msg += (
                    f"**#{i} {pos.symbol} {side}{wid}**\n"
                    f"  Size: ${pos.size_usd:,.2f} @ {pos.leverage:.1f}x\n"
                    f"  Entry: ${pos.entry_price:,.2f}  |  Current: ${pos.current_price:,.2f}\n"
                )
                if sl_orders:
                    # Show only one SL; warn if duplicates exist on-chain
                    if len(sl_orders) > 1:
                        msg += f"  ⚠️ {len(sl_orders)} SL orders (duplicates)\n"
                    msg += f"  SL @ ${sl_orders[0]['trigger_price']:,.2f}\n"
                if tp_orders:
                    for j, o in enumerate(tp_orders, 1):
                        msg += f"  TP{j} @ ${o['trigger_price']:,.2f}  (${o['size_usd']:,.2f})\n"
                existing_tp_size = sum(o["size_usd"] for o in tp_orders)
                remaining = max(0, pos.size_usd - existing_tp_size)
                remaining_pct = (remaining / pos.size_usd * 100) if pos.size_usd > 0 else 0
                if tp_orders:
                    msg += f"  Remaining: ${remaining:,.2f} ({remaining_pct:.0f}%)\n"
                msg += "\n"

            msg += "**Usage:**\n"
            msg += "  /addorder sl 1 95000              — add SL on #1\n"
            msg += "  /addorder tp 1 100000             — single TP (uses all remaining size)\n"
            msg += "  /addorder tp 1 98000 100000 103000 — multiple TPs (split remaining evenly)"
            await self.send_message(chat_id, msg)
            return

        parts = arg.strip().split()
        if len(parts) < 3:
            await self.send_message(
                chat_id,
                "Usage:\n  /addorder sl <#> <price>\n  /addorder tp <#> <price>           — single TP (auto-size)\n  /addorder tp <#> <p1> <p2> <p3>    — multiple TPs (split evenly)"
            )
            return

        order_kind = parts[0].upper()
        if order_kind not in ("SL", "TP"):
            await self.send_message(chat_id, "First argument must be 'sl' or 'tp'.")
            return

        try:
            pos_num = int(parts[1])
        except ValueError:
            await self.send_message(chat_id, "Position number must be an integer.")
            return
        if pos_num < 1 or pos_num > len(positions):
            await self.send_message(chat_id, f"Position #{pos_num} out of range (1-{len(positions)}).")
            return

        pos = positions[pos_num - 1]
        acct = getattr(pos, '_wallet_acct', self.account)
        side = "LONG" if pos.is_long else "SHORT"

        exchange = self.w3.eth.contract(
            address=Web3.to_checksum_address(cfg.exchange_router),
            abi=EXCHANGE_ROUTER_ABI,
        )
        order_vault = Web3.to_checksum_address(cfg.order_vault)
        collateral_token = Web3.to_checksum_address(cfg.collateral_token)

        if order_kind == "SL":
            try:
                price = float(parts[2].replace(",", ""))
            except ValueError:
                await self.send_message(chat_id, "Price must be a number.")
                return
            if price <= 0:
                await self.send_message(chat_id, "Price must be positive.")
                return
            if pos.is_long and price >= pos.entry_price:
                await self.send_message(chat_id, f"Warning: SL ${price:,.2f} is above entry ${pos.entry_price:,.2f} for a LONG. Proceeding anyway...")
            elif not pos.is_long and price <= pos.entry_price:
                await self.send_message(chat_id, f"Warning: SL ${price:,.2f} is below entry ${pos.entry_price:,.2f} for a SHORT. Proceeding anyway...")

            try:
                # Cancel existing SL orders before placing new one
                market_lower = pos.market.lower()
                fresh_orders = await asyncio.to_thread(fetch_open_orders, self.w3, acct.address)
                existing_sls = [
                    o for o in fresh_orders
                    if o["market"].lower() == market_lower
                    and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
                    and o.get("key_hex")
                ]
                for sl_order in existing_sls:
                    try:
                        key_bytes = bytes.fromhex(sl_order["key_hex"])
                        data = exchange.encode_abi("cancelOrder", [key_bytes])
                        tx = _open_mod.build_tx(self.w3, acct.address, exchange.address, data, value=0)
                        _open_mod.sign_send(self.w3, acct, tx, dry_run=cfg.dry_run)
                        self.logger.info(f"addorder: cancelled existing SL for {pos.symbol}")
                    except Exception as cancel_err:
                        self.logger.warning(f"addorder: failed to cancel existing SL: {cancel_err}")
                if existing_sls:
                    await asyncio.sleep(2)

                self.logger.info(f"addorder: placing SL on {pos.symbol} {side} at ${price:,.2f} size=${pos.size_usd:,.2f}")
                txh = await asyncio.to_thread(
                    create_sl_order,
                    self.w3, acct, exchange, acct.address,
                    pos.market, collateral_token, order_vault,
                    price, pos.size_usd, pos.symbol, pos.is_long,
                    cfg.slippage_bps, cfg.execution_fee_wei, cfg.dry_run,
                )
                await self.send_message(
                    chat_id,
                    f"SL placed on {pos.symbol} {side} @ ${price:,.2f}\nSize: ${pos.size_usd:,.2f} (100% close)\nTX: {txh}"
                )
                # Update internal position state
                internal_pos = self._find_internal_position(
                    pos.market, pos.is_long,
                    pos._wallet_id if hasattr(pos, '_wallet_id') else 0
                )
                if internal_pos:
                    internal_pos.stop_loss = price
                    self._save_position_state()
            except Exception as e:
                self.logger.error(f"addorder SL failed: {e}\n{traceback.format_exc()}")
                await self.send_message(chat_id, f"Failed to place SL: {e}")
            return

        # ── TP (single or multiple) ──
        price_strs = parts[2:]
        tp_prices = []
        for ps in price_strs:
            try:
                p = float(ps.replace(",", ""))
                if p <= 0:
                    await self.send_message(chat_id, f"Price must be positive, got: {ps}")
                    return
                tp_prices.append(p)
            except ValueError:
                await self.send_message(chat_id, f"Invalid price: '{ps}'")
                return

        if not tp_prices:
            await self.send_message(chat_id, "Provide at least one TP price.")
            return

        pos_tp_orders = [o for o in orders if o["market"].lower() == pos.market.lower() and o["order_type"] == 5]
        existing_tp_size = sum(o["size_usd"] for o in pos_tp_orders)
        remaining_size = max(0, pos.size_usd - existing_tp_size)

        if remaining_size <= 0:
            await self.send_message(
                chat_id,
                f"Existing TPs already cover the full position size (${existing_tp_size:,.2f} / ${pos.size_usd:,.2f}).\nCancel a TP first with /cancelorder."
            )
            return

        num_existing = len(pos_tp_orders)
        num_new = len(tp_prices)
        total_tps = num_existing + num_new
        new_weights = [i for i in range(num_existing + 1, total_tps + 1)]
        weight_sum = sum(new_weights)

        tp_allocations = []
        for i, tp_price in enumerate(sorted(tp_prices, reverse=(not pos.is_long))):
            tp_size = remaining_size * (new_weights[i] / weight_sum)
            tp_pct = tp_size / pos.size_usd
            tp_allocations.append((tp_price, tp_size, tp_pct))

        collateral_usd = pos.size_usd / pos.leverage if pos.leverage else pos.size_usd

        results = []
        for tp_price, tp_size, tp_pct in tp_allocations:
            tp = TakeProfit(price=tp_price, close_pct=tp_pct)
            try:
                txh = await asyncio.to_thread(
                    create_tp_order,
                    self.w3, acct, exchange, acct.address,
                    pos.market, collateral_token, order_vault,
                    tp, pos.size_usd, collateral_usd,
                    pos.symbol, pos.is_long,
                    cfg.slippage_bps, cfg.execution_fee_wei, cfg.dry_run,
                )
                results.append((tp_price, tp_size, tp_pct * 100, txh, None))
            except Exception as e:
                self.logger.error(f"addorder TP @ ${tp_price:,.2f} failed: {e}")
                results.append((tp_price, tp_size, tp_pct * 100, None, str(e)))

        ok = [r for r in results if r[3]]
        fail = [r for r in results if r[4]]

        msg = f"**Add TP — {pos.symbol} {side}**\n\n"
        for tp_price, size, pct, txh, err in results:
            if txh:
                msg += f"  TP @ ${tp_price:,.2f} — ${size:,.2f} ({pct:.0f}%) — {txh}\n"
            else:
                msg += f"  TP @ ${tp_price:,.2f} — FAILED: {err}\n"

        if ok and not fail:
            msg += f"\n{len(ok)} TP(s) placed successfully."
        elif ok and fail:
            msg += f"\n{len(ok)} placed, {len(fail)} failed."
        else:
            msg += "\nAll TPs failed."

        # Update internal position state with successfully placed TPs
        if ok:
            internal_pos = self._find_internal_position(pos.market, pos.is_long, pos._wallet_id if hasattr(pos, '_wallet_id') else 0)
            if internal_pos:
                for tp_price, tp_size, tp_pct_100, txh, err in results:
                    if txh:
                        from gmx import TakeProfitLevel
                        internal_pos.take_profits.append(
                            TakeProfitLevel(price=tp_price, percentage=tp_pct_100 / 100.0)
                        )
                self._save_position_state()

            # Set cooldown so TP monitor doesn't interfere with new orders
            self._set_orders_cooldown(30)

        await self.send_message(chat_id, msg)

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /cancelorder [#|all]
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_cancelorder(self, chat_id: int, arg: Optional[str]):
        """Telegram /cancelorder command handler.

        List and cancel individual SL/TP orders by number.

        Usage:
            /cancelorder              — list all open orders with numbers
            /cancelorder 3            — cancel order #3
            /cancelorder 1,3,5        — cancel multiple orders
            /cancelorder all          — cancel all cancellable orders (SL, TP, LimitIncrease)

        Market orders (MarketIncrease, MarketDecrease) cannot be cancelled (they execute immediately).

        Args:
            chat_id: Telegram chat ID
            arg: "<order_number>[,<number>...]" or "all"
        """
        cfg = self.cfg
        await self.send_message(chat_id, "Fetching open orders...")
        try:
            _positions, all_orders = await self._fetch_all_positions_and_orders()
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching orders: {e}")
            return

        ORDER_TYPE_NAMES = {2: "MarketInc", 3: "LimitInc", 4: "MarketDec", 5: "TP", 6: "SL"}
        CANCELLABLE = {3, 5, 6}

        if not all_orders:
            await self.send_message(chat_id, "No open orders on-chain.")
            return

        numbered = []
        for o in all_orders:
            o_type = o["order_type"]
            cancellable = o_type in CANCELLABLE and o.get("key_hex")
            numbered.append({**o, "_cancellable": cancellable})

        if arg is None:
            msg = "**Open Orders**\n\n"
            for i, o in enumerate(numbered, 1):
                label = ORDER_TYPE_NAMES.get(o["order_type"], f"Type{o['order_type']}")
                side = "LONG" if o["is_long"] else "SHORT"
                cancel_mark = "" if o["_cancellable"] else " (not cancellable)"
                wid = ""
                if o.get("_wallet_id"):
                    wid = f" [W{o['_wallet_id']}]"
                msg += (
                    f"**#{i}** {o['symbol']} {side} — {label} "
                    f"@ ${o['trigger_price']:,.2f}  "
                    f"(${o['size_usd']:,.2f}){wid}{cancel_mark}\n"
                )
            msg += "\nReply with:\n  /cancelorder 3    — cancel order #3\n  /cancelorder 1,3  — cancel multiple\n  /cancelorder all  — cancel all"
            await self.send_message(chat_id, msg)
            return

        arg_stripped = arg.strip().upper()
        if arg_stripped == "ALL":
            indices = [i for i, o in enumerate(numbered) if o["_cancellable"]]
        else:
            try:
                indices = []
                for part in arg.replace(" ", ",").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    num = int(part)
                    if num < 1 or num > len(numbered):
                        await self.send_message(chat_id, f"Order #{num} out of range (1-{len(numbered)})")
                        return
                    indices.append(num - 1)
            except ValueError:
                await self.send_message(chat_id, "Usage: /cancelorder 3  or  /cancelorder 1,3,5  or  /cancelorder all")
                return

        if not indices:
            await self.send_message(chat_id, "No cancellable orders selected.")
            return

        # Filter out non-cancellable orders and warn user
        valid_indices = []
        for idx in indices:
            o = numbered[idx]
            if not o["_cancellable"]:
                label = ORDER_TYPE_NAMES.get(o["order_type"], f"Type{o['order_type']}")
                await self.send_message(
                    chat_id,
                    f"Order #{idx+1} ({o['symbol']} {label}) cannot be cancelled (market orders execute immediately). Skipping."
                )
            else:
                valid_indices.append(idx)

        if not valid_indices:
            return

        indices = valid_indices
        exchange = self.w3.eth.contract(
            address=Web3.to_checksum_address(cfg.exchange_router),
            abi=EXCHANGE_ROUTER_ABI,
        )

        cancelled = 0
        failed = 0
        for idx in indices:
            o = numbered[idx]
            label = ORDER_TYPE_NAMES.get(o["order_type"], f"Type{o['order_type']}")
            key_bytes = bytes.fromhex(o["key_hex"])
            acct = o.get("_wallet_acct", self.account)
            wallet = Web3.to_checksum_address(acct.address)

            self.logger.info(f"cancelorder: cancelling #{idx+1} {o['symbol']} {label} key=0x{o['key_hex'][:16]}...")

            if cfg.dry_run:
                self.logger.info(f"  [DRY_RUN] Would cancel order #{idx+1}")
                cancelled += 1
                continue

            try:
                data = exchange.encode_abi("cancelOrder", [key_bytes])
                tx = _open_mod.build_tx(self.w3, wallet, exchange.address, data, value=0)
                txh = _open_mod.sign_send(self.w3, acct, tx, dry_run=False)
                receipt = _open_mod.wait_receipt(self.w3, txh)
                if receipt.get("status") == 1:
                    self.logger.info(f"  Cancelled #{idx+1}: {txh}")
                    cancelled += 1
                else:
                    self.logger.warning(f"  Cancel tx reverted for #{idx+1}: {txh}")
                    failed += 1
            except Exception as e:
                self.logger.warning(f"  Failed to cancel #{idx+1}: {e}")
                failed += 1

        parts_msg = []
        if cancelled:
            parts_msg.append(f"{cancelled} cancelled")
        if failed:
            parts_msg.append(f"{failed} failed")
        summary = ", ".join(parts_msg)

        detail_lines = []
        for idx in indices:
            o = numbered[idx]
            label = ORDER_TYPE_NAMES.get(o["order_type"], f"Type{o['order_type']}")
            detail_lines.append(f"  #{idx+1} {o['symbol']} {label} @ ${o['trigger_price']:,.2f}")

        msg = f"**Cancel Orders: {summary}**\n" + "\n".join(detail_lines)

        # Set cooldown so TP monitor doesn't misinterpret cancellations as hits
        if cancelled:
            self._set_orders_cooldown(30)

        await self.send_message(chat_id, msg)

    # ──────────────────────────────────────────────────────────────────────
    # Channel-based TP confirmation (fallback Layer 3)
    # ──────────────────────────────────────────────────────────────────────

    async def check_channel_tp_confirmation(self, text: str):
        """Check if a channel update message confirms a TP hit for any tracked position.

        Called when the signal handler detects an update message (not a new signal).
        Parses the message for target-hit patterns and matches against tracked positions
        that have unexecuted TPs where SL hasn't been moved yet.

        This is a safety net: if on-chain detection AND historical price check both
        miss a TP hit (extremely unlikely), the channel announcement catches it.
        """
        import re
        lower = text.lower()

        # Extract which target number was hit (if mentioned)
        tp_num_match = re.search(
            r"(?:target|tp)\s*(\d+)\s*(?:was\s+)?(?:hit|reached|smashed|done|achieved|✅)",
            lower,
        )
        # Also detect "all targets hit"
        all_hit = bool(re.search(r"all\s*(?:tp|targets?)\s*(?:hit|reached|done|smashed)", lower))

        if not tp_num_match and not all_hit:
            return

        # Try to identify which symbol this is about
        symbol_found = None
        for sym in ("BTC", "ETH", "SOL", "LINK"):
            if sym.lower() in lower or f"${sym.lower()}" in lower:
                symbol_found = sym
                break
        # Also check for full names
        sym_aliases = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}
        if not symbol_found:
            for alias, sym in sym_aliases.items():
                if alias in lower:
                    symbol_found = sym
                    break

        # Build list of candidate positions (with un-verified TPs)
        candidates = [
            (pid, p) for pid, p in self.positions.items()
            if p.is_open and p.take_profits
            and any(not self._tp_already_verified(p, tp.price) for tp in p.take_profits)
        ]

        # If symbol identified, filter to that symbol
        if symbol_found:
            candidates = [(pid, p) for pid, p in candidates if p.symbol == symbol_found]
        elif len(candidates) > 1:
            # Without symbol identification, only act if there's exactly one
            # open position with unexecuted TPs to avoid false positives
            self.logger.debug(
                f"Channel TP confirmation: can't determine symbol from message, "
                f"and {len(candidates)} positions open — skipping to avoid false match"
            )
            return

        for pos_id, pos in candidates:

            if pos.side == "LONG":
                sorted_tps = sorted(pos.take_profits, key=lambda tp: tp.price)
            else:
                sorted_tps = sorted(pos.take_profits, key=lambda tp: tp.price, reverse=True)

            if all_hit:
                # Mark all remaining TPs as hit via synthetic verified_decreases
                hits = 0
                base_size = pos.original_size_usd if pos.original_size_usd > 0 else pos.size_usd
                ts_now = int(time.time())
                for i, tp in enumerate(sorted_tps):
                    if not self._tp_already_verified(pos, tp.price):
                        pos.verified_decreases.append({
                            "execution_price": tp.price,
                            "net_pnl_usd": 0,
                            "timestamp": time.time(),
                            "tx_hash": f"channel:{ts_now}:{i}",
                            "log_index": i,
                            "size_delta_usd": base_size * tp.percentage,
                            "matched_tp_price": tp.price,
                        })
                        hits += 1
                if hits > 0:
                    pos.realized_pnl = sum(d.get("net_pnl_usd", 0) for d in pos.verified_decreases)
                    self._save_position_state()
                    self.logger.info(
                        f"{pos.symbol}: Channel confirmed ALL targets hit — "
                        f"marked {hits} TP(s), moving SL"
                    )
                    await self.notify(
                        f"📢 {pos.symbol} {pos.side} [W{pos.wallet_id}]: "
                        f"Channel confirmed all targets hit — moving SL"
                    )
                    try:
                        acct = self._get_account(pos.wallet_id)
                        orders = await asyncio.to_thread(fetch_open_orders, self.w3, acct.address)
                        await self.move_sl(pos, orders)
                    except Exception as e:
                        self.logger.warning(f"Channel TP confirm: failed to move SL: {e}")
                        await self.notify(
                            f"⚠️ {pos.symbol}: Channel confirmed TPs but SL move failed: {e}"
                        )

            elif tp_num_match:
                tp_num = int(tp_num_match.group(1))
                # Mark all TPs up to and including tp_num via synthetic verified_decreases
                hits = 0
                base_size = pos.original_size_usd if pos.original_size_usd > 0 else pos.size_usd
                for i, tp in enumerate(sorted_tps):
                    if i + 1 > tp_num:
                        break
                    if not self._tp_already_verified(pos, tp.price):
                        pos.verified_decreases.append({
                            "execution_price": tp.price,
                            "net_pnl_usd": 0,
                            "timestamp": time.time(),
                            "tx_hash": f"channel:{int(time.time())}:{i}",
                            "log_index": i,
                            "size_delta_usd": base_size * tp.percentage,
                            "matched_tp_price": tp.price,
                        })
                        hits += 1

                if hits > 0:
                    pos.realized_pnl = sum(d.get("net_pnl_usd", 0) for d in pos.verified_decreases)
                    self._save_position_state()
                    self.logger.info(
                        f"{pos.symbol}: Channel confirmed TP{tp_num} hit — "
                        f"marked {hits} TP(s), moving SL"
                    )
                    await self.notify(
                        f"📢 {pos.symbol} {pos.side} [W{pos.wallet_id}]: "
                        f"Channel confirmed TP{tp_num} hit — moving SL"
                    )
                    try:
                        acct = self._get_account(pos.wallet_id)
                        orders = await asyncio.to_thread(fetch_open_orders, self.w3, acct.address)
                        await self.move_sl(pos, orders)
                    except Exception as e:
                        self.logger.warning(f"Channel TP confirm: failed to move SL: {e}")
                        await self.notify(
                            f"⚠️ {pos.symbol}: Channel confirmed TP{tp_num} but SL move failed: {e}"
                        )
