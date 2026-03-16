"""
Entry logic (v2): next-day open entry with gap filters and breakout volume confirmation.

Flow:
  1. Setup detected at close of Day N (via signals2.scan_setups)
  2. On Day N+1, check:
     a. Open gaps above consolidation high → breakout trigger
     b. Gap-down filter: skip if open < prev_close * (1 - 2%)
     c. Gap-up filter: skip if open > prev_close * (1 + 10%)
     d. Volume confirmation: Day N+1 volume > 150% of 20-day avg
     e. Stop width: skip if (entry - LOD) / entry > 8%
  3. Entry price = Open of Day N+1
  4. Stop loss = Low of Day N+1

Since we only know Day N+1's low at end of day, we simulate the stop
being set at the low of the entry day (a slight simplification for daily data).
"""
from .config2 import (
    MAX_GAP_DOWN_PCT, MAX_GAP_UP_PCT, MAX_STOP_WIDTH_PCT,
    BREAKOUT_VOL_RATIO, SLIPPAGE_PCT,
)


def check_breakout_trigger(
    setup: dict,
    prev_close: float,
    next_open: float,
    next_high: float,
    next_low: float,
    next_close: float,
    next_volume: float,
    vol_sma20: float,
) -> dict | None:
    """
    Check if Day N+1 triggers a valid breakout entry.

    Args:
        setup: setup dict from signals2.scan_setups
        prev_close: close of Day N (setup day)
        next_open: open of Day N+1
        next_high: high of Day N+1
        next_low: low of Day N+1
        next_close: close of Day N+1
        next_volume: volume of Day N+1
        vol_sma20: 20-day volume average as of Day N

    Returns:
        Entry dict {entry_price, stop_price, risk_pct} or None if rejected.
    """
    consol_high = setup["consol_high"]

    # ── Gap filters ──
    if prev_close > 0:
        gap_pct = (next_open - prev_close) / prev_close
        # Skip gap down > 2%
        if gap_pct < -MAX_GAP_DOWN_PCT:
            return None
        # Skip gap up > 10% (overextended opening)
        if gap_pct > MAX_GAP_UP_PCT:
            return None

    # ── Breakout trigger: price must trade above consolidation high ──
    # Either opens above it, or trades through it during the day
    if next_high < consol_high:
        return None  # never broke out

    # Entry price: use open if it's already above consolidation high,
    # otherwise use consolidation high (simulating a buy-stop order)
    if next_open >= consol_high:
        entry_price = next_open
    else:
        entry_price = consol_high

    # Apply slippage (long entry slips up)
    entry_price *= (1 + SLIPPAGE_PCT)

    # ── Stop loss = Low of Day N+1 ──
    stop_price = next_low

    # Sanity: stop must be below entry
    if stop_price >= entry_price:
        return None

    # ── Stop width filter: skip if stop > 8% from entry ──
    risk_pct = (entry_price - stop_price) / entry_price
    if risk_pct > MAX_STOP_WIDTH_PCT:
        return None

    # ── Volume confirmation: breakout day volume > 150% of 20-day avg ──
    if vol_sma20 > 0 and next_volume < vol_sma20 * BREAKOUT_VOL_RATIO:
        return None

    return {
        "entry_price": round(entry_price, 4),
        "stop_price": round(stop_price, 4),
        "risk_pct": round(risk_pct, 4),
    }


def apply_exit_slippage(exit_price: float) -> float:
    """Apply slippage to exit price (long exits slip down)."""
    return exit_price * (1 - SLIPPAGE_PCT)
