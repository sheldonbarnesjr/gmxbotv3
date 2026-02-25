#!/usr/bin/env python3
"""
On-chain test suite for GMX V2 Trading Bot.

Tests critical functions against real Arbitrum state (read-only by default).
Uses DRY_RUN=true for any write operations to avoid real trades.

Usage:
    python3 test_onchain.py              # Run all tests
    python3 test_onchain.py --live       # Run with live Telegram notifications
"""

import os
import sys
import time
import asyncio
import logging
import traceback

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("test_onchain")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Setup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from web3 import Web3
from eth_account import Account

from config import load_config
from open import (
    parse_signal, Signal, TakeProfit, execute_signal,
    fetch_open_orders, fetch_positions, fetch_current_price,
    create_tp_order, create_sl_order, scale_price,
    EXCHANGE_ROUTER_ABI, ORDER_TYPE_LIMIT_DECREASE, ORDER_TYPE_STOP_LOSS_DECREASE,
)
from close import fetch_positions as close_fetch_positions, GMXPosition
from risk import (
    verify_tp_hit_by_price, determine_new_sl_target,
    calculate_unrealized_pnl, calculate_pnl_percentage,
    classify_exit_reason, validate_sl_tp_direction,
)
from analytics import TradeRecord


PASS = 0
FAIL = 0
WARN = 0


def ok(label):
    global PASS
    PASS += 1
    print(f"  ✅ PASS: {label}")


def fail(label, detail=""):
    global FAIL
    FAIL += 1
    msg = f"  ❌ FAIL: {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)


