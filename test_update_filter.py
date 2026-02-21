#!/usr/bin/env python3
"""
Tests for the update/status message filter in process_signal().

Verifies that:
  1. Channel update messages (TP hit, SL triggered, breakeven, etc.)
     are correctly detected and would be skipped.
  2. Legitimate new trading signals are NOT falsely blocked.

Run:  python3 test_update_filter.py
"""

import re
import sys

# ── The filter patterns (copied from gmx.py so test is self-contained) ──
_UPDATE_PATTERNS = [
    # Target / TP hit announcements
    r"target\s*\d*\s*(?:was\s+)?(?:hit|reached|smashed|done|nailed|achieved|✅)",
    r"tp\s*\d*\s*(?:was\s+)?(?:hit|reached|smashed|done|nailed|achieved|✅)",
    r"(?:first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|final|last|all)\s*targets?\s*(?:was\s+)?(?:hit|reached|smashed|done)",
    r"all\s*(?:tp|targets?)\s*(?:hit|reached|done|smashed)",
    # Stop loss / stopped out
    r"stopped?\s*(?:out|loss)",
    r"sl\s*(?:was\s+)?(?:hit|triggered|reached|filled)",
    r"stop\s*loss\s*(?:was\s+)?(?:hit|triggered|reached|filled)",
    # SL moved / breakeven updates
    r"sl\s*(?:moved?|set|adjusted)\s*(?:to|at)",
    r"(?:move|moved|set|adjust)\s*(?:sl|stop\s*loss)\s*(?:to|at)",
    r"breakeven",
    r"break\s*even",
    # Position closed / profit taken
    r"closed?\s*(?:in|at|with|for)\s*(?:profit|loss|[\+\-])",
    r"position\s*(?:closed?|exited)",
    r"trade\s*(?:closed?|exited|done|finished)",
    r"(?:profit|loss)\s*(?:taken|booked|secured|locked)",
    # Running in profit / loss updates
    r"running\s*(?:in\s*)?(?:profit|loss|\+|\-)",
    # PnL result lines
    r"pnl\s*[:=]",
    r"[\+\-]\s*\d+(?:\.\d+)?\s*(?:pips?|%|usd|usdt)",
    # Explicit "update" language
    r"(?:signal|trade)\s*update",
]


def is_update_message(text: str) -> bool:
    """Returns True if text matches any update pattern (would be skipped)."""
    lower = text.lower()
    for pat in _UPDATE_PATTERNS:
        if re.search(pat, lower):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════
# UPDATE MESSAGES — all of these SHOULD be filtered (is_update_message → True)
# ═══════════════════════════════════════════════════════════════════════
UPDATE_MESSAGES = [
    # ── Target hit variations (from MegaWhale-style channels) ──
    "🎯 BTC/USDT LONG\nFirst target was hit\nProfit: +2.5%",
    "ETH LONG — Target 1 hit ✅",
    "🟢 BTC LONG — Target 4 smashed! 🚀",
    "SOL SHORT — All targets reached ✅✅✅",
    "LINK LONG — Target 2 was hit! 🎯",
    "BTC SHORT target reached ✅",
    "Target 1 reached\nBTC LONG",
    "Target hit at $98,500 — BTC LONG",
    "All targets done 🎯🎯🎯",
    "Final target hit — closing position",
    "Second target reached!",
    "3rd target smashed 💥",
    "TP1 hit ✅",
    "TP2 reached — moving SL",
    "TP 3 smashed!",
    "All TP hit ✅",

    # ── Stop loss / stopped out ──
    "BTC LONG — Stopped out at entry 😐",
    "Stopped out — small loss",
    "ETH SHORT stopped out at breakeven",
    "SL hit at $92,000",
    "Stop loss triggered",
    "SL was hit — -1.2%",
    "SL reached at entry price",
    "Stop loss was triggered on BTC LONG",
    "SL filled at $42,500",

    # ── SL moved / breakeven ──
    "SL moved to entry ✅",
    "Move SL to breakeven",
    "SL set to $95,000 (entry)",
    "SL adjusted to TP1 level",
    "Moved SL to $96,500 after TP1",
    "Set stop loss to entry",
    "Adjust SL to breakeven now",
    "SL move to entry price",
    "Breakeven 🔒",
    "Now at break even — risk free trade",

    # ── Position closed / profit taken ──
    "Closed in profit 💰",
    "BTC LONG closed at +5.2%",
    "Trade closed with profit",
    "Position closed — +$350",
    "Closed for loss -2.1%",
    "Position exited at TP2",
    "Trade done ✅",
    "Profit taken at $99,500",
    "Loss booked — moving on",
    "Profit secured 🔒",
    "Trade exited — breakeven",

    # ── Running in profit/loss ──
    "Running in profit +3.5% 🟢",
    "Running +250 pips so far",
    "Currently running in loss -1%",

    # ── PnL result lines ──
    "PnL: +5.2%",
    "PnL = -$120",
    "+350 pips on BTC LONG 🎉",
    "-2.5% on ETH SHORT",
    "+150 pips ✅",
    "+5% USDT profit",

    # ── Explicit update language ──
    "Signal update: BTC LONG still running",
    "Trade update — TP1 secured, riding rest",

    # ── Combined/complex messages (realistic channel posts) ──
    """🟢 BTC/USDT LONG
First target was hit ✅
Profit: +2.5%
Move SL to entry""",

    """ETH SHORT
Target 2 smashed! 🎯
Running in profit +4.8%
SL moved to TP1""",

    """LINK LONG — All targets hit! 🎯🎯🎯
Total profit: +12.3%
Amazing trade 🚀""",

    """BTC LONG
Stopped out at entry
0% profit/loss
Better luck next time""",

    """SOL SHORT
TP1 hit ✅ (+3.2%)
TP2 hit ✅ (+5.1%)
SL moved to entry
Remaining position running""",
]


