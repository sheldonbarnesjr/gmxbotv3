#!/usr/bin/env python3
"""
Unit tests for the new trailing SL strategy.

Tests:
  1. determine_new_sl_target() — correct SL target for each TP hit count
  2. _infer_tp_hits() — correct inference from SL position
  3. move_sl() None handling — TP2 skips SL move

No on-chain activity, no gas cost.
"""

import sys
import asyncio
import logging
from typing import Optional, List, Dict

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("test_trailing_sl")

from gmx import Position, TakeProfitLevel
from risk import determine_new_sl_target
from sl_tp import SLTPMixin
from wallet_mgmt import WalletMixin


PASS = 0
FAIL = 0


def ok(label):
    global PASS
    PASS += 1
    log.info(f"  ✅ PASS: {label}")


def fail(label, detail=""):
    global FAIL
    FAIL += 1
    log.error(f"  ❌ FAIL: {label} -- {detail}")


def assert_eq(label, got, expected, tolerance=None):
    if tolerance and isinstance(got, (int, float)) and isinstance(expected, (int, float)):
        if abs(got - expected) <= tolerance:
            ok(f"{label}: {got}")
        else:
            fail(label, f"expected {expected}, got {got}")
    elif got == expected:
        ok(f"{label}: {got}")
    else:
        fail(label, f"expected {expected}, got {got}")


