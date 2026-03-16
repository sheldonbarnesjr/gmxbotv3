#!/usr/bin/env python3
"""
Morgan Trades SAR Swing Trading Strategy — Backtester (Phase 1: Breakouts)

Tests the bull flag / range breakout setup on US equities using daily OHLCV data.
Strategy source: Morgan Trades / sartrading.io

Usage:
    python3 backtest_sar.py                  # full backtest (~3500 tickers)
    python3 backtest_sar.py --smoke          # smoke test on ~15 known momentum stocks
    python3 backtest_sar.py --tickers NVDA SMCI PLTR
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
import yfinance as yf

# ─── Configuration ───────────────────────────────────────────────────────────

STARTING_CAPITAL = 100_000
RISK_PER_TRADE = 0.01            # 1% of account per trade
MAX_POSITION_PCT = 0.40          # max 40% of account in one position
START_DATE = "2023-01-01"
END_DATE = "2025-12-31"
PARTIAL_SELL_PCT = 0.20          # sell 20% at 5x risk
PARTIAL_SELL_THRESHOLD = 5.0     # 5x risk multiple triggers partial
MAX_HOLD_DAYS = 60               # safety exit
MAX_CONCURRENT_POSITIONS = 5     # max open positions at once
MAX_RISK_PER_DAY = 0.03          # max 3% account risk deployed per day (3 trades)
COOLDOWN_AFTER_LOSS = 1          # skip 1 trading day after a stop loss
MIN_PRICE = 5.00                 # raised from $1 — filter out penny/junk stocks
MIN_ADR_PCT = 5.0
MIN_DAILY_DOLLAR_VOL = 5_000_000 # raised from $3.5M — better liquidity
PRIOR_MOVE_PCT = 0.30            # 30% prior move for breakout detection
PRIOR_MOVE_WINDOW = (5, 60)      # look for 30%+ move in 5-60 day window
CONSOLIDATION_MIN_BARS = 3       # minimum bars in consolidation
VOLUME_DRY_UP_RATIO = 0.90       # pullback volume < 90% of 20-day avg (mild contraction)
BREAKOUT_VOL_RATIO = 1.5         # breakout volume > 1.5x 20-day avg
REQUIRE_SMA_STACK = True         # require 10 > 20 SMA and price > 50 SMA
REQUIRE_TWO_CLOSES_BELOW_10SMA = True  # require 2 consecutive closes below 10 SMA to exit
MAX_STOP_WIDTH_PCT = 0.08        # max 8% stop width (tighter entries)

BASE_DIR = Path(__file__).parent
DATA_CACHE = BASE_DIR / "data_cache"
DATA_CACHE.mkdir(exist_ok=True)

SMOKE_TICKERS = [
    "NVDA", "SMCI", "PLTR", "CELH", "CAVA", "DUOL", "APP", "CRDO",
    "ANET", "DECK", "LLY", "COST", "META", "AVGO", "TOST",
]

# ─── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class Trade:
    ticker: str
    entry_date: str
    entry_price: float
    stop_price: float
    shares: float
    position_value: float
    exit_date: str = ""
    exit_price: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    r_multiple: float = 0.0
    holding_days: int = 0
    exit_reason: str = ""
    partial_sold: bool = False
    partial_pnl: float = 0.0
    market_regime: str = ""

@dataclass
class BacktestResult:
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0
    avg_winner_r: float = 0.0
    avg_loser_r: float = 0.0
    best_trade_r: float = 0.0
    worst_trade_r: float = 0.0
    profit_factor: float = 0.0
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_holding_days: float = 0.0
    total_pnl: float = 0.0
    ending_capital: float = 0.0
    trades_in_bull: int = 0
    trades_in_bear: int = 0
    win_rate_bull: float = 0.0
    win_rate_bear: float = 0.0

# ─── Ticker Universe ─────────────────────────────────────────────────────────

def _fetch_wiki_table(url: str) -> list:
    """Fetch HTML tables from Wikipedia, handling SSL issues."""
    import ssl
    import urllib.request
    import io
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=ctx) as resp:
        html = resp.read().decode("utf-8")
    return pd.read_html(io.StringIO(html))

def get_sp500_tickers() -> list[str]:
    """Fetch S&P 500 tickers from Wikipedia."""
    cache_file = DATA_CACHE / "sp500_tickers.json"
    if cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)
    try:
        tables = _fetch_wiki_table("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        tickers = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
        with open(cache_file, "w") as f:
            json.dump(tickers, f)
        return tickers
    except Exception as e:
        print(f"  Warning: couldn't fetch S&P 500 list: {e}")
        return []

def get_nasdaq100_tickers() -> list[str]:
    """Fetch NASDAQ 100 tickers from Wikipedia."""
    cache_file = DATA_CACHE / "nasdaq100_tickers.json"
    if cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)
    try:
        tables = _fetch_wiki_table("https://en.wikipedia.org/wiki/Nasdaq-100")
        for t in tables:
            if "Ticker" in t.columns:
                tickers = t["Ticker"].str.replace(".", "-", regex=False).tolist()
                with open(cache_file, "w") as f:
                    json.dump(tickers, f)
                return tickers
            if "Symbol" in t.columns:
                tickers = t["Symbol"].str.replace(".", "-", regex=False).tolist()
                with open(cache_file, "w") as f:
                    json.dump(tickers, f)
                return tickers
    except Exception as e:
        print(f"  Warning: couldn't fetch NASDAQ 100 list: {e}")
    return []

def get_smallcap_tickers() -> list[str]:
    """
    Get a broad set of US small/mid-cap tickers using yfinance screener.
    Falls back to a curated list of actively traded small-caps.
    """
    cache_file = DATA_CACHE / "smallcap_tickers.json"
    if cache_file.exists():
        with open(cache_file) as f:
            tickers = json.load(f)
        if len(tickers) > 50:
            return tickers

    # Curated list of popular small/mid-cap momentum stocks
    tickers = [
        "SMCI", "CRDO", "CAVA", "DUOL", "TOST", "APP", "IONQ", "RKLB",
        "UPST", "AFRM", "SOFI", "HOOD", "BILL", "CELH", "HIMS", "DJT",
        "CIFR", "MARA", "RIOT", "CLSK", "BTDR", "BITF", "IREN", "CORZ",
        "SOUN", "RGTI", "QUBT", "LUNR", "BKSY", "RDW", "MNTS", "ASTS",
        "DNA", "JOBY", "ACHR", "LILM", "EVTL", "BLDE", "VLD", "ASTR",
        "PLBY", "WKHS", "GOEV", "FFIE", "LCID", "RIVN", "PSNY", "FSR",
        "NKLA", "ARVL", "REE", "XPEV", "NIO", "LI", "GRAB", "SE",
        "BABA", "JD", "PDD", "BIDU", "BILI", "IQ", "TME", "VNET",
        "FUTU", "TIGR", "DADA", "GDS", "TUYA", "DOYU", "HUYA", "YMM",
        "ZK", "QFIN", "FINV", "LU", "VIPS", "TAL", "EDU", "GOTU",
        "AI", "BBAI", "PLTR", "SNOW", "DDOG", "NET", "CRWD", "ZS",
        "OKTA", "MDB", "ESTC", "CFLT", "DKNG", "PENN", "MGM", "WYNN",
        "CHWY", "FRSH", "GTLB", "PATH", "DOCN", "DLO", "CWAN", "RELY",
        "BRZE", "SEMR", "TASK", "KD", "COUR", "UDMY", "INST", "SPT",
        "YOU", "VERX", "VRRM", "MNDY", "PCOR", "WK", "ALKT", "CERT",
        "TRUP", "GDRX", "HCAT", "OPAD", "CARG", "OPEN", "RDFN", "FIGS",
        "BIRD", "ALLB", "LMND", "ROOT", "MILE", "ACVA", "TH", "PTON",
        "BMBL", "MTCH", "ABNB", "DASH", "UBER", "LYFT", "GRAB", "CPNG",
        "SHOP", "MELI", "GLOB", "DLO", "STNE", "PAGS", "VTEX", "VSTE",
    ]
    with open(cache_file, "w") as f:
        json.dump(tickers, f)
    return tickers

def build_universe() -> list[str]:
    """Build the full ticker universe."""
    print("Building ticker universe...")
    sp500 = get_sp500_tickers()
    print(f"  S&P 500: {len(sp500)} tickers")
    nasdaq100 = get_nasdaq100_tickers()
    print(f"  NASDAQ 100: {len(nasdaq100)} tickers")
    smallcap = get_smallcap_tickers()
    print(f"  Small/Mid-Cap: {len(smallcap)} tickers")

    all_tickers = sorted(set(sp500 + nasdaq100 + smallcap))
    print(f"  Total unique: {len(all_tickers)} tickers")
    return all_tickers

# ─── Data Fetching ───────────────────────────────────────────────────────────

def fetch_data(ticker: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV data with local caching."""
    safe_ticker = ticker.replace("/", "_")
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
        # Flatten multi-level columns if present (yfinance returns (Price, Ticker))
        if isinstance(df.columns, pd.MultiIndex):
            df = df.droplevel("Ticker", axis=1)
        # Keep only OHLCV columns
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

