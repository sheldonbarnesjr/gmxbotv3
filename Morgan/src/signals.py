"""
Breakout pattern detection.

5-step process:
  1. Prior move >= 30% over 5-60 trading days
  2. 10 SMA slope positive
  3. 20 SMA slope positive
  4. Consolidation: pullback near SMA, tightening range, higher lows
  5. Volume declining during consolidation, breakout on above-average volume
"""
import pandas as pd

from .config import (
    PRIOR_MOVE_PCT, PRIOR_MOVE_WINDOW, CONSOLIDATION_MIN_BARS,
    VOLUME_DRY_UP_RATIO, BREAKOUT_VOL_RATIO,
)
from .scanner import passes_universe_filters


def _check_prior_move(df: pd.DataFrame, i: int) -> int | None:
    """
    Step 1: Look for a >= 30% move in the 5-60 day window before consolidation.
    Returns the index where the move started, or None.
    """
    for lookback in range(PRIOR_MOVE_WINDOW[1], PRIOR_MOVE_WINDOW[0] - 1, -5):
        if i - lookback < 0:
            continue
        window = df.iloc[i - lookback:i - CONSOLIDATION_MIN_BARS]
        if window.empty:
            continue
        past_low = window["Low"].min()
        recent_high = window["High"].max()
        if past_low > 0 and (recent_high / past_low - 1) >= PRIOR_MOVE_PCT:
            return i - lookback
    return None


def _check_sma_slopes(df: pd.DataFrame, i: int) -> bool:
    """Step 2 & 3: Both 10 and 20 SMA must be inclining (compare vs 5 bars ago)."""
    if i < 5:
        return False
    sma10_now = df.iloc[i]["SMA10"]
    sma10_prev = df.iloc[i - 5]["SMA10"]
    sma20_now = df.iloc[i]["SMA20"]
    sma20_prev = df.iloc[i - 5]["SMA20"]
    if pd.isna(sma10_now) or pd.isna(sma20_now):
        return False
    return sma10_now > sma10_prev and sma20_now > sma20_prev


def _check_consolidation(df: pd.DataFrame, i: int, move_start_idx: int) -> bool:
    """
    Step 4: Orderly consolidation near the 10/20 SMA.
    - Price within 5% of 10 or 20 SMA (or recently touched it)
    - Higher lows pattern (at least 50% of bars)
    - ATR decreasing or Bollinger bandwidth narrowing
    """
    row = df.iloc[i]
    close = row["Close"]

    # Price near SMA (current bar or recent 3 bars)
    near_sma = False
    for back in range(0, min(4, i + 1)):
        bar = df.iloc[i - back]
        if pd.isna(bar["SMA10"]) or pd.isna(bar["SMA20"]):
            continue
        if (abs(bar["Close"] - bar["SMA10"]) / bar["SMA10"] < 0.05 or
                abs(bar["Close"] - bar["SMA20"]) / bar["SMA20"] < 0.05 or
                (bar["Low"] <= bar["SMA10"] <= bar["High"]) or
                (bar["Low"] <= bar["SMA20"] <= bar["High"])):
            near_sma = True
            break
    if not near_sma:
        return False

    # Higher lows in consolidation zone
    consol_bars = min(10, i - move_start_idx)
    if consol_bars < CONSOLIDATION_MIN_BARS:
        return False
    recent_lows = df.iloc[i - consol_bars:i + 1]["Low"].values
    higher_lows = sum(1 for j in range(1, len(recent_lows)) if recent_lows[j] >= recent_lows[j - 1])
    if higher_lows < len(recent_lows) * 0.5:
        return False

    # Range tightening: ATR decreasing or BB width shrinking
    if not pd.isna(row.get("ATR14", float("nan"))):
        if i >= 10:
            atr_now = row["ATR14"]
            atr_prev = df.iloc[i - 10]["ATR14"]
            if not pd.isna(atr_prev) and atr_prev > 0:
                # ATR should be decreasing or stable — not expanding wildly
                if atr_now / atr_prev > 1.3:
                    return False

    return True


def _check_volume_pattern(df: pd.DataFrame, i: int, move_start_idx: int, consol_bars: int) -> bool:
    """Step 5a: Volume declining during consolidation."""
    move_end = max(move_start_idx + 1, i - consol_bars)
    move_vol = df.iloc[move_start_idx:move_end]["Volume"].mean()
    consol_vol = df.iloc[i - consol_bars:i]["Volume"].mean()
    if move_vol > 0 and consol_vol / move_vol > VOLUME_DRY_UP_RATIO:
        return False
    return True


def _check_breakout_volume(df: pd.DataFrame, i: int) -> bool:
    """Step 5b: Breakout bar has above-average volume."""
    row = df.iloc[i]
    if pd.isna(row["Vol_SMA20"]) or row["Vol_SMA20"] <= 0:
        return False
    return row["Volume"] >= row["Vol_SMA20"] * BREAKOUT_VOL_RATIO


def detect_breakouts(df: pd.DataFrame, start_date: str) -> list[dict]:
    """
    Scan daily bars for breakout setups (bull flag / consolidation breakout).
    Returns list of signal dicts: {date, entry_price, stop_price, risk_pct, bar_idx}.
    """
    signals = []
    start_ts = pd.Timestamp(start_date)

    if len(df) < 100:
        return signals

    for i in range(80, len(df)):
        date = df.index[i]
        if date < start_ts:
            continue

        if not passes_universe_filters(df, i):
            continue

        row = df.iloc[i]

        # Step 1: Prior move
        move_start = _check_prior_move(df, i)
        if move_start is None:
            continue

        # Steps 2-3: SMA slopes
        if not _check_sma_slopes(df, i):
            continue

        # Step 4: Consolidation
        if not _check_consolidation(df, i, move_start):
            continue

        # Step 5a: Volume drying up in consolidation
        consol_bars = min(10, i - move_start)
        if not _check_volume_pattern(df, i, move_start, consol_bars):
            continue

        # Price must break above consolidation high
        consol_high = df.iloc[i - consol_bars:i]["High"].max()
        if row["Close"] <= consol_high:
            continue

        # Step 5b: Breakout volume
        if not _check_breakout_volume(df, i):
            continue

        # Build signal
        entry_price = row["Close"]
        stop_price = row["Low"]  # Low of Day

        if stop_price >= entry_price:
            continue
        risk_pct = (entry_price - stop_price) / entry_price
        if risk_pct > 0.10:  # skip if stop is > 10% away
            continue

        signals.append({
            "date": date,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "risk_pct": risk_pct,
            "bar_idx": i,
        })

    return signals