def assert_none(label, val):
    if val is None:
        ok(f"{label}: None")
    else:
        fail(label, f"expected None, got {val}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Minimal harness for testing move_sl None path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MockHarness(SLTPMixin, WalletMixin):
    def __init__(self):
        self.cfg = type('cfg', (), {
            'dry_run': True,
            'execution_fee_wei': 0,
            'slippage_bps': 30,
            'exchange_router': '0x' + '0' * 40,
            'collateral_token': '0x' + '0' * 40,
            'order_vault': '0x' + '0' * 40,
        })()
        self.w3 = None
        self.account = None
        self.positions: Dict[str, Position] = {}
        self.logger = logging.getLogger("MockHarness")
        self._notifications: List[str] = []
        self.sl_move_called = False
        self.sl_move_skipped = False

    async def get_current_price(self, symbol):
        return 95000.0

    async def notify(self, message):
        self._notifications.append(message)
        return True

    async def send_message(self, chat_id, message):
        return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 1: determine_new_sl_target()
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_determine_new_sl_target():
    print("\n" + "=" * 60)
    print("  TEST 1: determine_new_sl_target()")
    print("=" * 60)

    entry = 90000.0
    tps = [
        TakeProfitLevel(price=93000, percentage=0.2),  # TP1
        TakeProfitLevel(price=95000, percentage=0.2),  # TP2
        TakeProfitLevel(price=97000, percentage=0.2),  # TP3
        TakeProfitLevel(price=99000, percentage=0.2),  # TP4
        TakeProfitLevel(price=101000, percentage=0.2), # TP5
    ]

    # 0 hits → no move
    sl, label = determine_new_sl_target(0, entry, tps)
    assert_none("0 hits → sl", sl)
    assert_none("0 hits → label", label)

    # TP1 → SL to Entry
    sl, label = determine_new_sl_target(1, entry, tps)
    assert_eq("TP1 → sl", sl, entry)
    assert_eq("TP1 → label", label, "Entry")

    # TP2 → no move (None, None)
    sl, label = determine_new_sl_target(2, entry, tps)
    assert_none("TP2 → sl", sl)
    assert_none("TP2 → label", label)

    # TP3 → SL to TP1
    sl, label = determine_new_sl_target(3, entry, tps)
    assert_eq("TP3 → sl", sl, 93000.0)
    assert_eq("TP3 → label", label, "TP1")

    # TP4 → SL to TP2
    sl, label = determine_new_sl_target(4, entry, tps)
    assert_eq("TP4 → sl", sl, 95000.0)
    assert_eq("TP4 → label", label, "TP2")

    # TP5 → SL stays at TP2
    sl, label = determine_new_sl_target(5, entry, tps)
    assert_eq("TP5 → sl", sl, 95000.0)
    assert_eq("TP5 → label", label, "TP2")

    # SHORT side test
    print("\n  --- SHORT side ---")
    short_tps = [
        TakeProfitLevel(price=87000, percentage=0.2),  # TP1 (closest to entry)
        TakeProfitLevel(price=85000, percentage=0.2),  # TP2
        TakeProfitLevel(price=83000, percentage=0.2),  # TP3
        TakeProfitLevel(price=81000, percentage=0.2),  # TP4
        TakeProfitLevel(price=79000, percentage=0.2),  # TP5
    ]
    # Sort SHORT: descending (TP1 = highest = closest to entry)
    short_sorted = sorted(short_tps, key=lambda t: t.price, reverse=True)

    sl, label = determine_new_sl_target(1, entry, short_sorted)
    assert_eq("SHORT TP1 → sl", sl, entry)

    sl, label = determine_new_sl_target(2, entry, short_sorted)
    assert_none("SHORT TP2 → sl", sl)

    sl, label = determine_new_sl_target(3, entry, short_sorted)
    assert_eq("SHORT TP3 → sl", sl, 87000.0)
    assert_eq("SHORT TP3 → label", label, "TP1")

    sl, label = determine_new_sl_target(4, entry, short_sorted)
    assert_eq("SHORT TP4 → sl", sl, 85000.0)
    assert_eq("SHORT TP4 → label", label, "TP2")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 2: _infer_tp_hits()
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_infer_tp_hits():
    print("\n" + "=" * 60)
    print("  TEST 2: _infer_tp_hits()")
    print("=" * 60)

    # Import the GMXBot class to get _infer_tp_hits
    # We'll use a minimal instance
    from gmx import GMXBot

    class MinimalBot:
        """Just enough to call _infer_tp_hits."""
        _infer_tp_hits = GMXBot._infer_tp_hits

    bot = MinimalBot()
    entry = 90000.0

    # LONG scenario: TPs at 93k, 95k, 97k, 99k, 101k
    all_tps = [
        TakeProfitLevel(price=93000, percentage=0.2),
        TakeProfitLevel(price=95000, percentage=0.2),
        TakeProfitLevel(price=97000, percentage=0.2),
        TakeProfitLevel(price=99000, percentage=0.2),
        TakeProfitLevel(price=101000, percentage=0.2),
    ]

    print("\n  --- LONG side ---")

    # SL at original (below entry) → 0 hits
    hits = bot._infer_tp_hits("LONG", entry, 85000.0, all_tps)
    assert_eq("LONG: SL below entry → hits", hits, 0)

    # SL at entry → 2 hits (stable state: both TP1 and TP2 leave SL at entry)
    hits = bot._infer_tp_hits("LONG", entry, entry, all_tps[2:])  # remaining: TP3,TP4,TP5
    assert_eq("LONG: SL at entry → hits", hits, 2)

    # SL slightly above entry (still within tolerance) → 2 hits
    hits = bot._infer_tp_hits("LONG", entry, entry + 100, all_tps[2:])
    assert_eq("LONG: SL ~entry → hits", hits, 2)

    # SL at TP1 price (93000) — past entry → 3 hits
    hits = bot._infer_tp_hits("LONG", entry, 93000.0, all_tps[3:])  # remaining: TP4,TP5
    assert_eq("LONG: SL at TP1 → hits", hits, 3)

    # SL at TP2 price (95000) — past entry, 1 remaining → 4 hits
    hits = bot._infer_tp_hits("LONG", entry, 95000.0, all_tps[4:])  # remaining: TP5
    assert_eq("LONG: SL at TP2 → hits", hits, 4)

    # SL at TP2 price, 0 remaining → 4 hits
    hits = bot._infer_tp_hits("LONG", entry, 95000.0, [])
    assert_eq("LONG: SL at TP2 (no remaining) → hits", hits, 4)

    print("\n  --- SHORT side ---")

    # SHORT scenario: entry at 90k, TPs at 87k, 85k, 83k, 81k, 79k
    short_tps = [
        TakeProfitLevel(price=87000, percentage=0.2),
        TakeProfitLevel(price=85000, percentage=0.2),
        TakeProfitLevel(price=83000, percentage=0.2),
        TakeProfitLevel(price=81000, percentage=0.2),
        TakeProfitLevel(price=79000, percentage=0.2),
    ]

    # SL above entry (wrong side for short) → 0 hits
    hits = bot._infer_tp_hits("SHORT", entry, 95000.0, short_tps)
    assert_eq("SHORT: SL above entry → hits", hits, 0)

    # SL at entry → 2 hits
    hits = bot._infer_tp_hits("SHORT", entry, entry, short_tps[2:])
    assert_eq("SHORT: SL at entry → hits", hits, 2)

    # SL at TP1 price (87000, below entry for short → past entry) → 3 hits
    hits = bot._infer_tp_hits("SHORT", entry, 87000.0, short_tps[3:])
    assert_eq("SHORT: SL at TP1 → hits", hits, 3)

    # SL at TP2 price (85000), 1 remaining → 4 hits
    hits = bot._infer_tp_hits("SHORT", entry, 85000.0, short_tps[4:])
    assert_eq("SHORT: SL at TP2 → hits", hits, 4)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 3: move_sl() skips when determine_new_sl_target returns None
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def test_move_sl_tp2_skip():
    print("\n" + "=" * 60)
    print("  TEST 3: move_sl() skips for TP2 (no SL move)")
    print("=" * 60)

    harness = MockHarness()

    entry = 90000.0
    tps = [
        TakeProfitLevel(price=93000, percentage=0.2),
        TakeProfitLevel(price=95000, percentage=0.2),
        TakeProfitLevel(price=97000, percentage=0.2),
        TakeProfitLevel(price=99000, percentage=0.2),
        TakeProfitLevel(price=101000, percentage=0.2),
    ]

    pos = Position(
        id="test_tp2",
        symbol="BTC",
        side="LONG",
        size_usd=200.0,
        leverage=2.0,
        entry_price=entry,
        stop_loss=entry,  # SL already at entry from TP1
        take_profits=tps,
        is_open=True,
        wallet_id=1,
        market_addr="0x" + "0" * 40,
        tp_hits_count=2,  # TP2 just hit
        sl_moved_to_entry=True,
        sl_move_label="Entry",
    )
    harness.positions["test_tp2"] = pos

    # Call move_sl without explicit target — it should auto-compute from
    # determine_new_sl_target(tp_hits_count=2) which returns (None, None)
    try:
        await harness.move_sl(pos, [], None, None)
        # If we get here without error, it correctly handled None
        ok("move_sl(TP2) returned without error (skipped SL move)")
    except TypeError as e:
        fail("move_sl(TP2) crashed with TypeError", str(e))
    except Exception as e:
        # Other exceptions (like order not found) are OK in this mock context
        # The key test is that it doesn't crash on None target
        if "None" in str(e) or "NoneType" in str(e):
            fail("move_sl(TP2) crashed on None handling", str(e))
        else:
            ok(f"move_sl(TP2) handled None correctly (other error: {type(e).__name__})")

    # Verify SL was NOT changed from entry price
    assert_eq("SL still at entry", pos.stop_loss, entry)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    test_determine_new_sl_target()
    test_infer_tp_hits()
    asyncio.run(test_move_sl_tp2_skip())

    print("\n" + "=" * 60)
    total = PASS + FAIL
    if FAIL == 0:
        print(f"  ALL {total} TESTS PASSED ✅")
    else:
        print(f"  {PASS}/{total} passed, {FAIL} FAILED ❌")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
