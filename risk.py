"""
Risk management for GMX V2 Trading Bot.

Pure functions — inputs → outputs, no global state.
Handles position sizing, portfolio % logic, max risk checks,
SL/TP validation, and "should trade?" guards.
"""

import logging
import re
from typing import Optional, List, Tuple

logger = logging.getLogger("GMXBot.risk")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Position sizing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cap_leverage(leverage: float, max_leverage: float) -> float:
    """Clamp leverage to the maximum allowed."""
    return min(leverage, max_leverage)


def calculate_position_size(
    total_portfolio: float,
    portfolio_pct: float,
    leverage: float,
) -> Tuple[float, float]:
    """Calculate collateral and notional size for a new position.

    Returns:
        (collateral_usd, size_usd)
    """
    collateral_usd = total_portfolio * portfolio_pct
    size_usd = collateral_usd * leverage
    return collateral_usd, size_usd


def check_min_collateral(
    collateral_usd: float,
    min_position_usd: float,
    portfolio_pct: float,
    total_portfolio: float,
) -> Optional[str]:
    """Return an error message if collateral is below minimum, else None."""
    if collateral_usd < min_position_usd:
        return (
            f"collateral ${collateral_usd:.2f} "
            f"({portfolio_pct:.0%} of ${total_portfolio:.2f} portfolio) "
            f"too small (min ${min_position_usd:.0f})"
        )
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Signal validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def validate_sl_required(
    stop_loss: Optional[float],
    require_sl: bool,
) -> Optional[str]:
    """Return error message if SL is required but missing."""
    if require_sl and (stop_loss is None or stop_loss <= 0):
        return "no stop loss"
    return None


def validate_tp_required(
    take_profits: list,
    require_tp: bool,
) -> Optional[str]:
    """Return error message if TP is required but missing."""
    if require_tp and not take_profits:
        return "no take profit"
    return None


def check_price_deviation(
    current_price: float,
    entry_mid: float,
    max_deviation: float = 0.10,
) -> Tuple[bool, float]:
    """Check if current price deviates too far from signal entry.

    Returns:
        (should_reject, deviation_fraction)
    """
    if entry_mid <= 0:
        return False, 0.0
    deviation = abs(current_price - entry_mid) / entry_mid
    return deviation > max_deviation, deviation


def validate_sl_tp_direction(
    is_long: bool,
    stop_loss: Optional[float],
    entry_low: float,
    entry_high: float,
    take_profits: list,
) -> Optional[str]:
    """Validate SL/TP prices are on correct side of entry.

    Returns error message string, or None if valid.
    """
    if is_long:
        if stop_loss and stop_loss >= entry_low:
            return "LONG SL must be below entry"
        for tp in take_profits:
            price = tp.price if hasattr(tp, "price") else tp
            if price <= entry_high:
                return f"LONG TP ${price:,.0f} must be above entry"
    else:
        if stop_loss and stop_loss <= entry_high:
            return "SHORT SL must be above entry"
        for tp in take_profits:
            price = tp.price if hasattr(tp, "price") else tp
            if price >= entry_low:
                return f"SHORT TP ${price:,.0f} must be below entry"
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Update / status message detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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

# Pre-compile for speed
_COMPILED_UPDATE_PATTERNS = [re.compile(p) for p in _UPDATE_PATTERNS]


