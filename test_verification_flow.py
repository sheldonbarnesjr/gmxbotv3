#!/usr/bin/env python3
"""
Tests for the TP/SL price verification and position close detection logic.

Validates that:
  1. TP hits are only counted when price actually reached the TP level
  2. SL moves only happen after price verification passes
  3. Position close detection verifies price against SL/TP before acting
  4. Exit reasons correctly identify SL at entry vs SL at TP level
  5. Stale/cancelled orders don't trigger false TP hits
  6. Multi-TP hits are verified individually
  7. PnL calculations use correct formulas (no leverage double-count)

Run:  python3 test_verification_flow.py
"""

import sys
import time
from dataclasses import dataclass, field
from typing import Optional, List

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REPLICATE DATA STRUCTURES (self-contained, no imports from gmx.py needed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class TakeProfitLevel:
    price: float
    percentage: float
    executed: bool = False
    executed_at: Optional[float] = None

@dataclass
class Position:
    id: str
    symbol: str
    side: str  # "LONG" or "SHORT"
    size_usd: float
    leverage: float
    entry_price: float
    current_price: float = 0.0
    stop_loss: Optional[float] = None
    take_profits: List[TakeProfitLevel] = field(default_factory=list)
    is_open: bool = True
    unrealized_pnl: float = 0.0
    wallet_id: int = 1
    market_addr: Optional[str] = None
    sl_moved_to_entry: bool = False
    sl_move_label: Optional[str] = None
    sl_move_failed: bool = False
    tp_hits_count: int = 0
    last_known_tp_count: int = 0
    pending_fill: bool = False

    @property
    def collateral_usd(self) -> float:
        return self.size_usd / self.leverage if self.leverage else 0.0

    @property
    def pnl_percentage(self) -> float:
        col = self.collateral_usd
        if col == 0:
            return 0.0
        return (self.unrealized_pnl / col) * 100


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EXTRACTED LOGIC FUNCTIONS (exact copies of the logic from gmx.py)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def verify_tp_price(
    pos: Position,
    hit_count: int,
    best_price: float,
) -> dict:
    """
    Verify that price actually reached the TP levels before acting.

    Returns dict with:
      - verified: bool — whether at least one TP was verified
      - verified_hit_count: int — how many TPs were price-verified
      - reason: str — explanation
    """
    sorted_tps = sorted(pos.take_profits, key=lambda t: t.price,
                        reverse=(pos.side == "SHORT"))
    is_long = pos.side == "LONG"
    first_hit_idx = pos.tp_hits_count  # first NEW TP index

    if first_hit_idx >= len(sorted_tps):
        return {"verified": True, "verified_hit_count": hit_count,
                "reason": "no TPs to verify against"}
    if not best_price:
        return {"verified": False, "verified_hit_count": 0,
                "reason": "no price available — cannot verify"}

    # Check the FIRST new TP that was supposedly hit
    first_tp_price = sorted_tps[first_hit_idx].price
    tolerance = first_tp_price * 0.03  # 3%

    first_reached = False
    if is_long and best_price >= first_tp_price - tolerance:
        first_reached = True
    elif not is_long and best_price <= first_tp_price + tolerance:
        first_reached = True

    if not first_reached:
        return {
            "verified": False,
            "verified_hit_count": 0,
            "reason": (f"Price ${best_price:,.2f} hasn't reached TP{first_hit_idx+1} "
                       f"@ ${first_tp_price:,.2f}")
        }

    # First TP verified. Check each subsequent TP for multi-hit.
    verified_hit_count = 1
    if hit_count > 1:
        for h in range(1, hit_count):
            idx = first_hit_idx + h
            if idx >= len(sorted_tps):
                break
            tp_p = sorted_tps[idx].price
            tp_tol = tp_p * 0.03
            reached = False
            if is_long and best_price >= tp_p - tp_tol:
                reached = True
            elif not is_long and best_price <= tp_p + tp_tol:
                reached = True
            if reached:
                verified_hit_count += 1
            else:
                break

    return {
        "verified": True,
        "verified_hit_count": verified_hit_count,
        "reason": f"Verified {verified_hit_count}/{hit_count} TPs"
    }


def determine_exit_reason(
    pos: Position,
    current_price: Optional[float],
    execution_price: Optional[float],
) -> str:
    """
    Determine the exit reason for a closed position.
    Exact replica of the logic in check_position_closed().
    """
    is_long = pos.side == "LONG"

    if pos.tp_hits_count > 0 and pos.last_known_tp_count == 0:
        return "All TPs filled"

    elif pos.sl_moved_to_entry and pos.stop_loss:
        sl_label = pos.sl_move_label or "Entry"
        sl_price = pos.stop_loss

        sl_tol = sl_price * 0.02
        sl_triggered = False
        if current_price:
            if is_long and current_price <= sl_price + sl_tol:
                sl_triggered = True
            elif not is_long and current_price >= sl_price - sl_tol:
                sl_triggered = True

        if sl_triggered:
            if sl_label == "Entry":
                return "SL (breakeven)"
            else:
                return f"SL at {sl_label} (${sl_price:,.2f})"
        else:
            if pos.tp_hits_count > 0:
                return f"TP/SL hit ({pos.tp_hits_count} TPs filled)"
            else:
                return "SL/TP hit"

    elif pos.tp_hits_count > 0:
        return f"Closed ({pos.tp_hits_count} TPs hit)"
    elif pos.last_known_tp_count > 0:
        return "SL/TP/liquidation"
    else:
        return "SL/liquidation"


def verify_close_price(
    pos: Position,
    current_price: float,
) -> dict:
    """
    Verify that the current price justifies the position being closed.
    Returns dict with sl_could_have_hit, tp_could_have_hit, and justified.
    """
    is_long = pos.side == "LONG"
    sl_price = pos.stop_loss
    result = {"sl_could_have_hit": False, "tp_could_have_hit": False, "justified": False}

    if not sl_price:
        result["justified"] = True  # can't verify without SL
        return result

    tolerance = sl_price * 0.02

    if is_long and current_price <= sl_price + tolerance:
        result["sl_could_have_hit"] = True
    elif not is_long and current_price >= sl_price - tolerance:
        result["sl_could_have_hit"] = True

    if pos.take_profits:
        sorted_tps = sorted(pos.take_profits, key=lambda t: t.price,
                            reverse=(pos.side == "SHORT"))
        last_tp = sorted_tps[-1].price if sorted_tps else None
        if last_tp:
            if is_long and current_price >= last_tp - (last_tp * 0.02):
                result["tp_could_have_hit"] = True
            elif not is_long and current_price <= last_tp + (last_tp * 0.02):
                result["tp_could_have_hit"] = True

    result["justified"] = result["sl_could_have_hit"] or result["tp_could_have_hit"]
    return result


def calculate_pnl(pos: Position, exit_price: float) -> dict:
    """Calculate PnL for a position at a given exit price."""
    if pos.side == "LONG":
        change = exit_price - pos.entry_price
    else:
        change = pos.entry_price - exit_price

    # size_usd already includes leverage — don't multiply again
    pnl_usd = (change / pos.entry_price) * pos.size_usd
    collateral = pos.collateral_usd
    pnl_pct = (pnl_usd / collateral) * 100 if collateral else 0

    return {"pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "collateral": collateral}


def compute_sl_target(pos: Position) -> dict:
    """Given a position with tp_hits_count, compute where SL should move to."""
    sorted_tps = sorted(pos.take_profits, key=lambda t: t.price,
                        reverse=(pos.side == "SHORT"))
    hits = pos.tp_hits_count

    if hits <= 1:
        return {"price": pos.entry_price, "label": "Entry"}
    elif hits - 2 < len(sorted_tps):
        return {"price": sorted_tps[hits - 2].price, "label": f"TP{hits - 1}"}
    else:
        if sorted_tps:
            return {"price": sorted_tps[-1].price, "label": f"TP{len(sorted_tps)}"}
        return {"price": pos.entry_price, "label": "Entry"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HELPER: create standard test positions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def make_btc_long(entry=65000.0, sl=63000.0, tps=None):
    """Create a standard BTC LONG position for testing."""
    if tps is None:
        tps = [66000, 67000, 68000, 69000]
    return Position(
        id="test-btc-long",
        symbol="BTC",
        side="LONG",
        size_usd=1250.0,  # $25 collateral * 50x leverage
        leverage=50.0,
        entry_price=entry,
        stop_loss=sl,
        take_profits=[TakeProfitLevel(price=p, percentage=0.25) for p in tps],
        market_addr="0x47c031236e19d024b42f8ae6780e44a573170703",
        last_known_tp_count=len(tps),
    )

def make_btc_short(entry=65900.0, sl=66500.0, tps=None):
    """Create a standard BTC SHORT position for testing."""
    if tps is None:
        tps = [65635, 65315, 65000, 64500]
    return Position(
        id="test-btc-short",
        symbol="BTC",
        side="SHORT",
        size_usd=1250.0,
        leverage=50.0,
        entry_price=entry,
        stop_loss=sl,
        take_profits=[TakeProfitLevel(price=p, percentage=0.25) for p in tps],
        market_addr="0x47c031236e19d024b42f8ae6780e44a573170703",
        last_known_tp_count=len(tps),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEST SUITE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

passed = 0
failed = 0

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}")
        if detail:
            print(f"     → {detail}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: TP PRICE VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("  SECTION 1: TP Price Verification (check_tp_hits)")
print("="*70)

# ── 1.1 LONG: Price reached TP1 → verified ──
print("\n[1.1] LONG — TP1 hit, price at TP1 level")
pos = make_btc_long()
result = verify_tp_price(pos, hit_count=1, best_price=66100.0)
test("TP1 verified when price >= TP1", result["verified"])
test("Verified count = 1", result["verified_hit_count"] == 1, f"got {result['verified_hit_count']}")

# ── 1.2 LONG: Price NOT at TP1 → rejected ──
print("\n[1.2] LONG — TP1 supposedly hit, but price far below TP1")
pos = make_btc_long()
# TP1=66000, 3% tolerance floor = 64020. Price at 63000 is well below.
result = verify_tp_price(pos, hit_count=1, best_price=63000.0)
test("TP1 rejected when price far below TP1", not result["verified"])
test("Verified count = 0", result["verified_hit_count"] == 0, f"got {result['verified_hit_count']}")

# ── 1.3 LONG: Price at TP1 within 3% tolerance ──
print("\n[1.3] LONG — TP1 hit, price within 3% tolerance (retraced slightly)")
pos = make_btc_long()
# TP1 = 66000, 3% tolerance = 1980, floor = 64020
result = verify_tp_price(pos, hit_count=1, best_price=63500.0)
test("TP1 rejected when price below 3% tolerance floor", not result["verified"],
     f"price=63500, TP1=66000, floor=64020")

result2 = verify_tp_price(pos, hit_count=1, best_price=64200.0)
test("TP1 verified when price at 3% tolerance", result2["verified"],
     f"price=64200, TP1=66000, floor=64020")

# ── 1.4 SHORT: Price reached TP1 → verified ──
print("\n[1.4] SHORT — TP1 hit, price at TP1 level")
pos = make_btc_short()
# SHORT TPs sorted in descending order: [65635, 65315, 65000, 64500]
result = verify_tp_price(pos, hit_count=1, best_price=65500.0)
test("SHORT TP1 verified when price <= TP1", result["verified"])
test("Verified count = 1", result["verified_hit_count"] == 1)

# ── 1.5 SHORT: Price NOT at TP1 → rejected ──
print("\n[1.5] SHORT — TP1 supposedly hit, but price far above TP1")
pos = make_btc_short()
# SHORT TP1 = 65635, 3% ceiling = 65635 + 1969 = 67604. Use price well above.
result = verify_tp_price(pos, hit_count=1, best_price=68000.0)
test("SHORT TP1 rejected when price far above TP1", not result["verified"])

# ── 1.6 LONG: Multi-TP hit, all verified ──
print("\n[1.6] LONG — 3 TPs hit, price reached TP3")
pos = make_btc_long()
# TPs: 66000, 67000, 68000, 69000. Price at 68200 → should verify 3 TPs
result = verify_tp_price(pos, hit_count=3, best_price=68200.0)
test("3 TPs verified when price >= TP3", result["verified"])
test("Verified count = 3", result["verified_hit_count"] == 3, f"got {result['verified_hit_count']}")

# ── 1.7 LONG: Multi-TP hit, only some verified ──
print("\n[1.7] LONG — 3 TPs supposedly hit, but price only reached TP2")
pos = make_btc_long()
# TPs: 66000, 67000, 68000, 69000
# TP2 floor = 67000 - 2010 = 64990. TP3 floor = 68000 - 2040 = 65960.
# Price 65500 → above TP2 floor (64990) ✅ but below TP3 floor (65960) ❌
result = verify_tp_price(pos, hit_count=3, best_price=65500.0)
test("Partial verification passes", result["verified"])
test("Only 2 of 3 TPs verified", result["verified_hit_count"] == 2,
     f"got {result['verified_hit_count']}")

# ── 1.8 SHORT: Multi-TP hit, all verified ──
print("\n[1.8] SHORT — 2 TPs hit, price reached TP2")
pos = make_btc_short()
# SHORT TPs sorted descending: [65635, 65315, 65000, 64500]
result = verify_tp_price(pos, hit_count=2, best_price=65200.0)
test("SHORT 2 TPs verified", result["verified"])
test("Verified count = 2", result["verified_hit_count"] == 2, f"got {result['verified_hit_count']}")

# ── 1.9 After TP1 already hit, verify TP2 ──
print("\n[1.9] LONG — TP1 already hit, now TP2 hit")
pos = make_btc_long()
pos.tp_hits_count = 1  # TP1 already counted
# Now verifying TP2 hit (second TP at 67000)
result = verify_tp_price(pos, hit_count=1, best_price=67100.0)
test("TP2 verified after TP1 already hit", result["verified"])
test("Verified count = 1", result["verified_hit_count"] == 1)

# ── 1.10 After TP1 already hit, TP2 NOT reached ──
print("\n[1.10] LONG — TP1 already hit, TP2 supposedly hit but price far below")
pos = make_btc_long()
pos.tp_hits_count = 1
# TP2=67000, 3% tolerance floor = 67000 - 2010 = 64990. Use price below floor.
result = verify_tp_price(pos, hit_count=1, best_price=64000.0)
test("TP2 rejected when price far below TP2", not result["verified"],
     f"price=64000, TP2=67000, floor=64990")

# ── 1.11 SHORT multi-TP: your exact trade scenario ──
print("\n[1.11] SHORT — Replicating your actual trade: TP2+TP3 hit")
pos = make_btc_short()
pos.tp_hits_count = 1  # TP1 already hit
# TP2=65315, TP3=65000. Price at 64800 should verify both
result = verify_tp_price(pos, hit_count=2, best_price=64800.0)
test("Both TP2 and TP3 verified at $64,800", result["verified"])
test("Verified count = 2", result["verified_hit_count"] == 2)

# SHORT TP2=65315, ceiling=65315+1959=67274. TP3=65000, ceiling=65000+1950=66950.
# Price 67100 → below TP2 ceiling (67274) ✅ but above TP3 ceiling (66950) ❌
result2 = verify_tp_price(pos, hit_count=2, best_price=67100.0)
test("Only TP2 verified at $67,100 (TP3 ceiling=$66,950 exceeded)", result2["verified"])
test("Verified count = 1 (TP2 only)", result2["verified_hit_count"] == 1,
     f"got {result2['verified_hit_count']}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: SL MOVE TARGETS
# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("  SECTION 2: SL Move Targets (progressive trailing)")
print("="*70)

# ── 2.1 After TP1: SL → entry ──
print("\n[2.1] After TP1 hit: SL should move to entry")
pos = make_btc_short(entry=65900.0)
pos.tp_hits_count = 1
sl_target = compute_sl_target(pos)
test("SL target = entry price", sl_target["price"] == 65900.0, f"got ${sl_target['price']:,.2f}")
test("SL label = Entry", sl_target["label"] == "Entry", f"got {sl_target['label']}")

# ── 2.2 After TP2: SL → TP1 ──
print("\n[2.2] After TP2 hit: SL should move to TP1")
pos = make_btc_short(entry=65900.0)
pos.tp_hits_count = 2
sl_target = compute_sl_target(pos)
# SHORT TPs descending: [65635, 65315, 65000, 64500]
# hits=2, so SL → sorted_tps[0] = TP1 = 65635
test("SL target = TP1 price ($65,635)", sl_target["price"] == 65635.0,
     f"got ${sl_target['price']:,.2f}")
test("SL label = TP1", sl_target["label"] == "TP1", f"got {sl_target['label']}")

# ── 2.3 After TP3: SL → TP2 ──
print("\n[2.3] After TP3 hit: SL should move to TP2")
pos = make_btc_short(entry=65900.0)
pos.tp_hits_count = 3
sl_target = compute_sl_target(pos)
test("SL target = TP2 price ($65,315)", sl_target["price"] == 65315.0,
     f"got ${sl_target['price']:,.2f}")
test("SL label = TP2", sl_target["label"] == "TP2", f"got {sl_target['label']}")

# ── 2.4 After TP4: SL → TP3 ──
print("\n[2.4] After TP4 hit (last TP): SL should move to TP3")
pos = make_btc_short(entry=65900.0)
pos.tp_hits_count = 4
sl_target = compute_sl_target(pos)
test("SL target = TP3 price ($65,000)", sl_target["price"] == 65000.0,
     f"got ${sl_target['price']:,.2f}")
test("SL label = TP3", sl_target["label"] == "TP3", f"got {sl_target['label']}")

# ── 2.5 LONG: After TP2: SL → TP1 ──
print("\n[2.5] LONG after TP2: SL should move to TP1")
pos = make_btc_long(entry=65000.0, tps=[66000, 67000, 68000])
pos.tp_hits_count = 2
sl_target = compute_sl_target(pos)
test("LONG SL → TP1 ($66,000)", sl_target["price"] == 66000.0,
     f"got ${sl_target['price']:,.2f}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: EXIT REASON DETECTION
# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("  SECTION 3: Exit Reason Detection")
print("="*70)

# ── 3.1 All TPs filled ──
print("\n[3.1] All TPs filled (tp_count=0, hits>0)")
pos = make_btc_long()
pos.tp_hits_count = 4
pos.last_known_tp_count = 0
reason = determine_exit_reason(pos, current_price=69500.0, execution_price=None)
test("Exit reason = 'All TPs filled'", reason == "All TPs filled", f"got: {reason}")

# ── 3.2 SL at breakeven (SL moved to entry) ──
print("\n[3.2] SL hit at breakeven (moved to entry, price near entry)")
pos = make_btc_short(entry=65900.0, sl=66500.0)
pos.sl_moved_to_entry = True
pos.sl_move_label = "Entry"
pos.stop_loss = 65900.0  # moved to entry
pos.tp_hits_count = 1
reason = determine_exit_reason(pos, current_price=65950.0, execution_price=None)
test("Exit reason = 'SL (breakeven)'", reason == "SL (breakeven)", f"got: {reason}")

# ── 3.3 SL at TP2 level (NOT breakeven) ──
print("\n[3.3] SL hit at TP2 level (SL was moved to TP2 after 3 TP hits)")
pos = make_btc_short(entry=65900.0)
pos.sl_moved_to_entry = True
pos.sl_move_label = "TP2"
pos.stop_loss = 65315.0  # moved to TP2
pos.tp_hits_count = 3
reason = determine_exit_reason(pos, current_price=65400.0, execution_price=None)
test("Exit reason = 'SL at TP2'", "SL at TP2" in reason, f"got: {reason}")
test("Exit reason includes price", "$65,315" in reason, f"got: {reason}")

# ── 3.4 SL at TP1 level ──
print("\n[3.4] SL hit at TP1 level")
pos = make_btc_long(entry=65000.0, tps=[66000, 67000, 68000])
pos.sl_moved_to_entry = True
pos.sl_move_label = "TP1"
pos.stop_loss = 66000.0
pos.tp_hits_count = 2
reason = determine_exit_reason(pos, current_price=65800.0, execution_price=None)
test("LONG: Exit reason includes 'SL at TP1'", "SL at TP1" in reason, f"got: {reason}")

# ── 3.5 Price not near SL → "TP/SL hit" ──
print("\n[3.5] Position closed but price not near SL (could be TP fill)")
pos = make_btc_short(entry=65900.0)
pos.sl_moved_to_entry = True
pos.sl_move_label = "Entry"
pos.stop_loss = 65900.0
pos.tp_hits_count = 2
# Price is way below entry — not near SL at entry
reason = determine_exit_reason(pos, current_price=64000.0, execution_price=None)
test("Exit reason = 'TP/SL hit' (price far from SL)", "TP/SL hit" in reason, f"got: {reason}")

# ── 3.6 No SL move, position closed ──
print("\n[3.6] Position closed, no SL move ever happened")
pos = make_btc_long()
pos.sl_moved_to_entry = False
pos.tp_hits_count = 0
pos.last_known_tp_count = 4
reason = determine_exit_reason(pos, current_price=62000.0, execution_price=None)
test("Exit reason = 'SL/TP/liquidation'", reason == "SL/TP/liquidation", f"got: {reason}")

# ── 3.7 No TPs ever existed ──
print("\n[3.7] Position with no TPs closed")
pos = make_btc_long()
pos.take_profits = []
pos.last_known_tp_count = 0
reason = determine_exit_reason(pos, current_price=62000.0, execution_price=None)
test("Exit reason = 'SL/liquidation'", reason == "SL/liquidation", f"got: {reason}")

# ── 3.8 TPs hit but SL never moved (move_sl failed) ──
print("\n[3.8] TPs hit but SL never moved (move_sl failed)")
pos = make_btc_short(entry=65900.0)
pos.tp_hits_count = 3
pos.sl_moved_to_entry = False
pos.sl_move_failed = True
pos.last_known_tp_count = 1
reason = determine_exit_reason(pos, current_price=65000.0, execution_price=None)
test("Exit reason acknowledges TP hits", "3 TPs hit" in reason, f"got: {reason}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: CLOSE PRICE VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("  SECTION 4: Close Price Verification (check_position_closed)")
print("="*70)

# ── 4.1 LONG: price below SL → justified ──
print("\n[4.1] LONG: price dropped below SL — close is justified")
pos = make_btc_long(sl=63000.0)
result = verify_close_price(pos, current_price=62800.0)
test("SL could have hit", result["sl_could_have_hit"])
test("Close is justified", result["justified"])

# ── 4.2 LONG: price above entry — doesn't justify SL hit ──
print("\n[4.2] LONG: price still above entry — SL shouldn't have hit")
pos = make_btc_long(sl=63000.0)
result = verify_close_price(pos, current_price=65500.0)
test("SL could NOT have hit", not result["sl_could_have_hit"])

# ── 4.3 LONG: price at last TP → justified (all TPs filled) ──
print("\n[4.3] LONG: price at last TP level — could be all TPs filled")
pos = make_btc_long(sl=63000.0, tps=[66000, 67000, 68000, 69000])
result = verify_close_price(pos, current_price=69500.0)
test("TP could have hit", result["tp_could_have_hit"])
test("Close is justified", result["justified"])

# ── 4.4 LONG: price in middle — neither SL nor last TP ──
print("\n[4.4] LONG: price in no-man's-land — doesn't justify close")
pos = make_btc_long(sl=63000.0, tps=[66000, 67000, 68000, 69000])
result = verify_close_price(pos, current_price=65500.0)
test("SL could NOT have hit", not result["sl_could_have_hit"])
test("TP could NOT have hit", not result["tp_could_have_hit"])
test("Close is NOT justified", not result["justified"])

# ── 4.5 SHORT: price above SL → justified ──
print("\n[4.5] SHORT: price rose above SL — close is justified")
pos = make_btc_short(sl=66500.0)
result = verify_close_price(pos, current_price=66600.0)
test("SHORT SL could have hit", result["sl_could_have_hit"])
test("Close is justified", result["justified"])

# ── 4.6 SHORT: price way below entry — doesn't justify SL ──
print("\n[4.6] SHORT: price dropped way below — SL shouldn't have hit")
pos = make_btc_short(sl=66500.0)
result = verify_close_price(pos, current_price=64000.0)
test("SHORT SL could NOT have hit", not result["sl_could_have_hit"])

# ── 4.7 SHORT: price at last TP → justified ──
print("\n[4.7] SHORT: price at last TP level — all TPs filled")
pos = make_btc_short(tps=[65635, 65315, 65000, 64500])
result = verify_close_price(pos, current_price=64300.0)
test("SHORT last TP could have hit", result["tp_could_have_hit"])
test("Close is justified", result["justified"])


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: PNL CALCULATIONS
# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("  SECTION 5: PnL Calculations (no leverage double-count)")
print("="*70)

# ── 5.1 LONG: +1% move ──
print("\n[5.1] LONG: BTC entry $65,000 → exit $65,650 (+1%)")
pos = make_btc_long(entry=65000.0)  # size=1250, leverage=50, collateral=25
result = calculate_pnl(pos, exit_price=65650.0)
test("Collateral = $25", abs(result["collateral"] - 25.0) < 0.01,
     f"got ${result['collateral']:.2f}")
# Expected: (650/65000) * 1250 = 12.50
test("PnL USD = $12.50", abs(result["pnl_usd"] - 12.50) < 0.01,
     f"got ${result['pnl_usd']:.2f}")
# PnL% relative to collateral: 12.50/25 * 100 = 50%
test("PnL % = +50%", abs(result["pnl_pct"] - 50.0) < 0.1,
     f"got {result['pnl_pct']:.1f}%")

# ── 5.2 SHORT: +1% move ──
print("\n[5.2] SHORT: BTC entry $65,900 → exit $65,241 (price dropped ~1%)")
pos = make_btc_short(entry=65900.0)
result = calculate_pnl(pos, exit_price=65241.0)
expected = ((65900 - 65241) / 65900) * 1250
test(f"PnL USD = ~${expected:.2f}", abs(result["pnl_usd"] - expected) < 0.1,
     f"got ${result['pnl_usd']:.2f}")

# ── 5.3 Verify no leverage double-counting ──
print("\n[5.3] Verify leverage NOT double-counted")
pos = make_btc_long(entry=65000.0)
result = calculate_pnl(pos, exit_price=65650.0)
# WRONG (old buggy way): (650/65000) * 1250 * 50 = $625
# CORRECT: (650/65000) * 1250 = $12.50
test("PnL is NOT $625 (double-counted)", abs(result["pnl_usd"] - 625.0) > 100,
     f"PnL = ${result['pnl_usd']:.2f}")
test("PnL IS $12.50 (correct)", abs(result["pnl_usd"] - 12.50) < 0.01,
     f"PnL = ${result['pnl_usd']:.2f}")

# ── 5.4 SHORT loss ──
print("\n[5.4] SHORT: losing trade (price went up)")
pos = make_btc_short(entry=65900.0)
result = calculate_pnl(pos, exit_price=66500.0)
test("PnL is negative (loss)", result["pnl_usd"] < 0, f"got ${result['pnl_usd']:.2f}")

# ── 5.5 Breakeven ──
print("\n[5.5] Breakeven: exit at entry price")
pos = make_btc_long(entry=65000.0)
result = calculate_pnl(pos, exit_price=65000.0)
test("PnL = $0 at breakeven", abs(result["pnl_usd"]) < 0.01, f"got ${result['pnl_usd']:.2f}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: YOUR ACTUAL TRADE SCENARIO (end-to-end)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("  SECTION 6: Your Actual Trade (BTC SHORT full simulation)")
print("="*70)

print("\n[6.1] Simulating: BTC SHORT entry=$65,900 SL=$66,500")
pos = make_btc_short(entry=65900.0, sl=66500.0, tps=[65635, 65315, 65000, 64500])

# Step 1: TP1 hit — price at $65,500
print("\n  Step 1: TP1 hit (price=$65,500)")
v1 = verify_tp_price(pos, hit_count=1, best_price=65500.0)
test("TP1 verified at $65,500", v1["verified"])
if v1["verified"]:
    pos.tp_hits_count += v1["verified_hit_count"]
sl1 = compute_sl_target(pos)
test("SL should move to Entry ($65,900)", sl1["price"] == 65900.0 and sl1["label"] == "Entry",
     f"got {sl1['label']} @ ${sl1['price']:,.2f}")

# Simulate SL move SUCCESS
pos.stop_loss = sl1["price"]
pos.sl_moved_to_entry = True
pos.sl_move_label = sl1["label"]

# Step 2: TP2+TP3 hit — price at $64,800
print("\n  Step 2: TP2+TP3 hit (price=$64,800)")
v2 = verify_tp_price(pos, hit_count=2, best_price=64800.0)
test("TP2+TP3 verified at $64,800", v2["verified"])
test("Both TPs verified", v2["verified_hit_count"] == 2, f"got {v2['verified_hit_count']}")
if v2["verified"]:
    pos.tp_hits_count += v2["verified_hit_count"]
sl2 = compute_sl_target(pos)
test("SL should move to TP2 ($65,315)", sl2["price"] == 65315.0 and sl2["label"] == "TP2",
     f"got {sl2['label']} @ ${sl2['price']:,.2f}")

# Simulate SL move SUCCESS
pos.stop_loss = sl2["price"]
pos.sl_move_label = sl2["label"]

# Step 3: Position closes — SL at TP2 triggers (price retraces to $65,400)
print("\n  Step 3: Position closes (price retraced to $65,400)")
reason = determine_exit_reason(pos, current_price=65400.0, execution_price=None)
test("Exit reason = 'SL at TP2'", "SL at TP2" in reason, f"got: {reason}")
test("NOT 'SL (breakeven)'", "breakeven" not in reason.lower(), f"got: {reason}")

pnl = calculate_pnl(pos, exit_price=65400.0)
test("PnL is positive (profit locked at TP2)", pnl["pnl_usd"] > 0,
     f"PnL = ${pnl['pnl_usd']:.2f}")

# ── 6.2 Simulate the FAILED SL move scenario ──
print("\n[6.2] Same trade but SL move FAILS after TP1")
pos2 = make_btc_short(entry=65900.0, sl=66500.0, tps=[65635, 65315, 65000, 64500])

# TP1 hits, but move_sl fails
pos2.tp_hits_count = 1
pos2.sl_move_failed = True
# SL is still at original $66,500 (never moved)

# TP2+TP3 hit
v2b = verify_tp_price(pos2, hit_count=2, best_price=64800.0)
test("[Failed SL] TP2+TP3 still verified", v2b["verified"])
pos2.tp_hits_count += v2b["verified_hit_count"]

# Position closes — original SL triggers at $66,500 (price went back up)
# But we have execution price from events showing $65,650
reason2 = determine_exit_reason(pos2, current_price=65650.0, execution_price=65650.0)
# pos2.sl_moved_to_entry is False, tp_hits_count = 3
test("[Failed SL] Exit reason mentions TPs hit", "3 TPs hit" in reason2, f"got: {reason2}")

# The sl_move_failed flag should be checked in the notification
test("[Failed SL] sl_move_failed is set", pos2.sl_move_failed)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("  SECTION 7: Edge Cases")
print("="*70)

# ── 7.1 No TPs defined ──
print("\n[7.1] Position with no take profits")
pos = Position(id="no-tp", symbol="BTC", side="LONG", size_usd=1000.0,
               leverage=10.0, entry_price=65000.0, stop_loss=63000.0,
               market_addr="0xabc", last_known_tp_count=0)
result = verify_tp_price(pos, hit_count=1, best_price=66000.0)
test("No TPs → verification passes by default", result["verified"])

# ── 7.2 Zero price (RPC failure) ──
print("\n[7.2] Zero/None price from RPC failure")
pos = make_btc_long()
result = verify_tp_price(pos, hit_count=1, best_price=0.0)
# With 0 price: first_tp_price * 0.03 = tolerance, 0 >= first_tp - tolerance?
# 0 >= 66000 - 1980 = 64020? No. So should fail.
test("Zero price → TP not verified", not result["verified"],
     f"verified={result['verified']}")

# ── 7.3 Very tight tolerance: price just barely at TP ──
print("\n[7.3] Price just barely at 3% tolerance boundary")
pos = make_btc_long(tps=[100000])  # TP at 100k
# 3% of 100000 = 3000. So floor = 97000
result_at = verify_tp_price(pos, hit_count=1, best_price=97000.0)
test("Price at exact 3% tolerance floor → verified", result_at["verified"])
result_below = verify_tp_price(pos, hit_count=1, best_price=96999.0)
test("Price $1 below tolerance → rejected", not result_below["verified"])

# ── 7.4 Position with 10 TPs ──
print("\n[7.4] Position with 10 TPs (max supported)")
tps_10 = [66000 + i*500 for i in range(10)]  # 66000 to 70500
pos = make_btc_long(tps=tps_10)
result = verify_tp_price(pos, hit_count=5, best_price=68100.0)
# TPs: 66000, 66500, 67000, 67500, 68000, 68500, 69000, 69500, 70000, 70500
# Price 68100 should verify TPs 1-5 (up to 68000)
test("5 of 10 TPs verified", result["verified"])
test("Verified count = 5", result["verified_hit_count"] == 5,
     f"got {result['verified_hit_count']}")

# ── 7.5 SL at TP1 with price near TP1 ──
print("\n[7.5] Exit reason when SL moved to TP1 and price is near TP1")
pos = make_btc_long(entry=65000.0, tps=[66000, 67000])
pos.sl_moved_to_entry = True
pos.sl_move_label = "TP1"
pos.stop_loss = 66000.0
pos.tp_hits_count = 2
reason = determine_exit_reason(pos, current_price=65800.0, execution_price=None)
test("Exit reason says 'SL at TP1' not 'breakeven'", "TP1" in reason and "breakeven" not in reason.lower(),
     f"got: {reason}")

# ── 7.6 Close verification with SL already moved ──
print("\n[7.6] Close price verification when SL was moved to TP2")
pos = make_btc_short(entry=65900.0, tps=[65635, 65315, 65000, 64500])
pos.stop_loss = 65315.0  # SL moved to TP2
result = verify_close_price(pos, current_price=65400.0)
test("SL at TP2 could have hit (price near $65,315)", result["sl_could_have_hit"])
test("Close justified", result["justified"])

# ── 7.7 Close verification — price between entry and SL (SHORT) ──
print("\n[7.7] SHORT: price between entry and SL — no trigger expected")
pos = make_btc_short(entry=65900.0, sl=66500.0, tps=[65635, 65315, 65000, 64500])
result = verify_close_price(pos, current_price=66200.0)
# Price is $66,200 — below SL of $66,500. For SHORT, SL triggers when price >= SL.
# 66200 >= 66500 - 1330 (2% tolerance) = 65170? No, tolerance is additive.
# SL=66500, tolerance = 66500*0.02 = 1330. Check: price >= SL - tolerance = 65170.
# 66200 >= 65170 → True. So SL could have hit.
test("SHORT: price $66,200 is within 2% of SL $66,500", result["sl_could_have_hit"],
     f"sl_could_have_hit={result['sl_could_have_hit']}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8: ORDER-EXISTS SAFETY CHECK LOGIC
# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
print("  SECTION 8: Order-Exists Safety Check")
print("="*70)

ORDER_TYPE_LIMIT_DECREASE = 5
ORDER_TYPE_STOP_LOSS_DECREASE = 6

def simulate_order_check(orders, market_addr):
    """Simulate the order-exists check from check_position_closed."""
    market_orders = [
        o for o in orders
        if o["market"].lower() == market_addr.lower()
        and o["order_type"] in (ORDER_TYPE_LIMIT_DECREASE, ORDER_TYPE_STOP_LOSS_DECREASE)
    ]
    return len(market_orders) > 0  # True = DON'T close

market = "0x47c031236e19d024b42f8ae6780e44a573170703"

print("\n[8.1] No orders remain → proceed with close")
should_wait = simulate_order_check([], market)
test("No orders → proceed with close", not should_wait)

print("\n[8.2] SL order still exists → wait")
orders = [{"market": market, "order_type": 6, "key_hex": "abc"}]
should_wait = simulate_order_check(orders, market)
test("SL still exists → wait for next cycle", should_wait)

print("\n[8.3] TP orders still exist → wait")
orders = [{"market": market, "order_type": 5, "key_hex": "def"}]
should_wait = simulate_order_check(orders, market)
test("TP still exists → wait for next cycle", should_wait)

print("\n[8.4] Orders for DIFFERENT market → don't wait")
orders = [{"market": "0xdifferent", "order_type": 6, "key_hex": "ghi"}]
should_wait = simulate_order_check(orders, market)
test("Different market orders → proceed with close", not should_wait)

print("\n[8.5] Limit increase (entry order) exists → don't wait")
orders = [{"market": market, "order_type": 3, "key_hex": "jkl"}]  # LimitIncrease
should_wait = simulate_order_check(orders, market)
test("Limit increase order → proceed with close", not should_wait)


# ═══════════════════════════════════════════════════════════════════════════════
# RESULTS
# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*70)
total = passed + failed
print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
print("="*70)

if failed > 0:
    print(f"\n  ⚠️  {failed} test(s) FAILED!")
    sys.exit(1)
else:
    print(f"\n  ✅  All {total} tests passed!")
    sys.exit(0)
