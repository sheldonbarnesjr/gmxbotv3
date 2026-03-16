"""
Stock universe filtering, indicator computation, and relative strength ranking (v2).

Filters (PDF Section 9 — Stock Universe Filters):
  - Price > $1.00
  - ADR% > 5% (20-day average daily range percentage)
  - Average Daily Dollar Volume > $3.5M
  - Top 2% relative strength over 1M, 3M, or 6M lookback
"""
import numpy as np
import pandas as pd

from .config2 import MIN_PRICE, MIN_ADR_PCT, MIN_DAILY_DOLLAR_VOL, RS_TOP_PCT


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add all technical indicators needed by the strategy:
    - SMA 10, 20, 50, 200
    - ADR% (20-day average)
    - Dollar volume & 20-day average
    - Volume SMA 20
    - ATR 14
    """
    df = df.copy()
    c = df["Close"]

    # Moving averages
    df["SMA10"] = c.rolling(10).mean()
    df["SMA20"] = c.rolling(20).mean()
    df["SMA50"] = c.rolling(50).mean()
    df["SMA200"] = c.rolling(200).mean()

    # ADR% — average daily range as percentage of close
    df["ADR_pct"] = ((df["High"] - df["Low"]) / c).rolling(20).mean() * 100

    # Dollar volume
    df["Dollar_Vol"] = c * df["Volume"]
    df["Avg_Dollar_Vol"] = df["Dollar_Vol"].rolling(20).mean()

    # Volume average
    df["Vol_SMA20"] = df["Volume"].rolling(20).mean()

    # ATR 14 for volatility measurement
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - c.shift(1)).abs(),
        (df["Low"] - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["ATR14"] = tr.rolling(14).mean()

    return df


def passes_universe_filters(df: pd.DataFrame, idx: int) -> bool:
    """
    Check if a stock passes the basic universe filters at a given bar index.
    PDF: Price > $1, ADR% > 5%, Avg Daily $ Vol > $3.5M
    """
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
    Rank all tickers by average return over 1M (21d), 3M (63d), 6M (126d).
    Returns the top `top_pct` (default 2%) as a set of ticker strings.

    Only uses data available up to `date` — no lookahead bias.
    A ticker must have data for ALL three periods to be ranked.
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
                past_close = df.iloc[idx - period]["Close"]
                if past_close > 0:
                    ret = (df.iloc[idx]["Close"] / past_close) - 1
                    returns.append(ret)
        if len(returns) == len(periods):
            scores[ticker] = np.mean(returns)

    if not scores:
        return set()

    sorted_tickers = sorted(scores.keys(), key=lambda t: scores[t], reverse=True)
    top_n = max(1, int(len(sorted_tickers) * top_pct))
    return set(sorted_tickers[:top_n])
