#!/usr/bin/env python3
"""
Morgan Trades SAR Swing Trading Strategy — Modular Backtester

Usage:
    python3 run_backtest.py                  # full backtest (~500+ tickers, 2019-2024)
    python3 run_backtest.py --smoke          # smoke test on ~15 known momentum stocks
    python3 run_backtest.py --tickers NVDA SMCI PLTR
    python3 run_backtest.py --start 2020-01-01 --end 2023-12-31
"""
import argparse
import sys

import pandas as pd

from src.config import (
    STARTING_CAPITAL, RISK_PER_TRADE, START_DATE, END_DATE, SMOKE_TICKERS,
)
from src.data import build_universe, fetch_single, fetch_all
from src.regime import compute_market_regime, regime_summary
from src.backtest import run_backtest, compute_metrics
from src.reporting import generate_all_reports


def main():
    parser = argparse.ArgumentParser(description="Morgan SAR Strategy Backtester")
    parser.add_argument("--smoke", action="store_true", help="Smoke test on ~15 known tickers")
    parser.add_argument("--tickers", nargs="+", help="Specific tickers to test")
    parser.add_argument("--start", default=START_DATE, help=f"Start date (default: {START_DATE})")
    parser.add_argument("--end", default=END_DATE, help=f"End date (default: {END_DATE})")
    parser.add_argument("--no-regime", action="store_true", help="Disable market regime filter")
    parser.add_argument("--max-positions", type=int, default=5, help="Max concurrent positions")
    args = parser.parse_args()

    start_date = args.start
    end_date = args.end

    print("=" * 70)
    print("  MORGAN TRADES SAR STRATEGY BACKTESTER")
    print(f"  Period: {start_date} to {end_date}")
    print(f"  Capital: ${STARTING_CAPITAL:,.0f} | Risk/Trade: {RISK_PER_TRADE*100}%")
    print(f"  Max Concurrent Positions: {args.max_positions}")
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

    # Fetch IXIC for market regime
    warmup_start = (pd.Timestamp(start_date) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    print("\nFetching NASDAQ Composite ($IXIC) for market regime...")
    ixic_df = fetch_single("^IXIC", warmup_start, end_date)
    if ixic_df is None:
        print("ERROR: Could not fetch $IXIC data. Aborting.")
        sys.exit(1)

    market_regime = compute_market_regime(ixic_df)
    summary = regime_summary(market_regime)
    print(f"  Bull regime: {summary['bull_days']}/{summary['total_days']} days ({summary['bull_pct']}%)")

    # Fetch all ticker data
    print(f"\nFetching data for {len(tickers)} tickers...")
    data = fetch_all(tickers, start_date, end_date)
    if not data:
        print("ERROR: No data fetched. Check network connection.")
        sys.exit(1)

    # Run backtest
    trades, equity_curve = run_backtest(
        data, market_regime, start_date, end_date,
        skip_bear=not args.no_regime,
        max_positions=args.max_positions,
    )

    # Compute metrics and generate reports
    result = compute_metrics(trades, equity_curve, start_date, end_date)
    generate_all_reports(result, trades, equity_curve)


if __name__ == "__main__":
    main()
