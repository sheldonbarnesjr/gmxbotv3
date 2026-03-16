"""
Exit logic: stop loss, partial profit taking, and trailing 10 SMA exit.

Rules:
  1. Stop Loss: exit 100% if low <= stop_price
  2. Partial Profit: sell 20% when unrealized >= 5x risk, move stop to breakeven
  3. Trailing: exit 100% if daily close < 10 SMA
"""
import pandas as pd

from .config import (
    PARTIAL_SELL_PCT, PARTIAL_SELL_THRESHOLD, MAX_HOLD_DAYS, SLIPPAGE_PCT,
)


def check_stop_loss(bar_low: float, current_stop: float) -> bool:
    """Returns True if the stop was hit (intraday low breached stop)."""
    return bar_low <= current_stop


def check_partial_profit(
    unrealized_r: float,
    partial_taken: bool,
    threshold: float = PARTIAL_SELL_THRESHOLD,
) -> bool:
    """Returns True if we should take partial profits."""
    return not partial_taken and unrealized_r >= threshold


def take_partial(
    remaining_shares: float,
    entry_price: float,
    current_price: float,
    sell_pct: float = PARTIAL_SELL_PCT,
) -> tuple[float, float, float]:
    """
    Execute partial profit taking.

    Returns:
        (new_remaining_shares, partial_pnl_realized, new_stop_price)
    """
    sell_shares = remaining_shares * sell_pct
    partial_pnl = sell_shares * (current_price * (1 - SLIPPAGE_PCT) - entry_price)
    new_remaining = remaining_shares - sell_shares
    new_stop = entry_price  # move to breakeven
    return new_remaining, partial_pnl, new_stop


def check_10sma_exit(df: pd.DataFrame, bar_idx: int) -> bool:
    """Returns True if bar's close is below the 10 SMA (trailing exit)."""
    start = max(0, bar_idx - 9)
    sma10 = df.iloc[start:bar_idx + 1]["Close"].mean()
    return df.iloc[bar_idx]["Close"] < sma10


def check_time_exit(holding_days: int, max_days: int = MAX_HOLD_DAYS) -> bool:
    """Returns True if max holding period is exceeded."""
    return holding_days >= max_days