def fetch_all_data(tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """Batch-download data for all tickers. Uses yfinance batch download for speed."""
    # Extend start date by 250 days for indicator warm-up (200 SMA + buffer)
    warmup_start = (pd.Timestamp(start) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")

    # Check which tickers are already cached
    uncached = []
    data = {}
    for ticker in tickers:
        safe_ticker = ticker.replace("/", "_")
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
        # Download in batches to avoid rate limiting
        batch_size = 50
        for i in range(0, len(uncached), batch_size):
            batch = uncached[i:i + batch_size]
            batch_str = " ".join(batch)
            try:
                batch_data = yf.download(
                    batch_str, start=warmup_start, end=end,
                    progress=False, auto_adjust=True, group_by="ticker", threads=True
                )
                if batch_data is None or batch_data.empty:
                    continue

                for ticker in batch:
                    try:
                        if isinstance(batch_data.columns, pd.MultiIndex):
                            # yfinance returns (Price, Ticker) multi-index
                            try:
                                df = batch_data.xs(ticker, level="Ticker", axis=1).copy()
                            except (KeyError, TypeError):
                                continue
                        else:
                            if len(batch) == 1:
                                df = batch_data.copy()
                            else:
                                continue
                        needed = ["Open", "High", "Low", "Close", "Volume"]
                        missing = [c for c in needed if c not in df.columns]
                        if missing:
                            continue
                        df = df[needed].copy()
                        df.dropna(how="all", inplace=True)
                        if len(df) >= 50:
                            safe_t = ticker.replace("/", "_")
                            cache_file = DATA_CACHE / f"{safe_t}_{warmup_start}_{end}.parquet"
                            df.to_parquet(cache_file)
                            data[ticker] = df
                    except Exception:
                        continue
            except Exception as e:
                print(f"    Batch download error: {e}")

            if i + batch_size < len(uncached):
                time.sleep(0.5)  # rate limit courtesy

    print(f"  Total tickers with data: {len(data)}")
    return data

# ─── Indicators & Filters ───────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add SMA, ADR%, dollar volume columns."""
    df = df.copy()
    df["SMA10"] = df["Close"].rolling(10).mean()
    df["SMA20"] = df["Close"].rolling(20).mean()
    df["SMA50"] = df["Close"].rolling(50).mean()
    df["SMA200"] = df["Close"].rolling(200).mean()
    df["ADR_pct"] = ((df["High"] - df["Low"]) / df["Close"]).rolling(20).mean() * 100
    df["Dollar_Vol"] = df["Close"] * df["Volume"]
    df["Avg_Dollar_Vol"] = df["Dollar_Vol"].rolling(20).mean()
    df["Vol_SMA20"] = df["Volume"].rolling(20).mean()
    return df

def passes_filters(df: pd.DataFrame, idx: int) -> bool:
    """Check if a stock passes universe filters at a given bar index."""
    row = df.iloc[idx]
    if row["Close"] < MIN_PRICE:
        return False
    if pd.isna(row["ADR_pct"]) or row["ADR_pct"] < MIN_ADR_PCT:
        return False
    if pd.isna(row["Avg_Dollar_Vol"]) or row["Avg_Dollar_Vol"] < MIN_DAILY_DOLLAR_VOL:
        return False

    # SMA stacking: price > 50 SMA (intermediate uptrend)
    if REQUIRE_SMA_STACK:
        if pd.isna(row["SMA50"]) or row["Close"] < row["SMA50"]:
            return False
        # 10 SMA > 20 SMA (short-term momentum confirmed)
        if pd.isna(row["SMA10"]) or pd.isna(row["SMA20"]):
            return False
        if row["SMA10"] < row["SMA20"]:
            return False

    return True

def compute_relative_strength(data: dict[str, pd.DataFrame], date: pd.Timestamp, periods: list[int] = [21, 63, 126]) -> set[str]:
    """
    Compute relative strength ranking. Return top 2% of tickers by average
    performance over 1M (21d), 3M (63d), 6M (126d) lookbacks.
    """
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
    top_n = max(1, int(len(sorted_tickers) * 0.02))
    return set(sorted_tickers[:top_n])

# ─── Market Regime Filter ────────────────────────────────────────────────────

def compute_market_regime(ixic_df: pd.DataFrame) -> pd.Series:
    """
    Returns a Series indexed by date with values 'BULL' or 'BEAR'.
    BULL = IXIC 10 SMA > 20 SMA.
    """
    sma10 = ixic_df["Close"].rolling(10).mean()
    sma20 = ixic_df["Close"].rolling(20).mean()
    regime = pd.Series("BEAR", index=ixic_df.index)
    regime[sma10 > sma20] = "BULL"
    return regime

# ─── Breakout Detection ─────────────────────────────────────────────────────

def detect_breakouts(df: pd.DataFrame, start_date: str) -> list[dict]:
    """
    Scan daily bars for breakout setups (bull flag pattern).

    The logic is two-phase:
    1. Check if PRIOR bars formed a valid consolidation (near SMA, volume drying up, higher lows)
    2. Check if TODAY's bar is the breakout (close > range high, high volume)
    """
    signals = []
    start_ts = pd.Timestamp(start_date)

    if len(df) < 100:
        return signals

    # Track last signal date per ticker to avoid duplicates within 10 bars
    last_signal_idx = -20

    for i in range(80, len(df)):
        date = df.index[i]
        if date < start_ts:
            continue

        # Avoid rapid-fire signals on the same stock
        if i - last_signal_idx < 10:
            continue

        if not passes_filters(df, i):
            continue

        row = df.iloc[i]

        # Skip if SMAs not computed
        if pd.isna(row["SMA10"]) or pd.isna(row["SMA20"]):
            continue

        # ── Step 1: Prior move >= 30% in 5-60 day window ──
        prior_move_found = False
        move_end_idx = None
        for lookback in range(PRIOR_MOVE_WINDOW[1], PRIOR_MOVE_WINDOW[0] - 1, -5):
            if i - lookback < 0:
                continue
            window = df.iloc[i - lookback:i]
            past_low = window["Low"].min()
            recent_high = window["High"].max()
            if past_low > 0 and (recent_high / past_low - 1) >= PRIOR_MOVE_PCT:
                prior_move_found = True
                move_end_idx = i - lookback + window["High"].values.argmax()
                break

        if not prior_move_found:
            continue

        # ── Step 2: 10 SMA or 20 SMA must be inclining (at least one) ──
        if i < 5:
            continue
        sma10_slope = df.iloc[i]["SMA10"] - df.iloc[i - 5]["SMA10"]
        sma20_slope = df.iloc[i]["SMA20"] - df.iloc[i - 5]["SMA20"]
        if sma10_slope <= 0 and sma20_slope <= 0:
            continue

        # ── Step 3: Consolidation in PRIOR bars (not today) ──
        # Look at the last 3-15 bars before today for consolidation characteristics
        consol_len = min(15, i - max(0, move_end_idx or 0))
        if consol_len < CONSOLIDATION_MIN_BARS:
            continue

        consol_slice = df.iloc[i - consol_len:i]  # excludes today (breakout bar)

        # 3a: At some point during consolidation, price was near 10 or 20 SMA
        near_sma_found = False
        for ci in range(len(consol_slice)):
            c_row = consol_slice.iloc[ci]
            if pd.isna(c_row["SMA10"]):
                continue
            if (abs(c_row["Close"] - c_row["SMA10"]) / c_row["SMA10"] < 0.08 or
                    abs(c_row["Close"] - c_row["SMA20"]) / c_row["SMA20"] < 0.08 or
                    c_row["Low"] <= c_row["SMA10"] <= c_row["High"] or
                    c_row["Low"] <= c_row["SMA20"] <= c_row["High"]):
                near_sma_found = True
                break
        if not near_sma_found:
            continue

        # 3b: Higher lows pattern (at least some)
        consol_lows = consol_slice["Low"].values
        if len(consol_lows) >= 3:
            higher_low_count = sum(1 for j in range(1, len(consol_lows))
                                   if consol_lows[j] >= consol_lows[j - 1] * 0.995)
            if higher_low_count < len(consol_lows) * 0.4:
                continue

        # ── Step 4: Volume drying up during consolidation ──
        consol_avg_vol = consol_slice["Volume"].mean()
        if not pd.isna(row["Vol_SMA20"]) and row["Vol_SMA20"] > 0:
            if consol_avg_vol / row["Vol_SMA20"] > VOLUME_DRY_UP_RATIO:
                continue  # consolidation volume too high — not a proper squeeze

        # ── Step 5: Breakout on high volume ──
        # Today's close must break above the consolidation range high
        consol_high = consol_slice["High"].max()
        if row["Close"] <= consol_high:
            continue

        # Volume confirmation: today > 1.5x 20-day average
        if pd.isna(row["Vol_SMA20"]) or row["Vol_SMA20"] <= 0:
            continue
        if row["Volume"] < row["Vol_SMA20"] * BREAKOUT_VOL_RATIO:
            continue

        # ── Signal confirmed ──
        entry_price = row["Close"]
        stop_price = row["Low"]  # Low of Day

        # Sanity: stop can't be >= entry
        if stop_price >= entry_price:
            continue
        # Stop shouldn't be too far — tight entries only
        risk_pct = (entry_price - stop_price) / entry_price
        if risk_pct > MAX_STOP_WIDTH_PCT:
            continue
        # Stop shouldn't be too tight either (< 1% means noise will trigger it)
        if risk_pct < 0.01:
            continue

        last_signal_idx = i
        signals.append({
            "date": date,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "risk_pct": risk_pct,
            "bar_idx": i,
        })

    return signals

# ─── Trade Simulation ────────────────────────────────────────────────────────

def simulate_trade(
    df: pd.DataFrame,
    signal: dict,
    capital: float,
    market_regime: pd.Series,
) -> Optional[Trade]:
    """Simulate a single trade from entry to exit."""
    entry_price = signal["entry_price"]
    stop_price = signal["stop_price"]
    entry_date = signal["date"]
    bar_idx = signal["bar_idx"]

    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return None

    risk_amount = capital * RISK_PER_TRADE
    shares = risk_amount / risk_per_share
    position_value = shares * entry_price

    # Cap position at MAX_POSITION_PCT of account
    if position_value > capital * MAX_POSITION_PCT:
        position_value = capital * MAX_POSITION_PCT
        shares = position_value / entry_price

    # Get market regime at entry
    regime = "UNKNOWN"
    if entry_date in market_regime.index:
        regime = market_regime.loc[entry_date]
    elif len(market_regime) > 0:
        # Find nearest date
        nearest = market_regime.index[market_regime.index.get_indexer([entry_date], method="ffill")[0]]
        regime = market_regime.loc[nearest]

    trade = Trade(
        ticker="",  # filled by caller
        entry_date=entry_date.strftime("%Y-%m-%d"),
        entry_price=round(entry_price, 2),
        stop_price=round(stop_price, 2),
        shares=round(shares, 2),
        position_value=round(position_value, 2),
        market_regime=regime,
    )

    remaining_shares = shares
    partial_pnl = 0.0
    current_stop = stop_price
    partial_taken = False
    closes_below_10sma = 0  # track consecutive closes below 10 SMA

    # Walk forward day by day
    for j in range(bar_idx + 1, min(bar_idx + 1 + MAX_HOLD_DAYS, len(df))):
        bar = df.iloc[j]
        bar_date = df.index[j]

        # Check stop loss (use intraday low)
        if bar["Low"] <= current_stop:
            exit_price = current_stop
            pnl = partial_pnl + remaining_shares * (exit_price - entry_price)
            trade.exit_date = bar_date.strftime("%Y-%m-%d")
            trade.exit_price = round(exit_price, 2)
            trade.pnl = round(pnl, 2)
            trade.pnl_pct = round(pnl / position_value * 100, 2)
            trade.r_multiple = round(pnl / risk_amount, 2) if risk_amount > 0 else 0
            trade.holding_days = (bar_date - entry_date).days
            trade.exit_reason = "stop_loss" if not partial_taken else "stop_breakeven"
            trade.partial_sold = partial_taken
            trade.partial_pnl = round(partial_pnl, 2)
            return trade

        # Check partial profit at 5x risk
        unrealized_per_share = bar["Close"] - entry_price
        unrealized_r = (unrealized_per_share * remaining_shares) / risk_amount if risk_amount > 0 else 0

        if not partial_taken and unrealized_r >= PARTIAL_SELL_THRESHOLD:
            sell_shares = remaining_shares * PARTIAL_SELL_PCT
            partial_pnl += sell_shares * (bar["Close"] - entry_price)
            remaining_shares -= sell_shares
            current_stop = entry_price  # move stop to breakeven
            partial_taken = True

        # After partial, trail stop to 10 SMA if it's above breakeven
        if partial_taken:
            sma10_val = df.iloc[max(0, j - 9):j + 1]["Close"].mean()
            if sma10_val > current_stop:
                current_stop = sma10_val * 0.99  # trail just below 10 SMA

        # Check 10 SMA close rule (daily close below 10 SMA)
        # Give the trade at least 3 days before applying this rule
        days_in_trade = j - bar_idx
        sma10 = df.iloc[max(0, j - 9):j + 1]["Close"].mean()

        if days_in_trade >= 3 and bar["Close"] < sma10:
            closes_below_10sma += 1
        else:
            closes_below_10sma = 0

        # Require 2 consecutive closes below 10 SMA to exit (avoids shakeouts)
        exit_on_sma = (REQUIRE_TWO_CLOSES_BELOW_10SMA and closes_below_10sma >= 2) or \
                      (not REQUIRE_TWO_CLOSES_BELOW_10SMA and closes_below_10sma >= 1)

        if exit_on_sma:
            exit_price = bar["Close"]
            pnl = partial_pnl + remaining_shares * (exit_price - entry_price)
            trade.exit_date = bar_date.strftime("%Y-%m-%d")
            trade.exit_price = round(exit_price, 2)
            trade.pnl = round(pnl, 2)
            trade.pnl_pct = round(pnl / position_value * 100, 2)
            trade.r_multiple = round(pnl / risk_amount, 2) if risk_amount > 0 else 0
            trade.holding_days = (bar_date - entry_date).days
            trade.exit_reason = "10sma_close"
            trade.partial_sold = partial_taken
            trade.partial_pnl = round(partial_pnl, 2)
            return trade

    # Time exit — max hold reached or ran out of data
    last_idx = min(bar_idx + MAX_HOLD_DAYS, len(df) - 1)
    last_bar = df.iloc[last_idx]
    last_date = df.index[last_idx]
    exit_price = last_bar["Close"]
    pnl = partial_pnl + remaining_shares * (exit_price - entry_price)

    trade.exit_date = last_date.strftime("%Y-%m-%d")
    trade.exit_price = round(exit_price, 2)
    trade.pnl = round(pnl, 2)
    trade.pnl_pct = round(pnl / position_value * 100, 2)
    trade.r_multiple = round(pnl / risk_amount, 2) if risk_amount > 0 else 0
    trade.holding_days = (last_date - entry_date).days
    trade.exit_reason = "time_exit"
    trade.partial_sold = partial_taken
    trade.partial_pnl = round(partial_pnl, 2)
    return trade

# ─── Portfolio Backtest Engine ───────────────────────────────────────────────

def run_backtest(
    data: dict[str, pd.DataFrame],
    market_regime: pd.Series,
    start_date: str,
    end_date: str,
) -> tuple[list[Trade], list[tuple[str, float]]]:
    """
    Run the full backtest across all tickers.
    Returns (trades, equity_curve) where equity_curve is [(date, capital), ...].
    """
    print("\nPhase 1: Computing indicators...")
    indicator_data = {}
    for ticker, df in data.items():
        indicator_data[ticker] = compute_indicators(df)

    print("Phase 2: Detecting breakout signals...")
    all_signals = []
    for ticker, df in indicator_data.items():
        signals = detect_breakouts(df, start_date)
        for s in signals:
            s["ticker"] = ticker
            all_signals.append(s)

    # Sort all signals by date
    all_signals.sort(key=lambda s: s["date"])
    print(f"  Found {len(all_signals)} raw breakout signals")

    # Compute relative strength periodically (monthly) to avoid O(n^2)
    print("Phase 3: Computing relative strength rankings...")
    rs_cache = {}
    trading_dates = sorted(set(s["date"] for s in all_signals))
    # Compute RS monthly
    months_computed = set()
    for date in trading_dates:
        month_key = date.strftime("%Y-%m")
        if month_key not in months_computed:
            rs_tickers = compute_relative_strength(indicator_data, date)
            rs_cache[month_key] = rs_tickers
            months_computed.add(month_key)

    print("Phase 4: Simulating trades...")
    capital = STARTING_CAPITAL
    trades: list[Trade] = []
    equity_curve: list[tuple[str, float]] = [(start_date, capital)]

    # Position tracking for concurrent limit + cooldown
    active_positions: dict[str, str] = {}  # ticker -> exit_date
    trades_per_day: dict[str, int] = {}    # date_str -> count of trades opened
    last_loss_date: Optional[str] = None   # for cooldown after losses
    ticker_cooldown: dict[str, str] = {}   # ticker -> earliest re-entry date after loss

    for signal in all_signals:
        ticker = signal["ticker"]
        date = signal["date"]
        date_str = date.strftime("%Y-%m-%d")

        # ── Concurrent position limit ──
        # Clean up expired positions
        active_positions = {t: ed for t, ed in active_positions.items() if ed > date_str}

        if len(active_positions) >= MAX_CONCURRENT_POSITIONS:
            continue

        # Skip if we already have a position in this ticker
        if ticker in active_positions:
            continue

        # ── Per-ticker cooldown after loss ──
        if ticker in ticker_cooldown and date_str < ticker_cooldown[ticker]:
            continue

        # ── Daily risk cap ──
        day_trades = trades_per_day.get(date_str, 0)
        if day_trades >= int(MAX_RISK_PER_DAY / RISK_PER_TRADE):
            continue

        # ── Cooldown after any loss ──
        if last_loss_date and COOLDOWN_AFTER_LOSS > 0:
            cooldown_end = (pd.Timestamp(last_loss_date) + pd.Timedelta(days=COOLDOWN_AFTER_LOSS)).strftime("%Y-%m-%d")
            if date_str < cooldown_end:
                continue

        # Check market regime — skip in bearish regime
        regime = "UNKNOWN"
        if date in market_regime.index:
            regime = market_regime.loc[date]
        elif len(market_regime) > 0:
            idx = market_regime.index.get_indexer([date], method="ffill")[0]
            if idx >= 0:
                regime = market_regime.iloc[idx]
        if regime == "BEAR":
            continue

        # Check relative strength (skip if universe too small for meaningful ranking)
        if len(data) > 50:
            month_key = date.strftime("%Y-%m")
            rs_set = rs_cache.get(month_key, set())
            if rs_set and ticker not in rs_set:
                continue

        # Don't risk more than we have
        if capital < STARTING_CAPITAL * 0.1:
            continue

        # Simulate the trade
        df = indicator_data[ticker]
        trade = simulate_trade(df, signal, capital, market_regime)
        if trade is None:
            continue

        trade.ticker = ticker
        trades.append(trade)

        # Update capital
        capital += trade.pnl
        equity_curve.append((trade.exit_date, round(capital, 2)))

        # Track position
        active_positions[ticker] = trade.exit_date
        trades_per_day[date_str] = trades_per_day.get(date_str, 0) + 1

        # Track losses for cooldown
        if trade.pnl < 0 and trade.exit_reason == "stop_loss":
            last_loss_date = trade.exit_date
            # Per-ticker cooldown: 10 trading days before re-entering a loser
            ticker_cooldown[ticker] = (pd.Timestamp(trade.exit_date) + pd.Timedelta(days=14)).strftime("%Y-%m-%d")

    print(f"  Executed {len(trades)} trades")
    return trades, equity_curve

# ─── Performance Metrics ─────────────────────────────────────────────────────

def compute_metrics(trades: list[Trade], equity_curve: list[tuple[str, float]]) -> BacktestResult:
    """Compute all performance metrics."""
    result = BacktestResult()

    if not trades:
        result.ending_capital = STARTING_CAPITAL
        return result

    result.total_trades = len(trades)
    winners = [t for t in trades if t.pnl > 0]
    losers = [t for t in trades if t.pnl <= 0]
    result.winners = len(winners)
    result.losers = len(losers)
    result.win_rate = round(len(winners) / len(trades) * 100, 1)

    r_multiples = [t.r_multiple for t in trades]
    winner_rs = [t.r_multiple for t in winners]
    loser_rs = [t.r_multiple for t in losers]

    result.avg_winner_r = round(np.mean(winner_rs), 2) if winner_rs else 0
    result.avg_loser_r = round(np.mean(loser_rs), 2) if loser_rs else 0
    result.best_trade_r = round(max(r_multiples), 2)
    result.worst_trade_r = round(min(r_multiples), 2)

    gross_profit = sum(t.pnl for t in winners)
    gross_loss = abs(sum(t.pnl for t in losers))
    result.profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    result.total_pnl = round(sum(t.pnl for t in trades), 2)
    result.ending_capital = round(STARTING_CAPITAL + result.total_pnl, 2)
    result.total_return_pct = round(result.total_pnl / STARTING_CAPITAL * 100, 1)

    result.avg_holding_days = round(np.mean([t.holding_days for t in trades]), 1)

    # Max drawdown from equity curve
    capitals = [ec[1] for ec in equity_curve]
    peak = capitals[0]
    max_dd = 0
    for c in capitals:
        peak = max(peak, c)
        dd = (peak - c) / peak
        max_dd = max(max_dd, dd)
    result.max_drawdown_pct = round(max_dd * 100, 1)

    # Sharpe ratio (annualized, using trade returns)
    trade_returns = [t.pnl / t.position_value for t in trades if t.position_value > 0]
    if len(trade_returns) > 1:
        avg_ret = np.mean(trade_returns)
        std_ret = np.std(trade_returns, ddof=1)
        # Approximate annualization: assume ~50 trades/year
        trades_per_year = max(1, len(trades) / 3)  # 3 year backtest
        if std_ret > 0:
            result.sharpe_ratio = round((avg_ret / std_ret) * np.sqrt(trades_per_year), 2)

    # Bull vs bear regime stats
    bull_trades = [t for t in trades if t.market_regime == "BULL"]
    bear_trades = [t for t in trades if t.market_regime == "BEAR"]
    result.trades_in_bull = len(bull_trades)
    result.trades_in_bear = len(bear_trades)
    result.win_rate_bull = round(
        len([t for t in bull_trades if t.pnl > 0]) / len(bull_trades) * 100, 1
    ) if bull_trades else 0
    result.win_rate_bear = round(
        len([t for t in bear_trades if t.pnl > 0]) / len(bear_trades) * 100, 1
    ) if bear_trades else 0

    return result

# ─── Output & Visualization ──────────────────────────────────────────────────

def print_results(result: BacktestResult, trades: list[Trade]):
    """Print formatted results to console."""
    print("\n" + "=" * 70)
    print("  MORGAN TRADES SAR STRATEGY — BACKTEST RESULTS (Breakouts)")
    print("=" * 70)
    print(f"  Period:            {START_DATE} to {END_DATE}")
    print(f"  Starting Capital:  ${STARTING_CAPITAL:,.0f}")
    print(f"  Ending Capital:    ${result.ending_capital:,.0f}")
    print(f"  Total Return:      {result.total_return_pct:+.1f}%")
    print(f"  Total PnL:         ${result.total_pnl:+,.0f}")
    print("-" * 70)
    print(f"  Total Trades:      {result.total_trades}")
    print(f"  Winners:           {result.winners} ({result.win_rate}%)")
    print(f"  Losers:            {result.losers}")
    print(f"  Profit Factor:     {result.profit_factor}")
    print(f"  Sharpe Ratio:      {result.sharpe_ratio}")
    print(f"  Max Drawdown:      {result.max_drawdown_pct}%")
    print("-" * 70)
    print(f"  Avg Winner (R):    {result.avg_winner_r}R")
    print(f"  Avg Loser (R):     {result.avg_loser_r}R")
    print(f"  Best Trade (R):    {result.best_trade_r}R")
    print(f"  Worst Trade (R):   {result.worst_trade_r}R")
    print(f"  Avg Hold Days:     {result.avg_holding_days}")
    print("-" * 70)
    print(f"  Bull Regime Trades: {result.trades_in_bull} (Win Rate: {result.win_rate_bull}%)")
    print(f"  Bear Regime Trades: {result.trades_in_bear} (Win Rate: {result.win_rate_bear}%)")
    print("=" * 70)

    if trades:
        # Top 10 trades
        sorted_trades = sorted(trades, key=lambda t: t.r_multiple, reverse=True)
        print("\n  TOP 10 TRADES:")
        print(f"  {'Ticker':<8} {'Entry Date':<12} {'R-Multiple':>10} {'PnL':>10} {'Exit Reason':<15}")
        print("  " + "-" * 60)
        for t in sorted_trades[:10]:
            print(f"  {t.ticker:<8} {t.entry_date:<12} {t.r_multiple:>10.1f}R  ${t.pnl:>8,.0f}  {t.exit_reason:<15}")

        # Bottom 5 trades
        print("\n  WORST 5 TRADES:")
        for t in sorted_trades[-5:]:
            print(f"  {t.ticker:<8} {t.entry_date:<12} {t.r_multiple:>10.1f}R  ${t.pnl:>8,.0f}  {t.exit_reason:<15}")

        # Monthly breakdown
        print("\n  MONTHLY BREAKDOWN:")
        monthly = {}
        for t in trades:
            month = t.entry_date[:7]
            if month not in monthly:
                monthly[month] = {"pnl": 0, "trades": 0, "wins": 0}
            monthly[month]["pnl"] += t.pnl
            monthly[month]["trades"] += 1
            if t.pnl > 0:
                monthly[month]["wins"] += 1

        print(f"  {'Month':<10} {'PnL':>10} {'Trades':>8} {'Win Rate':>10}")
        print("  " + "-" * 42)
        for month in sorted(monthly.keys()):
            m = monthly[month]
            wr = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0
            print(f"  {month:<10} ${m['pnl']:>9,.0f} {m['trades']:>8} {wr:>9.0f}%")

    print()

def save_results(result: BacktestResult, trades: list[Trade]):
    """Save detailed results to JSON."""
    output = {
        "summary": asdict(result),
        "params": {
            "starting_capital": STARTING_CAPITAL,
            "risk_per_trade": RISK_PER_TRADE,
            "max_position_pct": MAX_POSITION_PCT,
            "start_date": START_DATE,
            "end_date": END_DATE,
            "partial_sell_pct": PARTIAL_SELL_PCT,
            "partial_sell_threshold": PARTIAL_SELL_THRESHOLD,
        },
        "trades": [asdict(t) for t in trades],
    }
    out_path = BASE_DIR / "backtest_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"Results saved to {out_path}")

def plot_equity_curve(equity_curve: list[tuple[str, float]], trades: list[Trade]):
    """Generate equity curve chart."""
    if len(equity_curve) < 2:
        print("Not enough data for equity curve")
        return

    dates = [pd.Timestamp(ec[0]) for ec in equity_curve]
    capitals = [ec[1] for ec in equity_curve]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), height_ratios=[3, 1])
    fig.suptitle("Morgan SAR Breakout Strategy — Equity Curve", fontsize=14, fontweight="bold")

    # Equity curve
    ax1.plot(dates, capitals, "b-", linewidth=1.5, label="Portfolio Value")
    ax1.axhline(y=STARTING_CAPITAL, color="gray", linestyle="--", alpha=0.5, label="Starting Capital")
    ax1.fill_between(dates, STARTING_CAPITAL, capitals, alpha=0.1,
                     where=[c >= STARTING_CAPITAL for c in capitals], color="green")
    ax1.fill_between(dates, STARTING_CAPITAL, capitals, alpha=0.1,
                     where=[c < STARTING_CAPITAL for c in capitals], color="red")
    ax1.set_ylabel("Portfolio Value ($)")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"${x:,.0f}"))

    # R-multiple distribution
    if trades:
        r_mults = [t.r_multiple for t in trades]
        colors = ["green" if r > 0 else "red" for r in r_mults]
        ax2.bar(range(len(r_mults)), r_mults, color=colors, alpha=0.7, width=1.0)
        ax2.axhline(y=0, color="black", linewidth=0.5)
        ax2.set_ylabel("R-Multiple")
        ax2.set_xlabel("Trade #")
        ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    out_path = BASE_DIR / "equity_curve.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Equity curve saved to {out_path}")

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Morgan SAR Breakout Strategy Backtester")
    parser.add_argument("--smoke", action="store_true", help="Run smoke test on ~15 known tickers")
    parser.add_argument("--tickers", nargs="+", help="Specific tickers to test")
    args = parser.parse_args()

    print("=" * 70)
    print("  MORGAN TRADES SAR STRATEGY BACKTESTER")
    print(f"  Period: {START_DATE} to {END_DATE}")
    print(f"  Capital: ${STARTING_CAPITAL:,.0f} | Risk/Trade: {RISK_PER_TRADE*100}%")
    print("=" * 70)

    # Determine ticker universe
    if args.tickers:
        tickers = args.tickers
        print(f"\nCustom tickers: {', '.join(tickers)}")
    elif args.smoke:
        tickers = SMOKE_TICKERS
        print(f"\nSmoke test: {', '.join(tickers)}")
    else:
        tickers = build_universe()

    # Extend start for warm-up
    warmup_start = (pd.Timestamp(START_DATE) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")

    # Fetch IXIC for market regime
    print("\nFetching NASDAQ Composite ($IXIC) for market regime...")
    ixic_df = fetch_data("^IXIC", warmup_start, END_DATE)
    if ixic_df is None:
        print("ERROR: Could not fetch $IXIC data. Aborting.")
        sys.exit(1)
    market_regime = compute_market_regime(ixic_df)
    bull_days = (market_regime == "BULL").sum()
    total_days = len(market_regime.dropna())
    print(f"  Bull regime: {bull_days}/{total_days} days ({bull_days/total_days*100:.0f}%)")

    # Fetch all ticker data
    print(f"\nFetching data for {len(tickers)} tickers...")
    data = fetch_all_data(tickers, warmup_start, END_DATE)

    if not data:
        print("ERROR: No data fetched. Check network connection.")
        sys.exit(1)

    # Run backtest
    trades, equity_curve = run_backtest(data, market_regime, START_DATE, END_DATE)

    # Compute and display metrics
    result = compute_metrics(trades, equity_curve)
    print_results(result, trades)

    # Save results
    save_results(result, trades)
    plot_equity_curve(equity_curve, trades)

    print("Done!")

if __name__ == "__main__":
    main()