def is_update_message(text: str) -> bool:
    """Return True if the text looks like a channel update (not a new signal).

    These are TP hit announcements, SL moves, position closed notices, etc.
    """
    lower = text.lower()
    for pat in _COMPILED_UPDATE_PATTERNS:
        if pat.search(lower):
            return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Exit reason classification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def classify_exit_reason(
    *,
    is_long: bool,
    current_price: Optional[float],
    stop_loss: Optional[float],
    tp_hits_count: int,
    last_known_tp_count: int,
    sl_moved_to_entry: bool,
    sl_move_label: Optional[str],
    sl_orders_remaining: int = -1,
    tp_orders_remaining: int = -1,
) -> str:
    """Determine exit reason from position state. Pure function.

    When sl_orders_remaining / tp_orders_remaining are provided (>= 0),
    we can distinguish liquidation from SL/TP fills:
      - If SL and TP orders are STILL on-chain when position disappears,
        neither was triggered -> liquidation.
      - If SL orders are gone but TPs remain -> SL triggered.
      - If all TP orders are gone -> all TPs filled.
    """

    if tp_hits_count > 0 and last_known_tp_count == 0:
        return "All TPs filled"

    # ── Liquidation detection ──
    # If we have on-chain order counts AND both SL + TP orders still exist,
    # neither was triggered — the position was forcibly closed (liquidated).
    if sl_orders_remaining >= 0 and tp_orders_remaining >= 0:
        if sl_orders_remaining > 0 and tp_orders_remaining > 0 and tp_hits_count == 0:
            return "Liquidation"
        # SL orders still present but no TPs -> all TPs filled (orders may lag)
        if sl_orders_remaining > 0 and tp_orders_remaining == 0 and tp_hits_count > 0:
            return "All TPs filled"
        # No SL orders remain and TPs still present -> SL was triggered
        if sl_orders_remaining == 0 and tp_orders_remaining > 0:
            if sl_moved_to_entry and stop_loss:
                sl_label = sl_move_label or "Entry"
                if sl_label == "Entry":
                    return "SL (breakeven)"
                return f"SL at {sl_label} (${stop_loss:,.2f})"
            return "SL triggered"

    if sl_moved_to_entry and stop_loss:
        sl_label = sl_move_label or "Entry"
        sl_price = stop_loss

        # Is exit price near the SL price? (2% tolerance)
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
            return f"SL at {sl_label} (${sl_price:,.2f})"

        if tp_hits_count > 0:
            return f"TP/SL hit ({tp_hits_count} TPs filled)"
        return "SL/TP hit"

    if tp_hits_count > 0:
        return f"Closed ({tp_hits_count} TPs hit)"
    if last_known_tp_count > 0:
        return "SL/TP/liquidation"
    return "SL/liquidation"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PnL calculation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def calculate_unrealized_pnl(
    side: str,
    entry_price: float,
    current_price: float,
    size_usd: float,
) -> float:
    """Calculate unrealized PnL for a position.

    size_usd already includes leverage — don't multiply again.
    """
    if entry_price <= 0:
        return 0.0
    if side == "LONG":
        change = current_price - entry_price
    else:
        change = entry_price - current_price
    return (change / entry_price) * size_usd


def calculate_pnl_percentage(
    unrealized_pnl: float,
    size_usd: float,
    leverage: float,
) -> float:
    """PnL as percentage of collateral (what was actually deposited)."""
    collateral = size_usd / leverage if leverage else 0.0
    if collateral == 0:
        return 0.0
    return (unrealized_pnl / collateral) * 100


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TP hit price verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def verify_tp_hit_by_price(
    is_long: bool,
    tp_price: float,
    current_price: float,
    tolerance_pct: float = 0.0015,
) -> bool:
    """Check if current price has reached a TP level (within tolerance).

    tolerance_pct=0.15% — just enough slack for oracle/feed lag without
    triggering false TP-hit notifications.  The previous 3% default was
    far too loose (BTC TP at $100k would trigger at $97k).
    """
    tol = tp_price * tolerance_pct
    if is_long:
        return current_price >= tp_price - tol
    return current_price <= tp_price + tol


def determine_new_sl_target(
    tp_hits_count: int,
    entry_price: float,
    sorted_tps: list,
) -> Tuple[Optional[float], Optional[str]]:
    """Determine where SL should move after TP hit(s).

    Trailing strategy (lets positions run):
      TP1 hit → SL to Entry (breakeven)
      TP2 hit → no move (stay at Entry)
      TP3 hit → SL to TP1
      TP4+ hit → SL stays at TP2

    Returns (new_sl_price, sl_label), or (None, None) if no move needed.
    """
    def _tp_price(idx):
        tp = sorted_tps[idx]
        return tp.price if hasattr(tp, "price") else tp

    if tp_hits_count <= 0:
        return None, None

    if tp_hits_count == 1:
        return entry_price, "Entry"

    if tp_hits_count == 2:
        # No move — let it run, SL stays at Entry
        return None, None

    if tp_hits_count == 3:
        # SL to TP1
        if len(sorted_tps) >= 1:
            return _tp_price(0), "TP1"
        return entry_price, "Entry"

    # TP4+ → SL to TP2 (and stays there)
    if len(sorted_tps) >= 2:
        return _tp_price(1), "TP2"
    if len(sorted_tps) >= 1:
        return _tp_price(0), "TP1"
    return entry_price, "Entry"
