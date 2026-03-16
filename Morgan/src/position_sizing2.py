"""
Risk-based position sizing (v2).

PDF Section 6:
  - Maximum risk per trade: 1% of total account value
  - Shares to Buy = Risk Amount / (Entry Price - Stop Price)
  - Position size range: 10-40% of account
  - Typical stop width: 2-5% from entry
  - Tighter stops = larger position sizes with same dollar risk
"""
from .config2 import RISK_PER_TRADE, MAX_POSITION_PCT


def calculate_position(
    account_value: float,
    entry_price: float,
    stop_price: float,
) -> tuple[float, float, float]:
    """
    Calculate position size based on 1% risk rule.

    Returns: (shares, position_value, risk_amount)
    """
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return 0.0, 0.0, 0.0

    risk_amount = account_value * RISK_PER_TRADE
    shares = risk_amount / risk_per_share
    position_value = shares * entry_price

    # Cap position at 40% of account
    max_position = account_value * MAX_POSITION_PCT
    if position_value > max_position:
        position_value = max_position
        shares = position_value / entry_price
        risk_amount = shares * risk_per_share  # actual risk reduced

    return round(shares, 2), round(position_value, 2), round(risk_amount, 2)
