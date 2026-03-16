"""
Market regime filter based on NASDAQ Composite ($IXIC).

BULL: 10 SMA > 20 SMA → take all signals
BEAR: 10 SMA < 20 SMA → skip signals (or reduce size by 50%)
"""
import pandas as pd


def compute_market_regime(ixic_df: pd.DataFrame) -> pd.Series:
    """
    Returns a Series indexed by date with values 'BULL' or 'BEAR'.
    """
    sma10 = ixic_df["Close"].rolling(10).mean()
    sma20 = ixic_df["Close"].rolling(20).mean()
    regime = pd.Series("BEAR", index=ixic_df.index)
    regime[sma10 > sma20] = "BULL"
    return regime


def get_regime_at_date(regime: pd.Series, date: pd.Timestamp) -> str:
    """Look up regime for a given date, forward-filling gaps."""
    if date in regime.index:
        return regime.loc[date]
    if len(regime) == 0:
        return "UNKNOWN"
    idx = regime.index.get_indexer([date], method="ffill")[0]
    if idx >= 0:
        return regime.iloc[idx]
    return "UNKNOWN"


def regime_summary(regime: pd.Series) -> dict:
    """Return summary stats about bull/bear periods."""
    total = len(regime.dropna())
    bull = (regime == "BULL").sum()
    return {
        "total_days": total,
        "bull_days": int(bull),
        "bear_days": int(total - bull),
        "bull_pct": round(bull / total * 100, 1) if total > 0 else 0,
    }
