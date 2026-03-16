"""
Analysis, visualization, and reporting (v2).

Generates:
  1. Console summary with all metrics
  2. Trade log CSV
  3. Equity curve with drawdown overlay
  4. Monthly returns heatmap
  5. Regime comparison (bull vs bear performance)
  6. R-multiple distribution
  7. JSON results
"""
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

from .config2 import STARTING_CAPITAL, RESULTS_DIR
from .models2 import Trade, BacktestResult


# ─── Console Output ─────────────────────────────────────────────────────────

def print_results(result: BacktestResult, trades: list[Trade], start_date: str, end_date: str):
    """Print formatted results to console."""
    print("\n" + "=" * 70)
    print("  MORGAN TRADES SAR STRATEGY — BACKTEST RESULTS (v2)")
    print("=" * 70)
    print(f"  Period:            {start_date} to {end_date}")
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
    print(f"  Sortino Ratio:     {result.sortino_ratio}")
    print(f"  Max Drawdown:      {result.max_drawdown_pct}%")
    print("-" * 70)
    print(f"  Avg Winner (R):    {result.avg_winner_r}R")
    print(f"  Avg Loser (R):     {result.avg_loser_r}R")
    print(f"  Best Trade (R):    {result.best_trade_r}R")
    print(f"  Worst Trade (R):   {result.worst_trade_r}R")
    print(f"  Avg Hold Days:     {result.avg_holding_days}")
    print("-" * 70)
    print(f"  Bull Regime:       {result.trades_in_bull} trades | "
          f"Win Rate: {result.win_rate_bull}% | PnL: ${result.pnl_bull:+,.0f}")
    print(f"  Bear Regime:       {result.trades_in_bear} trades | "
          f"Win Rate: {result.win_rate_bear}% | PnL: ${result.pnl_bear:+,.0f}")
    print("=" * 70)

    if not trades:
        return

    # Top 10 trades
    sorted_trades = sorted(trades, key=lambda t: t.r_multiple, reverse=True)
    print("\n  TOP 10 TRADES:")
    print(f"  {'Ticker':<8} {'Entry':<12} {'Exit':<12} {'R':>6} {'PnL':>10} {'Reason':<16}")
    print("  " + "-" * 68)
    for t in sorted_trades[:10]:
        print(f"  {t.ticker:<8} {t.entry_date:<12} {t.exit_date:<12} "
              f"{t.r_multiple:>5.1f}R ${t.pnl:>8,.0f}  {t.exit_reason:<16}")

    # Worst 5
    print("\n  WORST 5 TRADES:")
    for t in sorted_trades[-5:]:
        print(f"  {t.ticker:<8} {t.entry_date:<12} {t.exit_date:<12} "
              f"{t.r_multiple:>5.1f}R ${t.pnl:>8,.0f}  {t.exit_reason:<16}")

    # Exit reason breakdown
    reasons = {}
    for t in trades:
        r = t.exit_reason
        if r not in reasons:
            reasons[r] = {"count": 0, "pnl": 0, "wins": 0}
        reasons[r]["count"] += 1
        reasons[r]["pnl"] += t.pnl
        if t.pnl > 0:
            reasons[r]["wins"] += 1

    print("\n  EXIT REASON BREAKDOWN:")
    print(f"  {'Reason':<18} {'Count':>6} {'Win%':>6} {'PnL':>10}")
    print("  " + "-" * 44)
    for reason in sorted(reasons.keys()):
        r = reasons[reason]
        wr = r["wins"] / r["count"] * 100 if r["count"] > 0 else 0
        print(f"  {reason:<18} {r['count']:>6} {wr:>5.0f}% ${r['pnl']:>9,.0f}")

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


# ─── CSV / JSON ─────────────────────────────────────────────────────────────

def save_trade_log_csv(trades: list[Trade]):
    """Save all trades to a CSV file."""
    if not trades:
        return
    df = pd.DataFrame([t.to_dict() for t in trades])
    cols = [
        "ticker", "entry_date", "exit_date", "entry_price", "exit_price",
        "stop_price", "shares", "position_value", "risk_amount",
        "pnl", "pnl_pct", "r_multiple", "holding_days",
        "exit_reason", "partial_sold", "partial_pnl", "market_regime",
    ]
    df = df[[c for c in cols if c in df.columns]]
    path = RESULTS_DIR / "trade_log2.csv"
    df.to_csv(path, index=False)
    print(f"  Trade log saved to {path}")


