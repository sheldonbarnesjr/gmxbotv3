#!/usr/bin/env python3
"""
Morgan Trades SAR Swing Trading Strategy — Modular Backtester (v2)

Key improvements over v1:
  - Setup detection on Day N, entry on Day N+1 open (no lookahead bias)
  - Gap filters: skip gap-down >2%, gap-up >10%
  - Stop width filter: skip if stop >8% from entry
  - Consolidation requires BOTH higher lows AND lower highs (tightening)
  - Volume contraction: pullback vol < 70% of 20-day avg (not move avg)
  - Daily equity curve (not per-trade)
  - Sortino ratio
  - Regime comparison charts
  - Validation mode: generate 10 setup charts before full backtest

Usage:
    python3 run_backtest2.py                      # full backtest (S&P 500 + NASDAQ 100, 2019-2024)
    python3 run_backtest2.py --smoke              # smoke test on ~15 known momentum stocks
    python3 run_backtest2.py --tickers NVDA SMCI PLTR
    python3 run_backtest2.py --validate           # generate 10 example setup charts only
    python3 run_backtest2.py --validate --smoke   # validate on smoke tickers
    python3 run_backtest2.py --start 2020-01-01 --end 2023-12-31
    python3 run_backtest2.py --no-regime          # disable market regime filter
    python3 run_backtest2.py --max-positions 3    # limit concurrent positions
"""
import argparse
import sys

import pandas as pd

from src.config2 import STARTING_CAPITAL, RISK_PER_TRADE, START_DATE, END_DATE, SMOKE_TICKERS
from src.data2 import build_universe, fetch_single, fetch_all
from src.regime2 import compute_market_regime, regime_summary
from src.backtest2 import run_backtest, compute_metrics
from src.reporting2 import generate_all_reports
from src.validate2 import generate_validation_charts


def main():
    parser = argparse.ArgumentParser(description="Morgan SAR Strategy Backtester v2")
    parser.add_argument("--smoke", action="store_true",
                        help="Smoke test on ~15 known momentum stocks")
    parser.add_argument("--tickers", nargs="+",
                        help="Specific tickers to test")
    parser.add_argument("--start", default=START_DATE,
                        help=f"Start date (default: {START_DATE})")
    parser.add_argument("--end", default=END_DATE,
                        help=f"End date (default: {END_DATE})")
    parser.add_argument("--no-regime", action="store_true",
                        help="Disable market regime filter")
    parser.add_argument("--max-positions", type=int, default=5,
                        help="Max concurrent positions (default: 5)")
    parser.add_argument("--validate", action="store_true",
                        help="Generate 10 example setup charts for visual verification")
    parser.add_argument("--validate-count", type=int, default=10,
                        help="Number of validation charts to generate (default: 10)")
    args = parser.parse_args()

    start_date = args.start
    end_date = args.end

    print("=" * 70)
    print("  MORGAN TRADES SAR STRATEGY BACKTESTER v2")
    print(f"  Period: {start_date} to {end_date}")
    print(f"  Capital: ${STARTING_CAPITAL:,.0f} | Risk/Trade: {RISK_PER_TRADE*100}%")
    print(f"  Max Concurrent Positions: {args.max_positions}")
    if args.validate:
        print(f"  Mode: VALIDATION (generating {args.validate_count} setup charts)")
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

    # Fetch all ticker data
    print(f"\nFetching data for {len(tickers)} tickers...")
    data = fetch_all(tickers, start_date, end_date)
    if not data:
        print("ERROR: No data fetched. Check network connection.")
        sys.exit(1)

    # ── Validation mode: just generate setup charts and exit ──
    if args.validate:
        paths = generate_validation_charts(data, start_date, n_charts=args.validate_count)
        if paths:
            print(f"\nValidation complete. Review the {len(paths)} charts in results/validation/")
            print("If the setups look correct, re-run without --validate for full backtest.")
        else:
            print("\nNo setups found. Check your filters or try different tickers.")
        return

    # ── Full backtest mode ──
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

    # Run backtest
    trades, equity_curve = run_backtest(
        data, market_regime, start_date, end_date,
        skip_bear=not args.no_regime,
        max_positions=args.max_positions,
    )

    # Compute metrics and generate reports
    result = compute_metrics(trades, equity_curve, start_date, end_date)
    generate_all_reports(result, trades, equity_curve, start_date, end_date)


if __name__ == "__main__":
    main()
