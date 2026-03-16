"""
Risk-based position sizing.

Rules:
  - Risk 1% of account per trade
  - Position capped at 40% of account value
  - shares = risk_amount / risk_per_share
"""
from .config import RISK_PER_TRADE, MAX_POSITION_PCT


def calculate_position(
    account_value: float,
    entry_price: float,
    stop_price: float,
    max_risk_pct: float = RISK_PER_TRADE,
    max_position_pct: float = MAX_POSITION_PCT,
) -> tuple[float, float, float]:
    """
    Calculate position size based on risk.

    Returns:
        (shares, position_value, risk_amount)
    """
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return 0.0, 0.0, 0.0

    risk_amount = account_value * max_risk_pct
    shares = risk_amount / risk_per_share
    position_value = shares * entry_price

    # Cap position at max_position_pct of account
    max_position = account_value * max_position_pct
    if position_value > max_position:
        position_value = max_position
        shares = position_value / entry_price
        # Actual risk is now less than 1%
        risk_amount = shares * risk_per_share

    return round(shares, 2), round(position_value, 2), round(risk_amount, 2)
