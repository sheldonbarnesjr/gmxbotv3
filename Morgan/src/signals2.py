"""
Breakout pattern detection (v2).

PDF 5-step breakout identification process:
  Step 1: A big move up of 30%+ over multiple days to weeks (not single-day pump)
  Step 2: 10/20 SMA must be inclining (upward slope)
  Step 3: Orderly pullback to 10/20 SMA with higher lows AND lower highs (tightening range)
  Step 4: Volume drying up as stock pulls back (< 70% of 20-day average, declining)
  Step 5: Breakout of range on HIGH VOLUME (> 150% of 20-day average)

This module detects the SETUP (steps 1-4) on Day N.
The actual breakout trigger (step 5) is checked on Day N+1 in entries2.py.
This separation avoids lookahead bias.
"""
import numpy as np
import pandas as pd

from .config2 import (
    PRIOR_MOVE_PCT, PRIOR_MOVE_WINDOW, CONSOLIDATION_MIN_BARS,
    CONSOLIDATION_MAX_BARS, VOLUME_CONTRACTION_RATIO,
)
from .scanner2 import passes_universe_filters


def _find_prior_move(df: pd.DataFrame, i: int) -> int | None:
    """
    Step 1: Look for a >= 30% move over multiple days (not single-day pump).
    Scans the window [i-60, i-3] for a low-to-high gain >= 30%.
    Returns the index where the move started, or None.
    """
    min_window, max_window = PRIOR_MOVE_WINDOW
    for lookback in range(max_window, min_window - 1, -5):
        if i - lookback < 0:
            continue
        start = i - lookback
        end = i - CONSOLIDATION_MIN_BARS
        if end <= start:
            continue
        window = df.iloc[start:end]
        if window.empty:
            continue

        past_low = window["Low"].min()
        recent_high = window["High"].max()
        if past_low > 0 and (recent_high / past_low - 1) >= PRIOR_MOVE_PCT:
            # Verify it's not a single-day pump: the high and low should be
            # on different days (multi-day move)
            low_idx = window["Low"].idxmin()
            high_idx = window["High"].idxmax()
            if low_idx != high_idx:
                return start
    return None


def _check_sma_inclining(df: pd.DataFrame, i: int) -> bool:
    """
    Step 2 & 3: Both 10 SMA and 20 SMA must have positive slope.
    Compare current value vs 5 bars ago.
    """
    if i < 5:
        return False
    row = df.iloc[i]
    prev = df.iloc[i - 5]
    for col in ("SMA10", "SMA20"):
        if pd.isna(row[col]) or pd.isna(prev[col]):
            return False
        if row[col] <= prev[col]:
            return False
    return True


def _detect_consolidation(df: pd.DataFrame, i: int, move_start: int) -> dict | None:
    """
    Step 3: Orderly pullback with HIGHER LOWS + LOWER HIGHS (tightening range).

    Returns a dict with consolidation info, or None if pattern not found:
      {consol_bars, consol_high, consol_low, higher_lows_pct, lower_highs_pct}
    """
    row = df.iloc[i]

    # Price must be near 10 or 20 SMA (within 5%, or bar touches SMA)
    near_sma = False
    for back in range(0, min(4, i + 1)):
        bar = df.iloc[i - back]
        if pd.isna(bar.get("SMA10")) or pd.isna(bar.get("SMA20")):
            continue
        if (abs(bar["Close"] - bar["SMA10"]) / bar["SMA10"] < 0.05 or
                abs(bar["Close"] - bar["SMA20"]) / bar["SMA20"] < 0.05 or
                bar["Low"] <= bar["SMA10"] <= bar["High"] or
                bar["Low"] <= bar["SMA20"] <= bar["High"]):
            near_sma = True
            break
    if not near_sma:
        return None

    # Determine consolidation window
    consol_bars = min(CONSOLIDATION_MAX_BARS, i - move_start)
    if consol_bars < CONSOLIDATION_MIN_BARS:
        return None

    consol_slice = df.iloc[i - consol_bars:i + 1]
    lows = consol_slice["Low"].values
    highs = consol_slice["High"].values

    if len(lows) < CONSOLIDATION_MIN_BARS:
        return None

    # Count higher lows (each bar's low >= previous bar's low)
    higher_lows = sum(1 for j in range(1, len(lows)) if lows[j] >= lows[j - 1] * 0.998)
    higher_lows_pct = higher_lows / (len(lows) - 1)

    # Count lower highs (each bar's high <= previous bar's high)
    lower_highs = sum(1 for j in range(1, len(highs)) if highs[j] <= highs[j - 1] * 1.002)
    lower_highs_pct = lower_highs / (len(highs) - 1)

    # Need at least 40% higher lows AND 40% lower highs for tightening
    if higher_lows_pct < 0.40 or lower_highs_pct < 0.35:
        return None

    return {
        "consol_bars": consol_bars,
        "consol_high": float(highs[:-1].max()),  # exclude current bar
        "consol_low": float(lows.min()),
        "higher_lows_pct": round(higher_lows_pct, 2),
        "lower_highs_pct": round(lower_highs_pct, 2),
    }


