"""
Data pipeline (v2): fetches, caches, and manages OHLCV data.

- S&P 500 + NASDAQ 100 tickers from Wikipedia
- $IXIC for market regime
- Local parquet caching to avoid re-downloading
- Batch download with yfinance for speed
"""
import json
import time
from typing import Optional

import pandas as pd
import yfinance as yf

from .config2 import DATA_CACHE


# ─── Ticker Universe ────────────────────────────────────────────────────────

def get_sp500_tickers() -> list[str]:
    """Fetch S&P 500 tickers from Wikipedia."""
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        )
        return tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
    except Exception as e:
        print(f"  Warning: couldn't fetch S&P 500 list: {e}")
        return []


def get_nasdaq100_tickers() -> list[str]:
    """Fetch NASDAQ 100 tickers from Wikipedia."""
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
        for t in tables:
            if "Ticker" in t.columns:
                return t["Ticker"].str.replace(".", "-", regex=False).tolist()
            if "Symbol" in t.columns:
                return t["Symbol"].str.replace(".", "-", regex=False).tolist()
    except Exception as e:
        print(f"  Warning: couldn't fetch NASDAQ 100 list: {e}")
    return []


def build_universe() -> list[str]:
    """Build the full ticker universe from S&P 500 + NASDAQ 100."""
    print("Building ticker universe...")
    sp500 = get_sp500_tickers()
    print(f"  S&P 500: {len(sp500)} tickers")
    nasdaq100 = get_nasdaq100_tickers()
    print(f"  NASDAQ 100: {len(nasdaq100)} tickers")
    all_tickers = sorted(set(sp500 + nasdaq100))
    print(f"  Total unique: {len(all_tickers)} tickers")
    return all_tickers


# ─── Data Fetching ──────────────────────────────────────────────────────────

def fetch_single(ticker: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV for one ticker with local parquet caching."""
    safe_ticker = ticker.replace("/", "_").replace("^", "_")
    cache_file = DATA_CACHE / f"{safe_ticker}_{start}_{end}.parquet"

    if cache_file.exists():
        try:
            df = pd.read_parquet(cache_file)
            if len(df) > 0:
                return df
        except Exception:
            pass

    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        needed = ["Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in needed if c not in df.columns]
        if missing:
            return None
        df = df[needed].copy()
        df.dropna(inplace=True)
        if len(df) < 50:
            return None
        df.to_parquet(cache_file)
        return df
    except Exception:
        return None


def fetch_all(tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """
    Batch-download data for all tickers.
    Extends start date by 1 year for indicator warm-up (200 SMA needs ~250 bars).
    Uses yfinance batch download for speed + local parquet cache.
    """
    warmup_start = (pd.Timestamp(start) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")

    # Check cache first
    uncached = []
    data = {}
    for ticker in tickers:
        safe_ticker = ticker.replace("/", "_").replace("^", "_")
        cache_file = DATA_CACHE / f"{safe_ticker}_{warmup_start}_{end}.parquet"
        if cache_file.exists():
            try:
                df = pd.read_parquet(cache_file)
                if len(df) > 0:
                    data[ticker] = df
                    continue
            except Exception:
                pass
        uncached.append(ticker)

    if uncached:
        print(f"  Downloading {len(uncached)} tickers (cached: {len(data)})...")
        batch_size = 50
        for i in range(0, len(uncached), batch_size):
            batch = uncached[i:i + batch_size]
            pct = min(100, int((i + len(batch)) / len(uncached) * 100))
            print(f"    Batch {i // batch_size + 1} "
                  f"({len(batch)} tickers, {pct}% done)...")
            try:
                batch_str = " ".join(batch)
                batch_data = yf.download(
                    batch_str, start=warmup_start, end=end,
                    progress=False, auto_adjust=True,
                    group_by="ticker", threads=True,
                )
                if batch_data is None or batch_data.empty:
                    continue

                for ticker in batch:
                    try:
                        if len(batch) == 1:
                            df = batch_data
                        else:
                            df = batch_data[ticker]
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.get_level_values(0)
                        needed = ["Open", "High", "Low", "Close", "Volume"]
                        missing = [c for c in needed if c not in df.columns]
                        if missing:
                            continue
                        df = df[needed].copy()
                        df.dropna(how="all", inplace=True)
                        if len(df) >= 50:
                            safe_t = ticker.replace("/", "_").replace("^", "_")
                            cf = DATA_CACHE / f"{safe_t}_{warmup_start}_{end}.parquet"
                            df.to_parquet(cf)
                            data[ticker] = df
                    except Exception:
                        continue
            except Exception as e:
                print(f"    Batch download error: {e}")

            if i + batch_size < len(uncached):
                time.sleep(0.5)  # rate limit courtesy

    print(f"  Total tickers with data: {len(data)}")
    return data