def save_results_json(result: BacktestResult, trades: list[Trade], start_date: str, end_date: str):
    """Save summary + trades to JSON."""
    output = {
        "summary": result.to_dict(),
        "params": {
            "starting_capital": STARTING_CAPITAL,
            "start_date": start_date,
            "end_date": end_date,
        },
        "trades": [t.to_dict() for t in trades],
    }
    path = RESULTS_DIR / "backtest_results2.json"
    with open(path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Results JSON saved to {path}")


# ─── Charts ─────────────────────────────────────────────────────────────────

def plot_equity_and_drawdown(equity_curve: list[tuple[str, float]], trades: list[Trade]):
    """Generate equity curve with drawdown overlay (requested output #2)."""
    if len(equity_curve) < 2:
        print("  Not enough data for equity curve")
        return

    dates = [pd.Timestamp(ec[0]) for ec in equity_curve]
    capitals = [ec[1] for ec in equity_curve]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 10), height_ratios=[3, 1],
        gridspec_kw={"hspace": 0.15}
    )
    fig.suptitle("Morgan SAR Strategy — Equity Curve & Drawdown", fontsize=14, fontweight="bold")

    # ── Equity curve ──
    ax1.plot(dates, capitals, "b-", linewidth=1.5, label="Portfolio Value")
    ax1.axhline(y=STARTING_CAPITAL, color="gray", linestyle="--", alpha=0.5, label="Starting Capital")
    ax1.fill_between(dates, STARTING_CAPITAL, capitals, alpha=0.08,
                     where=[c >= STARTING_CAPITAL for c in capitals], color="green")
    ax1.fill_between(dates, STARTING_CAPITAL, capitals, alpha=0.08,
                     where=[c < STARTING_CAPITAL for c in capitals], color="red")
    ax1.set_ylabel("Portfolio Value ($)")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"${x:,.0f}"))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=6))

    # ── Drawdown ──
    peak = capitals[0]
    drawdowns = []
    for c in capitals:
        peak = max(peak, c)
        drawdowns.append((c - peak) / peak * 100)

    ax2.fill_between(dates, 0, drawdowns, color="red", alpha=0.3)
    ax2.plot(dates, drawdowns, "r-", linewidth=0.8)
    ax2.set_ylabel("Drawdown (%)")
    ax2.set_xlabel("Date")
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=6))

    plt.tight_layout()
    path = RESULTS_DIR / "equity_curve2.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Equity curve saved to {path}")


def plot_monthly_heatmap(trades: list[Trade]):
    """Generate monthly returns heatmap (requested output #4)."""
    if not trades:
        return

    monthly = {}
    for t in trades:
        key = t.entry_date[:7]
        monthly[key] = monthly.get(key, 0) + t.pnl

    if not monthly:
        return

    # Build matrix: years as rows, months as columns
    data = {}
    for ym, pnl in monthly.items():
        year, month = int(ym[:4]), int(ym[5:7])
        if year not in data:
            data[year] = {}
        data[year][month] = pnl

    years = sorted(data.keys())
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    matrix = np.array([[data[y].get(m, 0) for m in range(1, 13)] for y in years])

    fig, ax = plt.subplots(figsize=(14, max(3, len(years) + 1)))
    vmax = max(abs(matrix.min()), abs(matrix.max())) or 1
    im = ax.imshow(matrix, cmap="RdYlGn", vmin=-vmax, vmax=vmax, aspect="auto")

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
                        color=color, fontsize=7)

    # Yearly totals
    yearly_totals = matrix.sum(axis=1)
    for i, total in enumerate(yearly_totals):
        ax.text(12.5, i, f"${total:+,.0f}", ha="left", va="center",
                fontsize=9, fontweight="bold",
                color="green" if total > 0 else "red")

    ax.set_title("Monthly PnL Heatmap", fontsize=13, fontweight="bold")
    plt.colorbar(im, ax=ax, label="PnL ($)", shrink=0.8)
    plt.tight_layout()

    path = RESULTS_DIR / "monthly_heatmap2.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Monthly heatmap saved to {path}")


