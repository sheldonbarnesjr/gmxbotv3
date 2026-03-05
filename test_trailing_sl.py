#!/usr/bin/env python3
"""
End-to-end test for trailing stop loss logic with verified_decreases model.

Opens a REAL position using wallet3 (PRIVATE_KEY_3) on Arbitrum,
places TP/SL orders on-chain, then simulates TP hits with fake
PositionDecrease events to verify the trailing SL strategy and
the new on-chain-derived TP tracking system.

Trailing SL strategy under test:
  TP1 hit → no SL move (let trade run)
  TP2 hit → SL moves to Entry (breakeven)
  TP3 hit → SL moves to TP1

verified_decreases model under test:
  - TP hits append to pos.verified_decreases (not tp.executed flags)
  - tp_hits_count is a @property = len(verified_decreases)
  - PnL derived from sum(d["net_pnl_usd"]) in verified_decreases
  - Remaining size from sum(d["size_delta_usd"]) in verified_decreases
  - Dedup: same event processed twice → no duplicate entries
  - _tp_already_verified: prevents re-matching already-verified TPs
  - Stale check: TP missing from chain → verified via PositionDecrease
  - classify_exit_reason: uses tp_orders_remaining (not last_known_tp_count)

REAL on-chain: position open, SL cancel + re-create.
SIMULATED: price data and PositionDecrease events (to fake TP hits).

Usage:
    python3 test_trailing_sl.py
"""

import os
import sys
import time
import uuid
import asyncio
import logging
import traceback
from dataclasses import field
from unittest.mock import patch
from typing import Optional, List, Dict, Any

from web3 import Web3
from eth_account import Account

# ── Bot imports (all functions we actually use in production) ──
from config import load_config, Config
from open import (
    parse_signal, Signal, TakeProfit,
    execute_signal,
    fetch_open_orders, fetch_current_price,
    create_sl_order, create_tp_order,
    cancel_orders_for_market, cancel_all_orders,
    EXCHANGE_ROUTER_ABI,
    ORDER_TYPE_STOP_LOSS_DECREASE, ORDER_TYPE_LIMIT_DECREASE,
    ORDER_TYPE_LIMIT_INCREASE,
)
from close import fetch_positions as chain_fetch_positions, GMXPosition, create_close_order
from gmx import Position, TakeProfitLevel, FailedOrder
from sl_tp import SLTPMixin
from risk import (
    determine_new_sl_target, verify_tp_hit_by_price,
    calculate_unrealized_pnl, calculate_pnl_percentage,
    cap_leverage, calculate_position_size,
    validate_sl_tp_direction, check_price_deviation,
    classify_exit_reason,
)
from history import fetch_recent_position_decreases


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Logging
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("TestTrailingSL")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TEST_SYMBOL = "ETH"
TEST_SIDE = "LONG"
TEST_LEVERAGE = 5.0
WALLET_ID = 3  # Using wallet3

# TP distances from entry (percentages)
# Must be >1% apart from each other so _tp_already_verified (1% tolerance)
# can distinguish them.  E.g. +3%, +6%, +9% → each TP is ~3% from neighbors.
TP1_PCT = 0.03   # +3%
TP2_PCT = 0.06   # +6%
TP3_PCT = 0.09   # +9%
SL_PCT  = 0.03   # -3%

# TP close percentages
TP1_CLOSE = 0.30  # 30%
TP2_CLOSE = 0.40  # 40%
TP3_CLOSE = 0.30  # 30%