# ═══════════════════════════════════════════════════════════════════════
# REAL SIGNALS — these must NOT be filtered (is_update_message → False)
# ═══════════════════════════════════════════════════════════════════════
REAL_SIGNALS = [
    # ── Standard signal formats ──
    """BTC LONG
Entry: 95000-96000
TP1: 98000 (50%)
TP2: 100000 (50%)
SL: 93000
Leverage: 10x""",

    """ETH SHORT
Entry: 3500-3550
TP1: 3400
TP2: 3300
TP3: 3200
SL: 3650
Leverage: 5x""",

    """SOL LONG
Entry: 145-148
TP1: 155
TP2: 165
SL: 138
Leverage: 8x""",

    """LINK LONG
Entry: 18.50-19.00
TP: 21.00
SL: 17.00
Leverage: 5x-10x""",

    # ── Compact formats ──
    "BTC LONG Entry: 95000 TP: 100000 SL: 92000 Lev: 10x",
    "ETH SHORT Entry 3500 TP1 3400 TP2 3300 SL 3600 5x",

    # ── With emoji/formatting ──
    """🟢 BTC/USDT LONG
Entry zone: $95,000 - $96,000
TP1: $98,000 (30%)
TP2: $100,000 (40%)
TP3: $105,000 (30%)
SL: $92,500
Leverage: 10x
Risk: Normal""",

    """🔴 ETH SHORT
Entry: 3,500 - 3,550
Target 1: 3,400
Target 2: 3,300
Target 3: 3,200
Stop loss: 3,650
Leverage: 5x""",

    # ── With "trailing" keyword (should NOT trigger breakeven filter) ──
    """BTC LONG
Entry: 95000-96000
TP1: 98000
TP2: 100000
SL: 93000
Leverage: 10x
Trailing: Enabled""",

    # ── MegaWhale signal format (BTC/USDT, "Target", $price+usd, Gain/RR lines) ──
    """BTC/USDT Short 50x
1hr rejection from 200ema and RD print on MCB,
10m neg momentum shift and
breakdown of daily
uptrend
Entry: $67,783usd
Target 1: $67,511usd
Target 2: $67,277usd
Target 3: $67,000usd
Target 4: $66,615usd
SL: $68,336usd
Gain: 1.723% loss: 0.817%
RR:""",

    # ── Minimal valid signal ──
    """BTC LONG
Entry: 95000
TP: 98000
SL: 93000
5x""",

    # ── Signal with "close" in TP percentage (should NOT match "closed in profit") ──
    """ETH LONG
Entry: 3500-3600
TP1: 3800 (50% close)
TP2: 4000 (50% close)
SL: 3300
Leverage: 7x""",
]


def run_tests():
    passed = 0
    failed = 0
    errors = []

    print("=" * 70)
    print("TESTING UPDATE MESSAGES (should ALL be filtered)")
    print("=" * 70)
    for i, msg in enumerate(UPDATE_MESSAGES, 1):
        result = is_update_message(msg)
        label = msg.split("\n")[0][:60]
        if result:
            print(f"  ✅ #{i:02d} FILTERED: {label}")
            passed += 1
        else:
            print(f"  ❌ #{i:02d} MISSED:   {label}")
            failed += 1
            errors.append(("UPDATE (should filter)", i, msg))

    print()
    print("=" * 70)
    print("TESTING REAL SIGNALS (should NOT be filtered)")
    print("=" * 70)
    for i, msg in enumerate(REAL_SIGNALS, 1):
        result = is_update_message(msg)
        label = msg.split("\n")[0][:60]
        if not result:
            print(f"  ✅ #{i:02d} PASSED:   {label}")
            passed += 1
        else:
            # Find which pattern matched (for debugging)
            matched_pat = "?"
            for pat in _UPDATE_PATTERNS:
                if re.search(pat, msg.lower()):
                    matched_pat = pat
                    break
            print(f"  ❌ #{i:02d} BLOCKED:  {label}  (by: {matched_pat})")
            failed += 1
            errors.append(("SIGNAL (should pass)", i, msg, matched_pat))

    print()
    print("=" * 70)
    total = passed + failed
    print(f"RESULTS: {passed}/{total} passed, {failed} failed")
    print("=" * 70)

    if errors:
        print("\nFAILURES:")
        for err in errors:
            kind = err[0]
            idx = err[1]
            msg_preview = err[2].split("\n")[0][:70]
            extra = f"  matched: {err[3]}" if len(err) > 3 else ""
            print(f"  [{kind}] #{idx}: {msg_preview}{extra}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
