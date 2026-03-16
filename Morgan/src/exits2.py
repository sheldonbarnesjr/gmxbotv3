"""
Exit logic (v2): stop loss, partial profit, 10 SMA trailing exit.

PDF Sell Rules:
  1. Stop Loss Hit → auto-sell 100% at LOD stop (market order)
  2. Up 5x+ risk → sell 10-30% before close, move stop to breakeven
  3. Close below 10 SMA → sell 100% of remaining position
  4. Up < 5x risk → hold, do NOT take profits early
"""
import pandas as pd

from .config2 import PARTIAL_SELL_PCT, PARTIAL_SELL_THRESHOLD, SLIPPAGE_PCT


def check_stop_hit(bar_low: float, current_stop: float) -> bool:
    """Returns True if intraday low breached the stop price."""
    return bar_low <= current_stop


def get_stop_exit_price(bar_open: float, current_stop: float) -> float:
    """
    Determine stop exit price. If bar opens below stop (gap down),
    exit at open (slippage already accounts for gap). Otherwise exit at stop.
    """
    if bar_open <= current_stop:
        return bar_open  # gapped through stop
    return current_stop


def check_partial_profit(unrealized_r: float, partial_taken: bool) -> bool:
    """Returns True if we should take partial profits (5x+ risk, not yet taken)."""
    return not partial_taken and unrealized_r >= PARTIAL_SELL_THRESHOLD


def execute_partial(
    remaining_shares: float,
    entry_price: float,
    current_price: float,
) -> tuple[float, float, float]:
    """
    Execute partial profit taking: sell 20% at current price, move stop to breakeven.

    Returns: (new_remaining_shares, realized_partial_pnl, new_stop_price)
    """
    sell_shares = remaining_shares * PARTIAL_SELL_PCT
    sell_price = current_price * (1 - SLIPPAGE_PCT)
    partial_pnl = sell_shares * (sell_price - entry_price)
    new_remaining = remaining_shares - sell_shares
    new_stop = entry_price  # move to breakeven
    return new_remaining, partial_pnl, new_stop


def check_10sma_close(bar_close: float, sma10: float) -> bool:
    """
    Returns True if the daily close is below the 10 SMA.
    PDF: "Sell FULL position when stock CLOSES BELOW the 10 SMA on Daily chart"
    """
    if pd.isna(sma10):
        return False
    return bar_close < sma10