def _check_volume_contraction(df: pd.DataFrame, i: int, consol_bars: int, move_start: int) -> bool:
    """
    Step 4: Volume drying up during consolidation.

    The PDF says "volume drying up as stock pulls back" — this means
    consolidation volume should be noticeably lower than the volume during
    the prior move (the strong leg up). We compare:
      - Average consolidation volume vs average move volume (< 70%)
      - Volume should be declining within the consolidation (second half < first half)
    """
    # Calculate average volume during the prior move (strong leg)
    move_end = max(move_start + 1, i - consol_bars)
    if move_end <= move_start:
        return False
    move_vol = df.iloc[move_start:move_end]["Volume"].values
    avg_move_vol = np.mean(move_vol)
    if avg_move_vol <= 0:
        return False

    # Average volume during consolidation
    consol_vol = df.iloc[i - consol_bars:i]["Volume"].values
    if len(consol_vol) == 0:
        return False
    avg_consol_vol = np.mean(consol_vol)

    # Consolidation volume < 70% of move volume
    if avg_consol_vol / avg_move_vol > VOLUME_CONTRACTION_RATIO:
        return False

    # Volume should be declining within consolidation (second half < first half)
    if len(consol_vol) >= 4:
        mid = len(consol_vol) // 2
        first_half = np.mean(consol_vol[:mid])
        second_half = np.mean(consol_vol[mid:])
        if first_half > 0 and second_half / first_half > 1.3:
            return False  # volume increasing, not drying up

    return True


def scan_setups(df: pd.DataFrame, start_date: str) -> list[dict]:
    """
    Scan daily bars for breakout SETUPS (steps 1-4 complete).

    The setup is detected at the close of Day N. The actual breakout trigger
    (step 5: price breaks consolidation high on high volume) is checked
    separately on Day N+1 to avoid lookahead bias.

    Returns list of setup dicts:
        {date, bar_idx, consol_high, consol_low, move_start_idx}
    """
    setups = []
    start_ts = pd.Timestamp(start_date)

    if len(df) < 100:
        return setups

    for i in range(80, len(df) - 1):  # -1 because we need Day N+1 for entry
        date = df.index[i]
        if date < start_ts:
            continue

        # Universe filters
        if not passes_universe_filters(df, i):
            continue

        # Step 1: Prior move >= 30%
        move_start = _find_prior_move(df, i)
        if move_start is None:
            continue

        # Step 2-3: SMA slopes positive
        if not _check_sma_inclining(df, i):
            continue

        # Step 3: Consolidation with higher lows + lower highs
        consol = _detect_consolidation(df, i, move_start)
        if consol is None:
            continue

        # Step 4: Volume drying up
        if not _check_volume_contraction(df, i, consol["consol_bars"], move_start):
            continue

        setups.append({
            "date": date,
            "bar_idx": i,
            "consol_high": consol["consol_high"],
            "consol_low": consol["consol_low"],
            "consol_bars": consol["consol_bars"],
            "higher_lows_pct": consol["higher_lows_pct"],
            "lower_highs_pct": consol["lower_highs_pct"],
            "move_start_idx": move_start,
        })

    return setups