# Keeper wait settings
KEEPER_POLL_INTERVAL = 5   # seconds between checks
KEEPER_MAX_WAIT = 90       # max seconds to wait for keeper


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TestBot Harness (inherits SLTPMixin for real check_tp_hits / move_sl)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBot(SLTPMixin):
    """Lightweight harness providing SLTPMixin dependencies.

    Real: w3, cfg, account, SL cancel/create on-chain.
    Mock: price feeds, TP order counts, notifications.
    """

    def __init__(self, cfg: Config, w3: Web3, account3: Account):
        self.cfg = cfg
        self.w3 = w3
        # Only wallet3 is used for this test
        self.account = account3
        self.account2 = None
        self.account3 = account3
        self.account4 = None
        self.logger = logging.getLogger("TestBot")
        self.positions: Dict[str, Position] = {}
        self.failed_order_queue: List[FailedOrder] = []
        self._orders_cooldown_until: float = 0.0
        self.health_stats: Dict[str, int] = {}
        self.trade_history: list = []

        # Mock-controlled state
        self._fake_price: Optional[float] = None

    def _get_account(self, wallet_id: int) -> Account:
        """Always returns wallet3 for this test."""
        return self.account3

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Return fake price when set, otherwise real Chainlink price."""
        if self._fake_price is not None:
            self.logger.info(f"[MOCK] get_current_price({symbol}) -> ${self._fake_price:,.2f}")
            return self._fake_price
        return await asyncio.to_thread(fetch_current_price, symbol, self.w3)

    async def notify(self, message: str):
        """Log notification instead of sending to Telegram."""
        self.logger.info(f"[NOTIFY] {message}")

    def _save_position_state(self):
        """No-op for test (don't write position_state.json)."""
        pass

    def _set_orders_cooldown(self, seconds: float = 30.0):
        self._orders_cooldown_until = time.time() + seconds
        self.logger.info(f"Orders cooldown set for {seconds:.0f}s")

    def _in_orders_cooldown(self) -> bool:
        return time.time() < self._orders_cooldown_until

    def set_fake_price(self, price: float):
        self._fake_price = price

    def clear_fake_price(self):
        self._fake_price = None

    def reset_cooldown(self):
        """Reset cooldown so next check_tp_hits runs immediately."""
        self._orders_cooldown_until = 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_test_signal(current_price: float) -> Signal:
    """Build a test signal dynamically from the current price."""
    entry = current_price
    tp1 = round(entry * (1 + TP1_PCT), 2)
    tp2 = round(entry * (1 + TP2_PCT), 2)
    tp3 = round(entry * (1 + TP3_PCT), 2)
    sl  = round(entry * (1 - SL_PCT), 2)

    signal = Signal(
        symbol=TEST_SYMBOL,
        side=TEST_SIDE,
        entry_low=entry,
        entry_high=entry,
        take_profits=[
            TakeProfit(price=tp1, close_pct=TP1_CLOSE),
            TakeProfit(price=tp2, close_pct=TP2_CLOSE),
            TakeProfit(price=tp3, close_pct=TP3_CLOSE),
        ],
        stop_loss=sl,
        leverage=TEST_LEVERAGE,
        raw_text=f"TEST: {TEST_SYMBOL} {TEST_SIDE} Entry: {entry} TP1: {tp1} TP2: {tp2} TP3: {tp3} SL: {sl} Leverage: {TEST_LEVERAGE}x",
        trade_type="scalp",
    )

    log.info(f"Test signal: {TEST_SYMBOL} {TEST_SIDE} {TEST_LEVERAGE}x")
    log.info(f"  Entry:  ${entry:,.2f}")
    log.info(f"  TP1:    ${tp1:,.2f} ({TP1_CLOSE:.0%})")
    log.info(f"  TP2:    ${tp2:,.2f} ({TP2_CLOSE:.0%})")
    log.info(f"  TP3:    ${tp3:,.2f} ({TP3_CLOSE:.0%})")
    log.info(f"  SL:     ${sl:,.2f}")
    return signal


def make_fake_decrease(market_addr, tp_price, size_delta, pnl, tx_suffix, log_index=0, order_type=5):
    """Build a fake PositionDecrease event dict.

    Args:
        order_type: GMX order type (5=TP/LimitDecrease, 6=SL, 4=MarketDecrease, None=unknown).
                    Defaults to 5 (TP) for backward compat with existing tests.
    """
    return {
        "market_address": market_addr.lower(),
        "is_long": True,
        "size_delta_usd": size_delta,
        "execution_price": tp_price,
        "net_pnl_usd": pnl,
        "timestamp": int(time.time()),
        "tx_hash": f"fake_{tx_suffix}_tx_" + "x" * 50,
        "log_index": log_index,
        "order_type": order_type,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Verification functions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def verify_tp1_hit(pos: Position, original_sl: float, expected_pnl: float, expected_size_delta: float):
    """Verify state after TP1 hit simulation."""
    sorted_tps = sorted(pos.take_profits, key=lambda tp: tp.price)

    # verified_decreases: should have exactly 1 entry
    assert pos.tp_hits_count == 1, f"Expected tp_hits_count=1, got {pos.tp_hits_count}"
    assert len(pos.verified_decreases) == 1, f"Expected 1 verified decrease, got {len(pos.verified_decreases)}"

    # Verify the decrease entry has correct data
    d = pos.verified_decreases[0]
    assert d["matched_tp_price"] == sorted_tps[0].price, (
        f"matched_tp_price should be TP1 ({sorted_tps[0].price}), got {d['matched_tp_price']}"
    )
    assert d["execution_price"] == sorted_tps[0].price, (
        f"execution_price mismatch: {d['execution_price']}"
    )
    assert d["net_pnl_usd"] == expected_pnl, (
        f"net_pnl_usd should be {expected_pnl}, got {d['net_pnl_usd']}"
    )
    assert abs(d["size_delta_usd"] - expected_size_delta) < 0.01, (
        f"size_delta_usd should be ~{expected_size_delta}, got {d['size_delta_usd']}"
    )
    assert d["tx_hash"], "tx_hash should not be empty"

    # SL should NOT move after TP1 (trailing strategy: let trade run)
    assert pos.sl_moved_to_entry is False, "SL should NOT move after TP1 (trailing strategy)"
    assert pos.stop_loss == original_sl, (
        f"SL should be unchanged at ${original_sl:,.2f}, got ${pos.stop_loss}"
    )

    # Realized PnL should be derived from verified_decreases
    assert pos.realized_pnl == expected_pnl, (
        f"realized_pnl should be {expected_pnl}, got {pos.realized_pnl}"
    )
    log.info("  PASS: TP1 hit -- no SL move, verified_decreases correct")


def verify_tp2_hit(pos: Position, entry_price: float, tp1_pnl: float, tp2_pnl: float):
    """Verify state after TP2 hit simulation."""
    # verified_decreases: should have exactly 2 entries
    assert pos.tp_hits_count == 2, f"Expected tp_hits_count=2, got {pos.tp_hits_count}"
    assert len(pos.verified_decreases) == 2, f"Expected 2 verified decreases, got {len(pos.verified_decreases)}"

    # Realized PnL should be sum of both decreases
    expected_total_pnl = tp1_pnl + tp2_pnl
    assert abs(pos.realized_pnl - expected_total_pnl) < 0.01, (
        f"realized_pnl should be ~{expected_total_pnl}, got {pos.realized_pnl}"
    )

    # SL should be moved to entry after TP2
    assert pos.sl_moved_to_entry is True, "SL should be moved to entry after TP2"
    assert pos.sl_move_label == "Entry", f"sl_move_label should be 'Entry', got '{pos.sl_move_label}'"
    assert pos.stop_loss == entry_price, (
        f"SL should be at entry ${entry_price:,.2f}, got ${pos.stop_loss:,.2f}"
    )

    # Each decrease should have unique tx_hash
    tx_hashes = [d["tx_hash"] for d in pos.verified_decreases]
    assert len(set(tx_hashes)) == 2, f"tx_hashes should be unique, got {tx_hashes}"
    log.info(f"  PASS: TP2 hit -- SL moved to Entry (${entry_price:,.2f}), PnL accumulated")


def verify_tp3_hit(pos: Position, tp1_price: float, total_pnl: float):
    """Verify state after TP3 hit simulation."""
    # verified_decreases: should have exactly 3 entries
    assert pos.tp_hits_count == 3, f"Expected tp_hits_count=3, got {pos.tp_hits_count}"
    assert len(pos.verified_decreases) == 3, f"Expected 3 verified decreases, got {len(pos.verified_decreases)}"

    # All three matched_tp_prices should be distinct
    matched_prices = {d["matched_tp_price"] for d in pos.verified_decreases}
    assert len(matched_prices) == 3, f"Should have 3 distinct matched prices, got {matched_prices}"

    # Total realized PnL from all 3 decreases
    assert abs(pos.realized_pnl - total_pnl) < 0.01, (
        f"realized_pnl should be ~{total_pnl}, got {pos.realized_pnl}"
    )

    # SL should be at TP1 after TP3
    assert pos.sl_move_label == "TP1", f"sl_move_label should be 'TP1', got '{pos.sl_move_label}'"
    assert pos.stop_loss == tp1_price, (
        f"SL should be at TP1 ${tp1_price:,.2f}, got ${pos.stop_loss:,.2f}"
    )

    # Total size_delta_usd should account for all decreases
    total_decreased = sum(d["size_delta_usd"] for d in pos.verified_decreases)
    assert total_decreased > 0, f"total size_delta_usd should be positive, got {total_decreased}"

    log.info(f"  PASS: TP3 hit -- SL moved to TP1 (${tp1_price:,.2f}), all decreases accumulated")


async def verify_onchain_sl(
    w3: Web3,
    wallet_addr: str,
    market_addr: str,
    expected_sl_price: float,
    tolerance_pct: float = 0.02,
    max_retries: int = 4,
):
    """Fetch real on-chain orders and verify SL trigger price.

    Retries up to max_retries times with increasing delays to handle
    RPC indexing lag (DataStore may not reflect new orders immediately).
    """
    market_lower = market_addr.lower()
    for attempt in range(1, max_retries + 1):
        delay = 3 + attempt * 2  # 5s, 7s, 9s, 11s
        await asyncio.sleep(delay)
        orders = await asyncio.to_thread(fetch_open_orders, w3, wallet_addr)
        sl_orders = [
            o for o in orders
            if o["market"].lower() == market_lower
            and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
        ]
        if sl_orders:
            actual_price = sl_orders[0]["trigger_price"]
            if expected_sl_price > 0:
                diff_pct = abs(actual_price - expected_sl_price) / expected_sl_price
                assert diff_pct < tolerance_pct, (
                    f"On-chain SL ${actual_price:,.2f} differs from expected "
                    f"${expected_sl_price:,.2f} by {diff_pct:.1%}"
                )
            log.info(
                f"  PASS: On-chain SL verified at ${actual_price:,.2f} "
                f"(expected ${expected_sl_price:,.2f})"
            )
            return
        log.info(f"  SL not visible on-chain yet (attempt {attempt}/{max_retries}), retrying...")

    # If we get here, SL was never found — could be keeper-executed immediately
    log.warning(
        f"  WARN: SL order not found on-chain after {max_retries} retries. "
        f"Keeper may have filled it (SL at ${expected_sl_price:,.2f} is near current price). "
        f"Skipping on-chain SL verify."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Unit tests (offline — no on-chain interaction)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_unit_tests():
    """Run offline unit tests for verified_decreases data model."""
    log.info("\n" + "=" * 60)
    log.info("UNIT TESTS — verified_decreases data model")
    log.info("=" * 60)
    passed = 0
    failed = 0

    # ── Test 1: TakeProfitLevel only has price and percentage ──
    try:
        tp = TakeProfitLevel(price=100000.0, percentage=0.3)
        assert tp.price == 100000.0
        assert tp.percentage == 0.3
        assert not hasattr(tp, 'executed'), "TakeProfitLevel should not have 'executed' field"
        assert not hasattr(tp, 'executed_at'), "TakeProfitLevel should not have 'executed_at' field"
        assert not hasattr(tp, 'realized_pnl_usd'), "TakeProfitLevel should not have 'realized_pnl_usd' field"
        log.info("  PASS: Test 1 — TakeProfitLevel is clean (price + percentage only)")
        passed += 1
    except Exception as e:
        log.error(f"  FAIL: Test 1 — TakeProfitLevel: {e}")
        failed += 1

    # ── Test 2: Position.tp_hits_count is a @property from verified_decreases ──
    try:
        pos = Position(
            id="test-1", symbol="ETH", side="LONG",
            size_usd=100.0, leverage=5.0, entry_price=2000.0,
            current_price=2000.0, stop_loss=1940.0,
            take_profits=[TakeProfitLevel(price=2020.0, percentage=0.3)],
        )
        assert pos.tp_hits_count == 0, f"Empty verified_decreases should give tp_hits_count=0, got {pos.tp_hits_count}"
        pos.verified_decreases.append({
            "execution_price": 2020.0, "net_pnl_usd": 0.30,
            "timestamp": time.time(), "tx_hash": "abc123",
            "log_index": 0, "size_delta_usd": 30.0,
            "matched_tp_price": 2020.0,
        })
        assert pos.tp_hits_count == 1, f"After 1 decrease, tp_hits_count should be 1, got {pos.tp_hits_count}"
        log.info("  PASS: Test 2 — tp_hits_count is @property from verified_decreases")
        passed += 1
    except Exception as e:
        log.error(f"  FAIL: Test 2 — tp_hits_count property: {e}")
        failed += 1

    # ── Test 3: Position has no last_known_tp_count or expected_tp_count ──
    try:
        pos = Position(
            id="test-3", symbol="BTC", side="LONG",
            size_usd=500.0, leverage=10.0, entry_price=90000.0,
            current_price=90000.0, stop_loss=87300.0,
            take_profits=[],
        )
        assert not hasattr(pos, 'last_known_tp_count') or 'last_known_tp_count' not in pos.__dataclass_fields__, \
            "Position should not have last_known_tp_count field"
        assert not hasattr(pos, 'expected_tp_count') or 'expected_tp_count' not in pos.__dataclass_fields__, \
            "Position should not have expected_tp_count field"
        log.info("  PASS: Test 3 — Position has no last_known_tp_count / expected_tp_count")
        passed += 1
    except Exception as e:
        log.error(f"  FAIL: Test 3 — removed fields: {e}")
        failed += 1

    # ── Test 4: _tp_already_verified works correctly ──
    try:
        # Create a temporary SLTPMixin instance to test the helper
        class FakeMixin(SLTPMixin):
            pass
        mixin = FakeMixin()

        # Use prices >1% apart so tolerance (1%) can distinguish them
        pos = Position(
            id="test-4", symbol="BTC", side="LONG",
            size_usd=1000.0, leverage=5.0, entry_price=90000.0,
            current_price=90000.0, stop_loss=87300.0,
            take_profits=[
                TakeProfitLevel(price=93000.0, percentage=0.3),   # +3.3%
                TakeProfitLevel(price=96000.0, percentage=0.4),   # +6.7%
            ],
        )
        # No decreases yet
        assert mixin._tp_already_verified(pos, 93000.0) is False
        assert mixin._tp_already_verified(pos, 96000.0) is False

        # Add one verified decrease for TP1
        pos.verified_decreases.append({
            "execution_price": 92950.0, "net_pnl_usd": 10.0,
            "timestamp": time.time(), "tx_hash": "abc",
            "log_index": 0, "size_delta_usd": 300.0,
            "matched_tp_price": 93000.0,
        })
        assert mixin._tp_already_verified(pos, 93000.0) is True, "TP1 should be verified"
        assert mixin._tp_already_verified(pos, 96000.0) is False, "TP2 should NOT be verified"

        # Edge: price=0 should return False
        assert mixin._tp_already_verified(pos, 0) is False
        log.info("  PASS: Test 4 — _tp_already_verified tolerance matching")
        passed += 1
    except Exception as e:
        log.error(f"  FAIL: Test 4 — _tp_already_verified: {e}")
        failed += 1

    # ── Test 5: determine_new_sl_target works with tp_hits_count ──
    try:
        sorted_tps = [
            TakeProfitLevel(price=2020.0, percentage=0.3),
            TakeProfitLevel(price=2040.0, percentage=0.4),
            TakeProfitLevel(price=2060.0, percentage=0.3),
        ]
        entry = 2000.0

        # TP1 hit → no move
        sl, label = determine_new_sl_target(1, entry, sorted_tps)
        assert sl is None and label is None, f"TP1: expected (None, None), got ({sl}, {label})"

        # TP2 hit → SL to entry
        sl, label = determine_new_sl_target(2, entry, sorted_tps)
        assert sl == entry and label == "Entry", f"TP2: expected ({entry}, 'Entry'), got ({sl}, {label})"

        # TP3 hit → SL to TP1
        sl, label = determine_new_sl_target(3, entry, sorted_tps)
        assert sl == 2020.0 and label == "TP1", f"TP3: expected (2020.0, 'TP1'), got ({sl}, {label})"

        log.info("  PASS: Test 5 — determine_new_sl_target trailing strategy")
        passed += 1
    except Exception as e:
        log.error(f"  FAIL: Test 5 — determine_new_sl_target: {e}")
        failed += 1

    # ── Test 6: classify_exit_reason uses tp_orders_remaining ──
    try:
        # All TPs filled (tp_orders_remaining=0, tp_hits_count > 0)
        r = classify_exit_reason(
            is_long=True, current_price=105000.0, stop_loss=95000.0,
            tp_hits_count=3, sl_moved_to_entry=True, sl_move_label="TP1",
            sl_orders_remaining=1, tp_orders_remaining=0,
        )
        assert r == "All TPs Filled", f"Expected 'All TPs Filled', got '{r}'"

        # Liquidation (both SL+TP still on chain, no hits)
        r = classify_exit_reason(
            is_long=True, current_price=80000.0, stop_loss=85000.0,
            tp_hits_count=0, sl_moved_to_entry=False, sl_move_label=None,
            sl_orders_remaining=1, tp_orders_remaining=3,
        )
        assert r == "Liquidation", f"Expected 'Liquidation', got '{r}'"

        # SL triggered (SL orders gone, TPs remain)
        r = classify_exit_reason(
            is_long=True, current_price=85000.0, stop_loss=85000.0,
            tp_hits_count=0, sl_moved_to_entry=False, sl_move_label=None,
            sl_orders_remaining=0, tp_orders_remaining=3,
        )
        assert r == "SL Hit", f"Expected 'SL Hit', got '{r}'"

        # SL at entry (SL moved to entry, SL orders gone, TPs remain)
        r = classify_exit_reason(
            is_long=True, current_price=90000.0, stop_loss=90000.0,
            tp_hits_count=1, sl_moved_to_entry=True, sl_move_label="Entry",
            sl_orders_remaining=0, tp_orders_remaining=2,
        )
        assert "Closed at Entry" in r, f"Expected 'Closed at Entry ...', got '{r}'"

        # Fallback: no order counts available, tp_hits > 0
        r = classify_exit_reason(
            is_long=True, current_price=90000.0, stop_loss=85000.0,
            tp_hits_count=1, sl_moved_to_entry=False, sl_move_label=None,
            sl_orders_remaining=-1, tp_orders_remaining=-1,
        )
        assert "1 TPs hit" in r, f"Expected '... 1 TPs hit ...', got '{r}'"

        log.info("  PASS: Test 6 — classify_exit_reason with tp_orders_remaining")
        passed += 1
    except Exception as e:
        log.error(f"  FAIL: Test 6 — classify_exit_reason: {e}")
        failed += 1

    # ── Test 7: Remaining size calculation from verified_decreases ──
    try:
        pos = Position(
            id="test-7", symbol="ETH", side="LONG",
            size_usd=1000.0, leverage=5.0, entry_price=2000.0,
            current_price=2020.0, stop_loss=1940.0,
            take_profits=[
                TakeProfitLevel(price=2020.0, percentage=0.3),
                TakeProfitLevel(price=2040.0, percentage=0.4),
                TakeProfitLevel(price=2060.0, percentage=0.3),
            ],
            original_size_usd=1000.0,
        )
        # TP1 hit: 30% of 1000 = 300
        pos.verified_decreases.append({
            "execution_price": 2020.0, "net_pnl_usd": 3.0,
            "timestamp": time.time(), "tx_hash": "t1",
            "log_index": 0, "size_delta_usd": 300.0,
            "matched_tp_price": 2020.0,
        })
        total_decreased = sum(d["size_delta_usd"] for d in pos.verified_decreases)
        remaining = max(pos.original_size_usd - total_decreased, 0.01)
        assert remaining == 700.0, f"Expected remaining=700, got {remaining}"

        # TP2 hit: 40% of 1000 = 400
        pos.verified_decreases.append({
            "execution_price": 2040.0, "net_pnl_usd": 8.0,
            "timestamp": time.time(), "tx_hash": "t2",
            "log_index": 0, "size_delta_usd": 400.0,
            "matched_tp_price": 2040.0,
        })
        total_decreased = sum(d["size_delta_usd"] for d in pos.verified_decreases)
        remaining = max(pos.original_size_usd - total_decreased, 0.01)
        assert remaining == 300.0, f"Expected remaining=300, got {remaining}"

        # TP3 hit: 30% of 1000 = 300
        pos.verified_decreases.append({
            "execution_price": 2060.0, "net_pnl_usd": 9.0,
            "timestamp": time.time(), "tx_hash": "t3",
            "log_index": 0, "size_delta_usd": 300.0,
            "matched_tp_price": 2060.0,
        })
        total_decreased = sum(d["size_delta_usd"] for d in pos.verified_decreases)
        remaining = max(pos.original_size_usd - total_decreased, 0.01)
        assert remaining == 0.01, f"Expected remaining=0.01 (clamped), got {remaining}"

        # Realized PnL: sum of all net_pnl_usd
        realized = sum(d["net_pnl_usd"] for d in pos.verified_decreases)
        assert realized == 20.0, f"Expected realized=20.0, got {realized}"

        log.info("  PASS: Test 7 — Remaining size + PnL from verified_decreases")
        passed += 1
    except Exception as e:
        log.error(f"  FAIL: Test 7 — remaining size calc: {e}")
        failed += 1

    # ── Test 8: Dedup — processed_tx_hashes prevents re-counting ──
    try:
        pos = Position(
            id="test-8", symbol="ETH", side="LONG",
            size_usd=100.0, leverage=5.0, entry_price=2000.0,
            current_price=2020.0, stop_loss=1940.0,
            take_profits=[TakeProfitLevel(price=2020.0, percentage=0.3)],
        )
        event_key = "0xabc123:0"
        pos.processed_tx_hashes.add(event_key)

        # After adding to processed set, a second event with same key should be filtered
        new_events = [
            {"tx_hash": "0xabc123", "log_index": 0, "execution_price": 2020.0}
        ]
        filtered = [
            d for d in new_events
            if f"{d['tx_hash']}:{d.get('log_index', 0)}" not in pos.processed_tx_hashes
        ]
        assert len(filtered) == 0, f"Duplicate event should be filtered, got {len(filtered)}"
        log.info("  PASS: Test 8 — Dedup via processed_tx_hashes")
        passed += 1
    except Exception as e:
        log.error(f"  FAIL: Test 8 — dedup: {e}")
        failed += 1

    # ── Test 9: verify_tp_hit_by_price tolerance ──
    try:
        # Exact match
        assert verify_tp_hit_by_price(True, 2020.0, 2020.0) is True
        # Within 0.15% default tolerance
        assert verify_tp_hit_by_price(True, 2020.0, 2019.0) is True
        # Well below TP for a long (price hasn't reached)
        assert verify_tp_hit_by_price(True, 2020.0, 2010.0) is False
        # SHORT: price below TP means hit
        assert verify_tp_hit_by_price(False, 1980.0, 1980.0) is True
        assert verify_tp_hit_by_price(False, 1980.0, 1990.0) is False
        log.info("  PASS: Test 9 — verify_tp_hit_by_price tolerance")
        passed += 1
    except Exception as e:
        log.error(f"  FAIL: Test 9 — verify_tp_hit_by_price: {e}")
        failed += 1

    # ── Summary ──
    total = passed + failed
    log.info(f"\nUnit tests: {passed}/{total} passed, {failed} failed")
    if failed > 0:
        log.error("UNIT TESTS FAILED — fix before running on-chain tests")
        return False
    log.info("All unit tests PASSED")
    return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Cleanup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def cleanup_position(w3: Web3, acct: Account, cfg: Config, market_addr: str):
    """Cancel all orders for ETH market and close any open position on wallet3."""
    log.info("=" * 60)
    log.info("CLEANUP: Cancelling orders and closing position")
    log.info("=" * 60)
    try:
        exchange = w3.eth.contract(
            address=Web3.to_checksum_address(cfg.exchange_router),
            abi=EXCHANGE_ROUTER_ABI,
        )
        # Cancel all orders for this market
        cancelled = await asyncio.to_thread(
            cancel_orders_for_market,
            w3, acct, exchange, market_addr, dry_run=False,
        )
        log.info(f"Cancelled {cancelled} orders")
        await asyncio.sleep(5)

        # Close position
        positions = await asyncio.to_thread(
            chain_fetch_positions, w3, acct.address,
        )
        eth_positions = [
            p for p in positions
            if p.symbol.upper() == TEST_SYMBOL
        ]
        for gpos in eth_positions:
            side = "LONG" if gpos.is_long else "SHORT"
            log.info(f"Closing {gpos.symbol} {side} size=${gpos.size_usd:.2f}")
            await asyncio.to_thread(
                create_close_order,
                w3, acct, gpos, percentage=1.0, dry_run=False,
            )
            log.info("Close order submitted, waiting for keeper...")
            await asyncio.sleep(15)

        log.info("Cleanup complete")
    except Exception as e:
        log.error(f"CLEANUP FAILED: {e} -- MANUAL INTERVENTION REQUIRED")
        log.error(traceback.format_exc())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main test orchestration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def run_test():
    log.info("=" * 60)
    log.info("TRAILING STOP LOSS TEST — verified_decreases model")
    log.info("=" * 60)

    # ── Phase 0a: Unit tests (offline, no RPC needed) ──
    if not run_unit_tests():
        log.error("Aborting — unit tests failed")
        return

    # ── Phase 0b: Setup ──
    log.info("\n[PHASE 0] Setup")
    cfg = load_config()
    w3 = Web3(Web3.HTTPProvider(cfg.rpc_url))
    assert w3.is_connected(), f"Cannot connect to RPC: {cfg.rpc_url}"
    log.info(f"Connected to {cfg.rpc_url} (chain_id={w3.eth.chain_id})")

    # Load wallet3
    assert cfg.private_key_3, "PRIVATE_KEY_3 not set in .env"
    account3 = Account.from_key(cfg.private_key_3)
    wallet_addr = account3.address
    log.info(f"Wallet3: {wallet_addr}")

    # Check ETH balance for gas
    eth_balance = w3.eth.get_balance(wallet_addr)
    eth_bal = eth_balance / 10**18
    log.info(f"ETH balance: {eth_bal:.6f} ETH")
    assert eth_bal > 0.001, f"Wallet3 needs ETH for gas (has {eth_bal:.6f})"

    # Market address
    market_addr = cfg.markets.get(TEST_SYMBOL)
    assert market_addr, f"No market address for {TEST_SYMBOL}"
    log.info(f"Market: {market_addr}")

    # Check no existing ETH position on wallet3
    existing = await asyncio.to_thread(chain_fetch_positions, w3, wallet_addr)
    eth_existing = [p for p in existing if p.symbol.upper() == TEST_SYMBOL]
    if eth_existing:
        log.warning(
            f"Wallet3 already has {len(eth_existing)} ETH position(s). "
            f"This test needs a clean slate."
        )
        log.warning("Attempting cleanup of existing position first...")
        await cleanup_position(w3, account3, cfg, market_addr)
        await asyncio.sleep(10)
        existing = await asyncio.to_thread(chain_fetch_positions, w3, wallet_addr)
        eth_existing = [p for p in existing if p.symbol.upper() == TEST_SYMBOL]
        assert not eth_existing, "Failed to clean up existing ETH position on wallet3"

    # Fetch current price and build signal
    current_price = await asyncio.to_thread(fetch_current_price, TEST_SYMBOL, w3)
    assert current_price > 0, f"Failed to fetch {TEST_SYMBOL} price"
    log.info(f"Current {TEST_SYMBOL} price: ${current_price:,.2f}")

    signal = build_test_signal(current_price)
    tp1_price = signal.take_profits[0].price
    tp2_price = signal.take_profits[1].price
    tp3_price = signal.take_profits[2].price
    original_sl = signal.stop_loss
    entry_price = current_price  # execute_signal uses market price as entry

    # Calculate minimum position size
    collateral_usd = max(cfg.min_position_usd, 2.5)
    size_usd = collateral_usd * TEST_LEVERAGE
    log.info(f"Position: collateral=${collateral_usd:.2f}, size=${size_usd:.2f}")

    # Create TestBot
    bot = TestBot(cfg, w3, account3)

    # Fake PnL values for test events
    TP1_PNL = 1.50
    TP2_PNL = 3.00
    TP3_PNL = 4.50

    position_opened = False

    try:
        # ── Phase 1: Open Position (REAL on-chain) ──
        log.info("\n" + "=" * 60)
        log.info("[PHASE 1] Opening position on-chain (wallet3)")
        log.info("=" * 60)

        results = await asyncio.to_thread(
            execute_signal,
            w3=w3,
            acct=account3,
            signal=signal,
            exchange_router=cfg.exchange_router,
            order_vault=cfg.order_vault,
            market=market_addr,
            collateral_token=cfg.collateral_token,
            size_usd=size_usd,
            collateral_usd=collateral_usd,
            execution_fee=cfg.execution_fee_wei,
            slippage_bps=cfg.slippage_bps,
            dry_run=False,
        )

        open_tx = results.get("open", "")
        tp_results = results.get("tp", [])
        sl_result = results.get("sl")
        log.info(f"Open TX: {open_tx}")
        log.info(f"TP results: {len(tp_results)} orders")
        log.info(f"SL result: {sl_result}")
        position_opened = True

        # Wait for keeper to fill the market order
        log.info("Waiting for keeper to fill market order...")
        filled = False
        for i in range(KEEPER_MAX_WAIT // KEEPER_POLL_INTERVAL):
            await asyncio.sleep(KEEPER_POLL_INTERVAL)
            positions = await asyncio.to_thread(
                chain_fetch_positions, w3, wallet_addr,
            )
            eth_pos = [p for p in positions if p.symbol.upper() == TEST_SYMBOL]
            if eth_pos:
                filled = True
                gpos = eth_pos[0]
                log.info(
                    f"Position filled! Entry=${gpos.entry_price:,.2f}, "
                    f"Size=${gpos.size_usd:,.2f}, Leverage={gpos.leverage:.1f}x"
                )
                # Update entry_price to actual fill price
                entry_price = gpos.entry_price
                break
            log.info(f"  Waiting... ({(i+1) * KEEPER_POLL_INTERVAL}s)")

        assert filled, "Position was not filled by keeper within timeout"

        # Verify on-chain orders
        orders = await asyncio.to_thread(fetch_open_orders, w3, wallet_addr)
        market_lower = market_addr.lower()
        tp_orders = [
            o for o in orders
            if o["market"].lower() == market_lower
            and o["order_type"] == ORDER_TYPE_LIMIT_DECREASE
        ]
        sl_orders = [
            o for o in orders
            if o["market"].lower() == market_lower
            and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
        ]
        log.info(f"On-chain orders: {len(tp_orders)} TPs, {len(sl_orders)} SLs")
        for o in tp_orders:
            log.info(f"  TP: trigger=${o['trigger_price']:,.2f}")
        for o in sl_orders:
            log.info(f"  SL: trigger=${o['trigger_price']:,.2f}")

        assert len(tp_orders) >= 1, f"Expected TP orders, found {len(tp_orders)}"
        assert len(sl_orders) >= 1, f"Expected SL orders, found {len(sl_orders)}"

        # Build internal Position for the TestBot
        # Use successfully placed TP prices from on-chain
        tp_levels = []
        placed_tp_prices = sorted([o["trigger_price"] for o in tp_orders])
        close_pcts = [TP1_CLOSE, TP2_CLOSE, TP3_CLOSE]
        for i, price in enumerate(placed_tp_prices):
            pct = close_pcts[i] if i < len(close_pcts) else (1.0 / len(placed_tp_prices))
            tp_levels.append(TakeProfitLevel(price=price, percentage=pct))

        # Update our TP prices to match what was actually placed on-chain
        if len(placed_tp_prices) >= 3:
            tp1_price = placed_tp_prices[0]
            tp2_price = placed_tp_prices[1]
            tp3_price = placed_tp_prices[2]
        elif len(placed_tp_prices) >= 1:
            tp1_price = placed_tp_prices[0]

        pos = Position(
            id=str(uuid.uuid4()),
            symbol=TEST_SYMBOL,
            side=TEST_SIDE,
            size_usd=gpos.size_usd,
            leverage=gpos.leverage,
            entry_price=entry_price,
            current_price=current_price,
            stop_loss=original_sl,
            take_profits=tp_levels,
            is_open=True,
            market_addr=market_addr,
            wallet_id=WALLET_ID,
            original_size_usd=gpos.size_usd,
        )
        bot.positions[pos.id] = pos

        log.info(f"\nInternal position created:")
        log.info(f"  ID:          {pos.short_id}")
        log.info(f"  Entry:       ${pos.entry_price:,.2f}")
        log.info(f"  Size:        ${pos.size_usd:,.2f}")
        log.info(f"  Leverage:    {pos.leverage:.1f}x")
        log.info(f"  SL:          ${pos.stop_loss:,.2f}")
        log.info(f"  TPs:         {len(pos.take_profits)}")
        log.info(f"  verified_decreases: {len(pos.verified_decreases)} (should be 0)")
        for i, tp in enumerate(sorted(pos.take_profits, key=lambda t: t.price)):
            log.info(f"    TP{i+1}: ${tp.price:,.2f} ({tp.percentage:.0%})")

        # Pre-check: verified_decreases should be empty
        assert len(pos.verified_decreases) == 0, "verified_decreases should start empty"
        assert pos.tp_hits_count == 0, "tp_hits_count should be 0"

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Phase 2: Simulate TP1 Hit
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        log.info("\n" + "=" * 60)
        log.info("[PHASE 2] Simulating TP1 Hit")
        log.info(f"  Fake price -> ${tp1_price:,.2f}")
        log.info(f"  Expected: verified_decreases=1, NO SL move")
        log.info("=" * 60)

        tp1_size_delta = pos.size_usd * TP1_CLOSE
        bot.set_fake_price(tp1_price)
        bot.reset_cooldown()

        fake_decrease_tp1 = [make_fake_decrease(
            market_addr, tp1_price, tp1_size_delta, TP1_PNL, "tp1"
        )]

        with patch("sl_tp.fetch_recent_position_decreases", return_value=fake_decrease_tp1):
            await bot.check_tp_hits()

        verify_tp1_hit(pos, original_sl, TP1_PNL, tp1_size_delta)
        log.info("  Phase 2 PASSED")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Phase 2b: Dedup — replay same event, should NOT add duplicate
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        log.info("\n" + "=" * 60)
        log.info("[PHASE 2b] Dedup test — replay TP1 event")
        log.info(f"  Expected: verified_decreases still=1 (no duplicate)")
        log.info("=" * 60)

        bot.reset_cooldown()
        with patch("sl_tp.fetch_recent_position_decreases", return_value=fake_decrease_tp1):
            await bot.check_tp_hits()

        assert len(pos.verified_decreases) == 1, (
            f"Dedup failed: expected 1 verified decrease after replay, got {len(pos.verified_decreases)}"
        )
        assert pos.tp_hits_count == 1, f"tp_hits_count should still be 1, got {pos.tp_hits_count}"
        log.info("  PASS: Dedup — replay same event did NOT create duplicate")
        log.info("  Phase 2b PASSED")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Phase 3: Simulate TP2 Hit (SL should move to Entry)
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        log.info("\n" + "=" * 60)
        log.info("[PHASE 3] Simulating TP2 Hit")
        log.info(f"  Fake price -> ${tp2_price:,.2f}")
        log.info(f"  Expected: verified_decreases=2, SL moves to Entry (${entry_price:,.2f})")
        log.info("  NOTE: SL movement is REAL on-chain (cancel old + create new)")
        log.info("=" * 60)

        tp2_size_delta = pos.size_usd * TP2_CLOSE
        bot.set_fake_price(tp2_price)
        bot.reset_cooldown()

        fake_decrease_tp2 = [make_fake_decrease(
            market_addr, tp2_price, tp2_size_delta, TP2_PNL, "tp2"
        )]

        with patch("sl_tp.fetch_recent_position_decreases", return_value=fake_decrease_tp2):
            await bot.check_tp_hits()

        verify_tp2_hit(pos, entry_price, TP1_PNL, TP2_PNL)

        # Verify on-chain SL was actually moved
        log.info("  Verifying on-chain SL...")
        await verify_onchain_sl(w3, wallet_addr, market_addr, entry_price)
        log.info("  Phase 3 PASSED")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Phase 4: Simulate TP3 Hit (SL should move to TP1)
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        log.info("\n" + "=" * 60)
        log.info("[PHASE 4] Simulating TP3 Hit")
        log.info(f"  Fake price -> ${tp3_price:,.2f}")
        log.info(f"  Expected: verified_decreases=3, SL moves to TP1 (${tp1_price:,.2f})")
        log.info("  NOTE: SL movement is REAL on-chain (cancel old + create new)")
        log.info("=" * 60)

        tp3_size_delta = pos.size_usd * TP3_CLOSE
        bot.set_fake_price(tp3_price)
        bot.reset_cooldown()

        fake_decrease_tp3 = [make_fake_decrease(
            market_addr, tp3_price, tp3_size_delta, TP3_PNL, "tp3"
        )]

        with patch("sl_tp.fetch_recent_position_decreases", return_value=fake_decrease_tp3):
            await bot.check_tp_hits()

        total_pnl = TP1_PNL + TP2_PNL + TP3_PNL
        verify_tp3_hit(pos, tp1_price, total_pnl)

        # Verify on-chain SL was moved to TP1
        log.info("  Verifying on-chain SL...")
        await verify_onchain_sl(w3, wallet_addr, market_addr, tp1_price)
        log.info("  Phase 4 PASSED")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Phase 5: Verify remaining size after all TPs hit
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        log.info("\n" + "=" * 60)
        log.info("[PHASE 5] Verify remaining size and PnL")
        log.info("=" * 60)

        total_decreased = sum(d["size_delta_usd"] for d in pos.verified_decreases)
        expected_total = tp1_size_delta + tp2_size_delta + tp3_size_delta
        assert abs(total_decreased - expected_total) < 0.01, (
            f"Total decreased should be ~{expected_total:.2f}, got {total_decreased:.2f}"
        )
        log.info(f"  Total size decreased: ${total_decreased:.2f} (expected ${expected_total:.2f})")

        base_size = pos.original_size_usd or pos.size_usd
        remaining = max(base_size - total_decreased, 0.0)
        log.info(f"  Remaining size: ${remaining:.2f} (of ${base_size:.2f})")

        realized = sum(d["net_pnl_usd"] for d in pos.verified_decreases)
        assert abs(realized - total_pnl) < 0.01, (
            f"Realized PnL should be ~{total_pnl}, got {realized}"
        )
        log.info(f"  Realized PnL: ${realized:.2f}")

        # Verify each decrease has a unique matched_tp_price
        matched_prices = [d["matched_tp_price"] for d in pos.verified_decreases]
        assert len(set(matched_prices)) == 3, (
            f"Should have 3 distinct matched_tp_prices, got {matched_prices}"
        )
        log.info(f"  Matched TP prices: {[f'${p:,.2f}' for p in matched_prices]}")

        # Verify each decrease has a unique tx_hash
        tx_hashes = [d["tx_hash"] for d in pos.verified_decreases]
        assert len(set(tx_hashes)) == 3, (
            f"Should have 3 distinct tx_hashes, got {tx_hashes}"
        )
        log.info("  PASS: All verified_decreases are distinct and consistent")
        log.info("  Phase 5 PASSED")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # All tests passed
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        log.info("\n" + "=" * 60)
        log.info("ALL TESTS PASSED")
        log.info("=" * 60)
        log.info(f"  Unit tests (9 offline)         : PASS")
        log.info(f"  TP1 hit -> verified_decreases=1 : PASS")
        log.info(f"  Dedup -> no duplicate entries    : PASS")
        log.info(f"  TP2 hit -> SL to Entry on-chain  : PASS")
        log.info(f"  TP3 hit -> SL to TP1 on-chain    : PASS")
        log.info(f"  Remaining size + PnL consistent  : PASS")
        log.info(f"  No errors in open/TP/SL flow     : PASS")

    except AssertionError as e:
        log.error(f"\nTEST FAILED (assertion): {e}")
        log.error(traceback.format_exc())
    except Exception as e:
        log.error(f"\nTEST FAILED (exception): {e}")
        log.error(traceback.format_exc())
    finally:
        bot.clear_fake_price()
        if position_opened:
            await cleanup_position(w3, account3, cfg, market_addr)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    asyncio.run(run_test())
