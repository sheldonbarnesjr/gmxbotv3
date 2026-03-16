"""
Stock universe filtering & relative strength ranking.

Filters:
  - Price > $1.00
  - ADR% > 5% (20-day average)
  - Average Daily Dollar Volume > $3.5M (20-day average)
  - Top 2% relative strength over 1M, 3M, or 6M lookback
"""
import numpy as np
import pandas as pd

from .config import MIN_PRICE, MIN_ADR_PCT, MIN_DAILY_DOLLAR_VOL, RS_TOP_PCT


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add SMA, ADR%, dollar volume, and volume averages."""
    df = df.copy()
    df["SMA10"] = df["Close"].rolling(10).mean()
    df["SMA20"] = df["Close"].rolling(20).mean()
    df["SMA50"] = df["Close"].rolling(50).mean()
    df["SMA200"] = df["Close"].rolling(200).mean()
    df["ADR_pct"] = ((df["High"] - df["Low"]) / df["Close"]).rolling(20).mean() * 100
    df["Dollar_Vol"] = df["Close"] * df["Volume"]
    df["Avg_Dollar_Vol"] = df["Dollar_Vol"].rolling(20).mean()
    df["Vol_SMA20"] = df["Volume"].rolling(20).mean()
    # ATR for consolidation detection
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift(1)).abs(),
        (df["Low"] - df["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["ATR14"] = tr.rolling(14).mean()
    # Bollinger bandwidth for tightening detection
    df["BB_mid"] = df["Close"].rolling(20).mean()
    bb_std = df["Close"].rolling(20).std()
    df["BB_upper"] = df["BB_mid"] + 2 * bb_std
    df["BB_lower"] = df["BB_mid"] - 2 * bb_std
    df["BB_width"] = (df["BB_upper"] - df["BB_lower"]) / df["BB_mid"]
    return df


def passes_universe_filters(df: pd.DataFrame, idx: int) -> bool:
    """Check if a stock passes the basic universe filters at bar index."""
    row = df.iloc[idx]
    if row["Close"] < MIN_PRICE:
        return False
    if pd.isna(row["ADR_pct"]) or row["ADR_pct"] < MIN_ADR_PCT:
        return False
    if pd.isna(row["Avg_Dollar_Vol"]) or row["Avg_Dollar_Vol"] < MIN_DAILY_DOLLAR_VOL:
        return False
    return True


def compute_relative_strength(
    data: dict[str, pd.DataFrame],
    date: pd.Timestamp,
    periods: list[int] | None = None,
    top_pct: float = RS_TOP_PCT,
) -> set[str]:
    """
    Rank tickers by average return over 1M (21d), 3M (63d), 6M (126d).
    Returns the top `top_pct` (default 2%) tickers.
    Uses only data available up to `date` — no lookahead.
    """
    if periods is None:
        periods = [21, 63, 126]

    scores = {}
    for ticker, df in data.items():
        if date not in df.index:
            continue
        idx = df.index.get_loc(date)
        if isinstance(idx, slice):
            idx = idx.start

        returns = []
        for period in periods:
            if idx >= period:
                ret = (df.iloc[idx]["Close"] / df.iloc[idx - period]["Close"]) - 1
                returns.append(ret)
        if len(returns) == len(periods):
            scores[ticker] = np.mean(returns)

    if not scores:
        return set()

    sorted_tickers = sorted(scores.keys(), key=lambda t: scores[t], reverse=True)
    top_n = max(1, int(len(sorted_tickers) * top_pct))
    return set(sorted_tickers[:top_n])
