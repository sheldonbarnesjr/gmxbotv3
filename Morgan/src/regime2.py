"""
Market regime filter (v2) based on NASDAQ Composite ($IXIC / $COMPQX).

PDF Section 8 — Market Condition Filters:
  BULLISH: 10 SMA ABOVE 20 SMA → Trade normally, full size
  BEARISH: 10 SMA BELOW 20 SMA → Either do not trade OR expect lower win rate

Alternative: Can also use $SPY or $QQQ daily charts.
"""
import pandas as pd


def compute_market_regime(ixic_df: pd.DataFrame) -> pd.Series:
    """
    Returns a Series indexed by date with values 'BULL' or 'BEAR'.
    BULL when 10 SMA > 20 SMA on NASDAQ Composite.
    """
    sma10 = ixic_df["Close"].rolling(10).mean()
    sma20 = ixic_df["Close"].rolling(20).mean()
    regime = pd.Series("BEAR", index=ixic_df.index, dtype="object")
    regime[sma10 > sma20] = "BULL"
    return regime


def get_regime_at_date(regime: pd.Series, date: pd.Timestamp) -> str:
    """Look up regime for a given date, forward-filling for weekends/holidays."""
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
    bull = int((regime == "BULL").sum())
    return {
        "total_days": total,
        "bull_days": bull,
        "bear_days": total - bull,
        "bull_pct": round(bull / total * 100, 1) if total > 0 else 0,
    }