def plot_regime_comparison(trades: list[Trade]):
    """Generate bull vs bear regime performance comparison (requested output #5)."""
    if not trades:
        return

    bull = [t for t in trades if t.market_regime == "BULL"]
    bear = [t for t in trades if t.market_regime == "BEAR"]

    if not bull and not bear:
        return

    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    fig.suptitle("Bull vs Bear Regime Comparison", fontsize=13, fontweight="bold")

    # ── Panel 1: Trade count & win rate ──
    ax = axes[0]
    labels = ["Bull", "Bear"]
    counts = [len(bull), len(bear)]
    win_rates = [
        len([t for t in bull if t.pnl > 0]) / len(bull) * 100 if bull else 0,
        len([t for t in bear if t.pnl > 0]) / len(bear) * 100 if bear else 0,
    ]
    x = np.arange(len(labels))
    bars = ax.bar(x, counts, color=["#2ecc71", "#e74c3c"], alpha=0.7, width=0.5)
    ax.set_ylabel("Trade Count")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_title("Trades by Regime")
    # Annotate with win rates
    for i, (bar, wr) in enumerate(zip(bars, win_rates)):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                f"WR: {wr:.0f}%", ha="center", fontsize=10, fontweight="bold")
    ax.grid(True, alpha=0.3, axis="y")

    # ── Panel 2: PnL comparison ──
    ax = axes[1]
    pnls = [sum(t.pnl for t in bull), sum(t.pnl for t in bear)]
    colors = ["#2ecc71" if p >= 0 else "#e74c3c" for p in pnls]
    bars = ax.bar(x, pnls, color=colors, alpha=0.7, width=0.5)
    ax.set_ylabel("Total PnL ($)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_title("PnL by Regime")
    ax.axhline(y=0, color="black", linewidth=0.5)
    for bar, pnl in zip(bars, pnls):
        ax.text(bar.get_x() + bar.get_width() / 2,
                bar.get_height() + (50 if pnl >= 0 else -200),
                f"${pnl:+,.0f}", ha="center", fontsize=9, fontweight="bold")
    ax.grid(True, alpha=0.3, axis="y")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"${x:,.0f}"))

    # ── Panel 3: R-multiple distribution ──
    ax = axes[2]
    if bull:
        bull_rs = [t.r_multiple for t in bull]
        ax.hist(bull_rs, bins=20, alpha=0.5, color="#2ecc71", label=f"Bull (avg {np.mean(bull_rs):.1f}R)")
    if bear:
        bear_rs = [t.r_multiple for t in bear]
        ax.hist(bear_rs, bins=20, alpha=0.5, color="#e74c3c", label=f"Bear (avg {np.mean(bear_rs):.1f}R)")
    ax.set_xlabel("R-Multiple")
    ax.set_ylabel("Frequency")
    ax.set_title("R-Multiple Distribution")
    ax.axvline(x=0, color="black", linewidth=0.5)
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    path = RESULTS_DIR / "regime_comparison2.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Regime comparison saved to {path}")


def plot_r_distribution(trades: list[Trade]):
    """Plot R-multiple bar chart for all trades."""
    if not trades:
        return

    fig, ax = plt.subplots(figsize=(14, 4))
    r_mults = [t.r_multiple for t in trades]
    colors = ["#2ecc71" if r > 0 else "#e74c3c" for r in r_mults]
    ax.bar(range(len(r_mults)), r_mults, color=colors, alpha=0.7, width=1.0)
    ax.axhline(y=0, color="black", linewidth=0.5)
    ax.set_ylabel("R-Multiple")
    ax.set_xlabel("Trade #")
    ax.set_title("R-Multiple per Trade (chronological)", fontweight="bold")
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    path = RESULTS_DIR / "r_distribution2.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  R-distribution saved to {path}")


# ─── Generate All ───────────────────────────────────────────────────────────

def generate_all_reports(
    result: BacktestResult,
    trades: list[Trade],
    equity_curve: list[tuple[str, float]],
    start_date: str,
    end_date: str,
):
    """Generate all reports, charts, and data files."""
    print("\nGenerating reports...")
    print_results(result, trades, start_date, end_date)
    save_trade_log_csv(trades)
    save_results_json(result, trades, start_date, end_date)
    plot_equity_and_drawdown(equity_curve, trades)
    plot_monthly_heatmap(trades)
    plot_regime_comparison(trades)
    plot_r_distribution(trades)
    print("\nAll reports generated in results/ directory.")
