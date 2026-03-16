"""
Analysis, visualization, and reporting.

Generates:
  - Console summary
  - Equity curve plot vs SPY buy-and-hold
  - Drawdown chart
  - Monthly/yearly returns heatmap
  - Trade log CSV
  - JSON results
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

from .config import STARTING_CAPITAL, RESULTS_DIR, START_DATE, END_DATE
from .models import Trade, BacktestResult


def print_results(result: BacktestResult, trades: list[Trade]):
    """Print formatted results to console."""
    print("\n" + "=" * 70)
    print("  MORGAN TRADES SAR STRATEGY — BACKTEST RESULTS")
    print("=" * 70)
    print(f"  Period:            {START_DATE} to {END_DATE}")
    print(f"  Starting Capital:  ${STARTING_CAPITAL:,.0f}")
    print(f"  Ending Capital:    ${result.ending_capital:,.0f}")
    print(f"  Total Return:      {result.total_return_pct:+.1f}%")
    print(f"  CAGR:              {result.cagr:+.1f}%")
    print(f"  Total PnL:         ${result.total_pnl:+,.0f}")
    print("-" * 70)
    print(f"  Total Trades:      {result.total_trades}")
    print(f"  Trades/Year:       {result.trades_per_year}")
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
    print(f"  Bull Regime:       {result.trades_in_bull} trades (Win Rate: {result.win_rate_bull}%)")
    print(f"  Bear Regime:       {result.trades_in_bear} trades (Win Rate: {result.win_rate_bear}%)")
    print("=" * 70)

    if trades:
        sorted_trades = sorted(trades, key=lambda t: t.r_multiple, reverse=True)
        print("\n  TOP 10 TRADES:")
        print(f"  {'Ticker':<8} {'Entry Date':<12} {'R-Multiple':>10} {'PnL':>10} {'Exit Reason':<15}")
        print("  " + "-" * 60)
        for t in sorted_trades[:10]:
            print(f"  {t.ticker:<8} {t.entry_date:<12} {t.r_multiple:>10.1f}R  ${t.pnl:>8,.0f}  {t.exit_reason:<15}")

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


def save_trade_log_csv(trades: list[Trade]):
    """Save all trades to a CSV file."""
    if not trades:
        return
    df = pd.DataFrame([t.to_dict() for t in trades])
    path = RESULTS_DIR / "trade_log.csv"
    df.to_csv(path, index=False)
    print(f"  Trade log saved to {path}")


def save_results_json(result: BacktestResult, trades: list[Trade]):
    """Save summary + trades to JSON."""
    output = {
        "summary": result.to_dict(),
        "params": {
            "starting_capital": STARTING_CAPITAL,
            "start_date": START_DATE,
            "end_date": END_DATE,
        },
        "trades": [t.to_dict() for t in trades],
    }
    path = RESULTS_DIR / "backtest_results.json"
    with open(path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Results JSON saved to {path}")


def plot_equity_curve(equity_curve: list[tuple[str, float]], trades: list[Trade]):
    """Generate equity curve + R-multiple distribution chart."""
    if len(equity_curve) < 2:
        print("  Not enough data for equity curve")
        return

    dates = [pd.Timestamp(ec[0]) for ec in equity_curve]
    capitals = [ec[1] for ec in equity_curve]

    fig, axes = plt.subplots(3, 1, figsize=(14, 14), height_ratios=[3, 1, 1])
    fig.suptitle("Morgan SAR Breakout Strategy — Backtest Results", fontsize=14, fontweight="bold")

    # ── Panel 1: Equity curve ──
    ax1 = axes[0]
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

    # ── Panel 2: Drawdown ──
    ax2 = axes[1]
    peak = capitals[0]
    drawdowns = []
    for c in capitals:
        peak = max(peak, c)
        drawdowns.append((c - peak) / peak * 100)
    ax2.fill_between(dates, 0, drawdowns, color="red", alpha=0.3)
    ax2.plot(dates, drawdowns, "r-", linewidth=0.8)
    ax2.set_ylabel("Drawdown (%)")
    ax2.grid(True, alpha=0.3)

    # ── Panel 3: R-multiple distribution ──
    ax3 = axes[2]
    if trades:
        r_mults = [t.r_multiple for t in trades]
        colors = ["green" if r > 0 else "red" for r in r_mults]
        ax3.bar(range(len(r_mults)), r_mults, color=colors, alpha=0.7, width=1.0)
        ax3.axhline(y=0, color="black", linewidth=0.5)
        ax3.set_ylabel("R-Multiple")
        ax3.set_xlabel("Trade #")
        ax3.grid(True, alpha=0.3)

    plt.tight_layout()
    path = RESULTS_DIR / "equity_curve.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Equity curve saved to {path}")


def plot_monthly_heatmap(trades: list[Trade]):
    """Generate monthly returns heatmap."""
    if not trades:
        return

    monthly = {}
    for t in trades:
        key = t.entry_date[:7]  # YYYY-MM
        monthly[key] = monthly.get(key, 0) + t.pnl

    if not monthly:
        return

    # Build a DataFrame with years as rows, months as columns
    data = {}
    for ym, pnl in monthly.items():
        year, month = ym.split("-")
        year = int(year)
        month = int(month)
        if year not in data:
            data[year] = {}
        data[year][month] = pnl

    years = sorted(data.keys())
    months = list(range(1, 13))
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    matrix = []
    for year in years:
        row = [data[year].get(m, 0) for m in months]
        matrix.append(row)

    matrix = np.array(matrix)

    fig, ax = plt.subplots(figsize=(12, max(3, len(years) + 1)))
    cmap = plt.cm.RdYlGn
    vmax = max(abs(matrix.min()), abs(matrix.max())) or 1
    im = ax.imshow(matrix, cmap=cmap, vmin=-vmax, vmax=vmax, aspect="auto")

    ax.set_xticks(range(12))
    ax.set_xticklabels(month_names)
    ax.set_yticks(range(len(years)))
    ax.set_yticklabels(years)

    # Annotate cells
    for i in range(len(years)):
        for j in range(12):
            val = matrix[i, j]
            if val != 0:
                color = "white" if abs(val) > vmax * 0.6 else "black"
                ax.text(j, i, f"${val:,.0f}", ha="center", va="center",
                        color=color, fontsize=8)

    # Yearly totals on the right
    yearly_totals = matrix.sum(axis=1)
    for i, total in enumerate(yearly_totals):
        ax.text(12.3, i, f"${total:+,.0f}", ha="left", va="center",
                fontsize=9, fontweight="bold",
                color="green" if total > 0 else "red")

    ax.set_title("Monthly PnL Heatmap", fontsize=12, fontweight="bold")
    plt.colorbar(im, ax=ax, label="PnL ($)", shrink=0.8)
    plt.tight_layout()

    path = RESULTS_DIR / "monthly_heatmap.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Monthly heatmap saved to {path}")


def generate_all_reports(result: BacktestResult, trades: list[Trade],
                         equity_curve: list[tuple[str, float]]):
    """Generate all reports and charts."""
    print("\nGenerating reports...")
    print_results(result, trades)
    save_trade_log_csv(trades)
    save_results_json(result, trades)
    plot_equity_curve(equity_curve, trades)
    plot_monthly_heatmap(trades)
    print("Done!")
