"""
Entry logic: determines exact entry price with slippage.

For daily backtesting, entry is the breakout bar's close price
(simulating an ORB breakout confirmation intraday).
Slippage is applied on entry.
"""
from .config import SLIPPAGE_PCT


def apply_entry_slippage(entry_price: float, direction: str = "long") -> float:
    """Apply slippage to entry price. Long entries slip up, shorts slip down."""
    if direction == "long":
        return entry_price * (1 + SLIPPAGE_PCT)
    return entry_price * (1 - SLIPPAGE_PCT)


def apply_exit_slippage(exit_price: float, direction: str = "long") -> float:
    """Apply slippage to exit price. Long exits slip down, shorts slip up."""
    if direction == "long":
        return exit_price * (1 - SLIPPAGE_PCT)
    return exit_price * (1 + SLIPPAGE_PCT)