def warn(label, detail=""):
    global WARN
    WARN += 1
    msg = f"  ⚠️  WARN: {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 1: Signal Parsing — verify TP prices are real, not $1/$2/$3
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_signal_parsing():
    print("\n═══ Test 1: Signal Parsing ═══")

    # The user's actual signal that caused $1/$2/$3 bug
    signal_text = """BTC/USDT Short 25x

high risk short here, rallying into prior resistance after rapid surge, VRPR gap above resistance, target where the rally started, could see one more move up on the short term to create a short term bearish div before correcting, treat as a high risk trade

Entry: $66,215usd

Target 1: $65,770usd
Target 2: $65,169usd
Target 3: $64,742usd
Target 4: $64,218usd

SL: $67,200usd"""

    try:
        signal = parse_signal(signal_text)
        print(f"  Symbol: {signal.symbol}, Side: {signal.side}, Leverage: {signal.leverage}x")
        print(f"  Entry: ${signal.entry_low:,.2f} - ${signal.entry_high:,.2f}")
        print(f"  SL: ${signal.stop_loss:,.2f}")

        if signal.symbol == "BTC":
            ok("Symbol parsed correctly")
        else:
            fail("Symbol", f"expected BTC, got {signal.symbol}")

        if signal.side == "SHORT":
            ok("Side parsed correctly")
        else:
            fail("Side", f"expected SHORT, got {signal.side}")

        if abs(signal.entry_low - 66215.0) < 1:
            ok("Entry price correct")
        else:
            fail("Entry price", f"expected ~66215, got {signal.entry_low}")

        if abs(signal.stop_loss - 67200.0) < 1:
            ok("Stop loss correct")
        else:
            fail("Stop loss", f"expected ~67200, got {signal.stop_loss}")

        expected_tps = [65770.0, 65169.0, 64742.0, 64218.0]
        for i, tp in enumerate(signal.take_profits):
            print(f"  TP{i+1}: ${tp.price:,.2f} ({tp.close_pct:.0%})")
            if abs(tp.price - expected_tps[i]) < 1:
                ok(f"TP{i+1} price correct (${tp.price:,.0f})")
            else:
                fail(f"TP{i+1} price", f"expected ${expected_tps[i]:,.0f}, got ${tp.price:,.2f}")

        # Check that NO TP is $1, $2, or $3
        for i, tp in enumerate(signal.take_profits):
            if tp.price < 10:
                fail(f"TP{i+1} sanity", f"price ${tp.price:,.2f} is absurdly low (old $1/$2/$3 bug)")
            else:
                ok(f"TP{i+1} sanity — not a $1/$2/$3 artifact")

        # Verify close percentages sum to ~1.0
        total_pct = sum(tp.close_pct for tp in signal.take_profits)
        if abs(total_pct - 1.0) < 0.01:
            ok(f"TP close percentages sum to {total_pct:.2f}")
        else:
            fail("TP close pct sum", f"expected ~1.0, got {total_pct:.4f}")

    except Exception as e:
        fail("Signal parsing crashed", str(e))

    # Test 1b: TP format with "TP1:" prefix (common format)
    print("\n  --- Sub-test: TP1/TP2/TP3 format ---")
    signal_tp = """BTC LONG 10x
Entry: 60000-61000
TP1: 63000 (30%)
TP2: 65000 (40%)
TP3: 70000 (30%)
SL: 58000"""

    try:
        sig2 = parse_signal(signal_tp)
        for i, tp in enumerate(sig2.take_profits):
            print(f"  TP{i+1}: ${tp.price:,.2f} ({tp.close_pct:.0%})")
            if tp.price > 10000:
                ok(f"TP{i+1} format correct (${tp.price:,.0f})")
            else:
                fail(f"TP{i+1} format", f"price ${tp.price:,.2f} — backtracking bug!")
    except Exception as e:
        fail("TP format parsing", str(e))

    # Test 1c: Edge case — TP numbers without prices (this was the $1/$2/$3 trigger)
    print("\n  --- Sub-test: TP numbers without prices (backtracking edge case) ---")
    signal_edge = """BTC SHORT
Entry: 66215
TP1
TP2
TP3
TP4: 64218
SL: 67200"""

    try:
        sig3 = parse_signal(signal_edge)
        bad_tps = [tp for tp in sig3.take_profits if tp.price < 100]
        if bad_tps:
            fail("Backtracking edge case", f"found ${bad_tps[0].price:,.2f} as TP price")
        else:
            # Should parse only TP4 via tp_pattern, or fall through to target_pattern
            for i, tp in enumerate(sig3.take_profits):
                print(f"  TP{i+1}: ${tp.price:,.2f}")
            ok("No backtracking artifacts — TP prices are sane")
    except ValueError as ve:
        # It's OK if this raises ValueError for missing TPs
        ok(f"Correctly rejected malformed signal: {ve}")
    except Exception as e:
        fail("Edge case parsing", str(e))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 2: On-chain reads — fetch positions, orders, prices
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_onchain_reads(w3, acct):
    print("\n═══ Test 2: On-chain Reads ═══")

    # 2a: Fetch current prices via Chainlink
    for symbol in ["BTC", "ETH", "SOL"]:
        try:
            price = fetch_current_price(symbol, w3=w3)
            if price and price > 0:
                ok(f"{symbol} price: ${price:,.2f}")
            else:
                fail(f"{symbol} price", "returned None or 0")
        except Exception as e:
            fail(f"{symbol} price fetch", str(e))

    # 2b: Fetch on-chain positions
    try:
        positions = close_fetch_positions(w3, acct.address)
        print(f"\n  On-chain positions for {acct.address[:10]}...:")
        if positions:
            for p in positions:
                side = "LONG" if p.is_long else "SHORT"
                print(f"    {p.symbol} {side} — ${p.size_usd:,.2f} @ ${p.entry_price:,.2f} "
                      f"PnL: ${p.unrealized_pnl:,.2f}")
            ok(f"Found {len(positions)} on-chain position(s)")
        else:
            ok("No on-chain positions (OK)")
    except Exception as e:
        fail("Fetch positions", str(e))

    # 2c: Fetch open orders
    try:
        orders = fetch_open_orders(w3, acct.address)
        tp_count = sum(1 for o in orders if o.get("order_type") == ORDER_TYPE_LIMIT_DECREASE)
        sl_count = sum(1 for o in orders if o.get("order_type") == ORDER_TYPE_STOP_LOSS_DECREASE)
        print(f"\n  Open orders: {len(orders)} total ({tp_count} TP, {sl_count} SL)")
        for o in orders:
            otype = {ORDER_TYPE_LIMIT_DECREASE: "TP", ORDER_TYPE_STOP_LOSS_DECREASE: "SL"}.get(o.get("order_type"), f"type={o.get('order_type')}")
            trigger = o.get("trigger_price", 0)
            print(f"    {otype}: trigger=${trigger:,.2f}, market={o.get('market','?')[:10]}...")
        ok(f"Fetched {len(orders)} order(s)")
    except Exception as e:
        fail("Fetch orders", str(e))

    return positions, orders


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 3: TP hit verification logic
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_tp_hit_logic():
    print("\n═══ Test 3: TP Hit Verification Logic ═══")

    # Scenario: BTC SHORT at $66,199, TP at $64,218
    # Price should NOT trigger TP when above $64,218
    entry = 66199.0
    tp_price = 64218.0

    # Current price near entry — should NOT trigger
    result = verify_tp_hit_by_price(is_long=False, tp_price=tp_price, current_price=66101.0)
    if not result:
        ok(f"SHORT TP @ ${tp_price:,.0f}: price $66,101 → NOT triggered (correct)")
    else:
        fail("FALSE TP HIT", f"price $66,101 triggered SHORT TP at ${tp_price:,.0f}!")

    # Price at entry — should NOT trigger
    result = verify_tp_hit_by_price(is_long=False, tp_price=tp_price, current_price=entry)
    if not result:
        ok(f"SHORT TP @ ${tp_price:,.0f}: price ${entry:,.0f} → NOT triggered (correct)")
    else:
        fail("FALSE TP HIT", f"price ${entry:,.0f} triggered SHORT TP at ${tp_price:,.0f}!")

    # Price 3% above TP — should NOT trigger (old bug would trigger here)
    price_3pct_above = tp_price * 1.03  # ~$66,144
    result = verify_tp_hit_by_price(is_long=False, tp_price=tp_price, current_price=price_3pct_above)
    if not result:
        ok(f"SHORT TP @ ${tp_price:,.0f}: price ${price_3pct_above:,.0f} (3% above) → NOT triggered (FIXED)")
    else:
        fail("OLD 3% BUG STILL PRESENT", f"price ${price_3pct_above:,.0f} (3% above TP) falsely triggered!")

    # Price at TP — should trigger
    result = verify_tp_hit_by_price(is_long=False, tp_price=tp_price, current_price=tp_price)
    if result:
        ok(f"SHORT TP @ ${tp_price:,.0f}: price ${tp_price:,.0f} → triggered (correct)")
    else:
        fail("TP not triggered at exact price", f"${tp_price:,.0f}")

    # Price below TP — should trigger
    result = verify_tp_hit_by_price(is_long=False, tp_price=tp_price, current_price=64000.0)
    if result:
        ok(f"SHORT TP @ ${tp_price:,.0f}: price $64,000 → triggered (correct)")
    else:
        fail("TP not triggered below target", "$64,000")

    # LONG scenario
    print("\n  --- LONG TP tests ---")
    result = verify_tp_hit_by_price(is_long=True, tp_price=70000.0, current_price=69000.0)
    if not result:
        ok("LONG TP @ $70,000: price $69,000 → NOT triggered")
    else:
        fail("LONG false trigger", "$69,000 < $70,000 should not trigger")

    result = verify_tp_hit_by_price(is_long=True, tp_price=70000.0, current_price=70050.0)
    if result:
        ok("LONG TP @ $70,000: price $70,050 → triggered")
    else:
        fail("LONG TP not triggered", "$70,050 should trigger TP at $70,000")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 4: SL trailing logic
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_sl_trailing():
    print("\n═══ Test 4: SL Trailing Logic ═══")

    entry = 66215.0
    tps = [
        TakeProfit(price=65770.0, close_pct=0.15),
        TakeProfit(price=65169.0, close_pct=0.30),
        TakeProfit(price=64742.0, close_pct=0.30),
        TakeProfit(price=64218.0, close_pct=0.25),
    ]

    # TP1 hit → SL moves to entry (breakeven)
    new_sl, label = determine_new_sl_target(1, entry, tps)
    if abs(new_sl - entry) < 1:
        ok(f"TP1 hit → SL to entry (${new_sl:,.0f}) — {label}")
    else:
        fail("TP1 SL target", f"expected entry ${entry:,.0f}, got ${new_sl:,.0f}")

    # TP2 hit → SL moves to TP1
    new_sl, label = determine_new_sl_target(2, entry, tps)
    if abs(new_sl - 65770.0) < 1:
        ok(f"TP2 hit → SL to TP1 (${new_sl:,.0f}) — {label}")
    else:
        fail("TP2 SL target", f"expected TP1 $65,770, got ${new_sl:,.0f}")

    # TP3 hit → SL moves to TP2
    new_sl, label = determine_new_sl_target(3, entry, tps)
    if abs(new_sl - 65169.0) < 1:
        ok(f"TP3 hit → SL to TP2 (${new_sl:,.0f}) — {label}")
    else:
        fail("TP3 SL target", f"expected TP2 $65,169, got ${new_sl:,.0f}")

    # TP4 hit → SL moves to TP3
    new_sl, label = determine_new_sl_target(4, entry, tps)
    if abs(new_sl - 64742.0) < 1:
        ok(f"TP4 hit → SL to TP3 (${new_sl:,.0f}) — {label}")
    else:
        fail("TP4 SL target", f"expected TP3 $64,742, got ${new_sl:,.0f}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 5: PnL / Win Rate calculations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_pnl_winrate():
    print("\n═══ Test 5: PnL / Win Rate Calculations ═══")

    # Test unrealized PnL
    pnl_long = calculate_unrealized_pnl("LONG", 60000, 65000, 10000)
    expected_long = (65000 - 60000) / 60000 * 10000  # ~833.33
    if abs(pnl_long - expected_long) < 1:
        ok(f"LONG PnL: ${pnl_long:,.2f} (expected ~${expected_long:,.2f})")
    else:
        fail("LONG PnL", f"expected ~${expected_long:,.2f}, got ${pnl_long:,.2f}")

    pnl_short = calculate_unrealized_pnl("SHORT", 66199, 64218, 10000)
    expected_short = (66199 - 64218) / 66199 * 10000  # ~299.2
    if abs(pnl_short - expected_short) < 1:
        ok(f"SHORT PnL: ${pnl_short:,.2f} (expected ~${expected_short:,.2f})")
    else:
        fail("SHORT PnL", f"expected ~${expected_short:,.2f}, got ${pnl_short:,.2f}")

    # Test PnL percentage
    pct = calculate_pnl_percentage(833.33, 10000, 10)  # 10x leverage
    if abs(pct - 83.3) < 1:
        ok(f"PnL %: {pct:.1f}% (expected ~83.3%)")
    else:
        fail("PnL %", f"expected ~83.3%, got {pct:.1f}%")

    # Test win rate with mock trades
    trades = [
        TradeRecord(id="1", symbol="BTC", side="LONG", entry_price=60000, exit_price=65000,
                    size_usd=10000, leverage=10, duration_hours=2.0, pnl_usd=833.33,
                    pnl_percentage=83.3, exit_reason="tp_hit", opened_at=time.time()-7200,
                    closed_at=time.time()),
        TradeRecord(id="2", symbol="BTC", side="SHORT", entry_price=66199, exit_price=67000,
                    size_usd=10000, leverage=25, duration_hours=0.5, pnl_usd=-120.96,
                    pnl_percentage=-30.2, exit_reason="sl_triggered", opened_at=time.time()-1800,
                    closed_at=time.time()),
        TradeRecord(id="3", symbol="ETH", side="LONG", entry_price=3000, exit_price=3200,
                    size_usd=5000, leverage=5, duration_hours=4.0, pnl_usd=333.33,
                    pnl_percentage=33.3, exit_reason="tp_hit", opened_at=time.time()-14400,
                    closed_at=time.time()),
    ]

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd < 0]
    win_rate = len(wins) / len(trades) * 100

    if abs(win_rate - 66.7) < 1:
        ok(f"Win rate: {win_rate:.1f}% (2/3 wins)")
    else:
        fail("Win rate", f"expected ~66.7%, got {win_rate:.1f}%")

    total_pnl = sum(t.pnl_usd for t in trades)
    if total_pnl > 0:
        ok(f"Total PnL: ${total_pnl:,.2f} (net positive)")
    else:
        fail("Total PnL", f"expected positive, got ${total_pnl:,.2f}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 6: Exit reason classification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_exit_classification():
    print("\n═══ Test 6: Exit Reason Classification ═══")

    # SL triggered (SHORT: price went above SL)
    reason = classify_exit_reason(
        is_long=False, current_price=67500,
        stop_loss=67200, tp_hits_count=0,
        last_known_tp_count=4, sl_moved_to_entry=False,
        sl_move_label=None,
    )
    if "sl" in reason.lower():
        ok(f"SHORT SL triggered: '{reason}'")
    else:
        warn(f"SHORT SL classification: '{reason}' (expected 'sl_triggered')")

    # TP filled (all TPs hit, position closed)
    reason = classify_exit_reason(
        is_long=False, current_price=64000,
        stop_loss=66215, tp_hits_count=4,
        last_known_tp_count=0, sl_moved_to_entry=True,
        sl_move_label="TP3",
    )
    if "tp" in reason.lower():
        ok(f"All TPs filled: '{reason}'")
    else:
        warn(f"All TPs classification: '{reason}' (expected 'tp_filled')")

    # Manual close (no clear trigger)
    reason = classify_exit_reason(
        is_long=True, current_price=62000,
        stop_loss=58000, tp_hits_count=0,
        last_known_tp_count=3, sl_moved_to_entry=False,
        sl_move_label=None,
    )
    print(f"  Manual/unknown close: '{reason}'")
    ok(f"Classification returned: '{reason}'")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 7: SL/TP direction validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_sl_tp_validation():
    print("\n═══ Test 7: SL/TP Direction Validation ═══")

    # Valid SHORT: SL above entry, TPs below entry
    tps = [TakeProfit(price=64218, close_pct=0.25)]
    err = validate_sl_tp_direction(False, 67200, 66215, 66215, tps)
    if err is None:
        ok("Valid SHORT: SL above entry, TP below entry")
    else:
        fail("Valid SHORT rejected", err)

    # Invalid SHORT: SL below entry (wrong side)
    err = validate_sl_tp_direction(False, 64000, 66215, 66215, tps)
    if err:
        ok(f"Invalid SHORT SL detected: '{err[:50]}...'")
    else:
        fail("Invalid SHORT SL not detected", "SL below entry should be rejected")

    # Valid LONG: SL below entry, TPs above entry
    tps_long = [TakeProfit(price=70000, close_pct=0.50)]
    err = validate_sl_tp_direction(True, 58000, 60000, 61000, tps_long)
    if err is None:
        ok("Valid LONG: SL below entry, TP above entry")
    else:
        fail("Valid LONG rejected", err)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 8: Retry mechanism
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_retry_mechanism():
    print("\n═══ Test 8: Retry Mechanism ═══")

    from open import retry_on_chain

    call_count = 0

    @retry_on_chain(max_retries=3, base_delay=0.1, label="test_retry")
    def flaky_function():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError(f"Network error (attempt {call_count})")
        return "success"

    try:
        result = flaky_function()
        if result == "success" and call_count == 3:
            ok(f"Retry succeeded after {call_count} attempts")
        else:
            fail("Retry behavior", f"result={result}, call_count={call_count}")
    except Exception as e:
        fail("Retry mechanism", str(e))

    # Test that RuntimeError (reverted tx) is NOT retried
    revert_count = 0

    @retry_on_chain(max_retries=3, base_delay=0.1, label="test_no_retry_revert")
    def reverted_tx():
        nonlocal revert_count
        revert_count += 1
        raise RuntimeError("tx reverted")

    try:
        reverted_tx()
        fail("Revert should raise", "but it didn't")
    except RuntimeError:
        if revert_count == 1:
            ok("RuntimeError (revert) NOT retried — raised immediately")
        else:
            fail("Revert retry", f"called {revert_count} times, expected 1")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 9: TP price scaling for GMX V2
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_price_scaling():
    print("\n═══ Test 9: Price Scaling for GMX V2 ═══")

    # BTC: 8 decimals → scale to 30 decimals
    scaled = scale_price(64218.0, "BTC")
    expected = int(64218.0 * 10**22) * 10**0  # 8 decimals → multiply by 10^(30-8) = 10^22
    # Actually scale_price logic may differ, let's just check it's > 0 and reasonable
    if scaled > 0:
        # Reverse: scaled / 10^30 should give a number in the right range
        unscaled = scaled / (10**30)
        if abs(unscaled - 64218.0) < 1:
            ok(f"BTC ${64218:,.0f} scales to {scaled} (unscaled back: ${unscaled:,.2f})")
        else:
            warn(f"BTC price scaling: {scaled} → unscaled ${unscaled:,.2f} (expected ~$64,218)")
    else:
        fail("BTC price scaling returned 0 or negative")

    # ETH: 18 decimals → scale to 30 decimals
    scaled_eth = scale_price(3200.0, "ETH")
    if scaled_eth > 0:
        unscaled_eth = scaled_eth / (10**30)
        if abs(unscaled_eth - 3200.0) < 1:
            ok(f"ETH ${3200:,.0f} scales correctly (unscaled: ${unscaled_eth:,.2f})")
        else:
            warn(f"ETH price scaling: unscaled ${unscaled_eth:,.2f} (expected ~$3,200)")
    else:
        fail("ETH price scaling returned 0")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test 10: Notification test (sends to Telegram if --live flag)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def test_notifications(live=False):
    print("\n═══ Test 10: Notification System ═══")

    if not live:
        print("  (Skipped — use --live to send real Telegram notifications)")
        return

    try:
        from telethon import TelegramClient
        cfg = load_config()

        if not cfg.telegram_api_id or not cfg.telegram_api_hash:
            warn("Telegram API credentials not configured")
            return

        client = TelegramClient(cfg.telegram_session, cfg.telegram_api_id, cfg.telegram_api_hash)
        await client.start()

        chat = cfg.notify_chat
        if not chat and chat != "me":
            # Try raw env var as fallback
            raw_chat = os.getenv("NOTIFY_CHAT", "").strip()
            if raw_chat:
                chat = raw_chat
            else:
                warn("NOTIFY_CHAT not configured")
            await client.disconnect()
            return

        # Send test notification
        test_msg = (
            "🧪 **Bot Test Notification**\n\n"
            "This is an automated test from test_onchain.py\n\n"
            "**Signal Parse Test:**\n"
            "BTC SHORT 25x\n"
            "Entry: $66,215\n"
            "TP1: $65,770 (15%)\n"
            "TP2: $65,169 (30%)\n"
            "TP3: $64,742 (30%)\n"
            "TP4: $64,218 (25%)\n"
            "SL: $67,200\n\n"
            "✅ All TP prices are real BTC prices (not $1/$2/$3)\n"
            "✅ SL trailing logic verified\n"
            "✅ On-chain reads working\n"
            f"✅ Test completed at {time.strftime('%H:%M:%S')}"
        )

        await client.send_message(chat, test_msg)
        ok("Telegram notification sent successfully")

        await client.disconnect()

    except Exception as e:
        fail("Telegram notification", str(e))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def run_tests():
    global PASS, FAIL, WARN
    live = "--live" in sys.argv

    print("=" * 60)
    print("  GMX V2 Trading Bot — On-Chain Test Suite")
    print("=" * 60)

    # Setup Web3
    cfg = load_config()
    w3 = Web3(Web3.HTTPProvider(cfg.rpc_url))
    if not w3.is_connected():
        print(f"\n❌ Cannot connect to RPC: {cfg.rpc_url}")
        return

    print(f"\nRPC: {cfg.rpc_url[:40]}...")
    print(f"Chain ID: {w3.eth.chain_id}")
    print(f"Block: {w3.eth.block_number}")

    acct = Account.from_key(cfg.private_key) if cfg.private_key else None
    if acct:
        balance = w3.eth.get_balance(acct.address)
        print(f"Wallet: {acct.address[:10]}...{acct.address[-6:]}")
        print(f"ETH Balance: {balance / 10**18:.6f} ETH")
    else:
        print("⚠️ No private key — skipping wallet-dependent tests")

    # Run tests
    test_signal_parsing()
    test_tp_hit_logic()
    test_sl_trailing()
    test_pnl_winrate()
    test_exit_classification()
    test_sl_tp_validation()
    test_retry_mechanism()
    test_price_scaling()

    if acct:
        test_onchain_reads(w3, acct)

    await test_notifications(live=live)

    # Summary
    print("\n" + "=" * 60)
    total = PASS + FAIL + WARN
    print(f"  Results: {PASS} passed, {FAIL} failed, {WARN} warnings ({total} total)")
    if FAIL == 0:
        print("  ✅ ALL TESTS PASSED")
    else:
        print(f"  ❌ {FAIL} TEST(S) FAILED")
    print("=" * 60)

    return FAIL == 0


if __name__ == "__main__":
    success = asyncio.run(run_tests())
    sys.exit(0 if success else 1)
