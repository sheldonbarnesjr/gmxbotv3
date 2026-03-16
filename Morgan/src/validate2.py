"""
Validation module (v2): generate example setup charts for visual verification.

Produces 10 charts showing detected breakout setups so the user can verify
that the pattern detection logic is finding real bull flag / consolidation
breakout patterns before running the full backtest.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

from .config2 import RESULTS_DIR
from .scanner2 import compute_indicators
from .signals2 import scan_setups


def plot_setup_chart(
    df: pd.DataFrame,
    ticker: str,
    setup: dict,
    chart_idx: int,
    output_dir=None,
):
    """
    Plot a single setup chart showing:
    - Price action (OHLC-style with close line)
    - 10 and 20 SMA
    - Consolidation zone highlighted
    - Volume with 20-day average
    - Breakout bar marked
    """
    if output_dir is None:
        output_dir = RESULTS_DIR / "validation"
    output_dir.mkdir(exist_ok=True)

    bar_idx = setup["bar_idx"]
    consol_bars = setup["consol_bars"]
    move_start = setup["move_start_idx"]

    # Show context: from move start to 10 bars after breakout
    ctx_start = max(0, move_start - 10)
    ctx_end = min(len(df), bar_idx + 15)
    ctx = df.iloc[ctx_start:ctx_end].copy()

    if len(ctx) < 10:
        return

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 8), height_ratios=[3, 1],
        gridspec_kw={"hspace": 0.1}
    )

    dates = ctx.index
    close = ctx["Close"].values
    high = ctx["High"].values
    low = ctx["Low"].values

    # ── Price panel ──
    ax1.plot(dates, close, "k-", linewidth=1.2, label="Close", zorder=3)
    ax1.fill_between(dates, low, high, alpha=0.15, color="steelblue", label="H-L Range")

    # SMAs
    if "SMA10" in ctx.columns:
        sma10 = ctx["SMA10"].values
        ax1.plot(dates, sma10, "b-", linewidth=1, alpha=0.7, label="10 SMA")
    if "SMA20" in ctx.columns:
        sma20 = ctx["SMA20"].values
        ax1.plot(dates, sma20, "r-", linewidth=1, alpha=0.7, label="20 SMA")

    # Highlight consolidation zone
    consol_start_idx = bar_idx - consol_bars - ctx_start
    consol_end_idx = bar_idx - ctx_start
    if 0 <= consol_start_idx < len(dates) and 0 < consol_end_idx <= len(dates):
        consol_dates = dates[max(0, consol_start_idx):consol_end_idx + 1]
        ax1.axvspan(consol_dates[0], consol_dates[-1], alpha=0.1, color="orange",
                    label="Consolidation Zone")
        # Draw consolidation high line
        ax1.axhline(y=setup["consol_high"], color="orange", linestyle="--",
                    alpha=0.6, linewidth=1, label=f"Breakout Level: ${setup['consol_high']:.2f}")

    # Mark the setup day (Day N) and breakout day (Day N+1)
    setup_rel = bar_idx - ctx_start
    if 0 <= setup_rel < len(dates):
        ax1.axvline(x=dates[setup_rel], color="green", linestyle=":", alpha=0.7)
        ax1.plot(dates[setup_rel], close[setup_rel], "g^", markersize=12,
                 zorder=5, label="Setup Day (N)")
    if 0 <= setup_rel + 1 < len(dates):
        ax1.axvline(x=dates[setup_rel + 1], color="blue", linestyle=":", alpha=0.7)
        ax1.plot(dates[setup_rel + 1], close[setup_rel + 1], "b*", markersize=14,
                 zorder=5, label="Entry Day (N+1)")

    setup_date = df.index[bar_idx].strftime("%Y-%m-%d")
    ax1.set_title(
        f"Setup #{chart_idx}: {ticker} — {setup_date}\n"
        f"Higher Lows: {setup['higher_lows_pct']*100:.0f}% | "
        f"Lower Highs: {setup['lower_highs_pct']*100:.0f}% | "
        f"Consol Bars: {consol_bars}",
        fontsize=12, fontweight="bold"
    )
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylabel("Price ($)")
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"${x:.2f}"))

    # ── Volume panel ──
    vol = ctx["Volume"].values
    vol_colors = ["green" if ctx.iloc[j]["Close"] >= ctx.iloc[j]["Open"] else "red"
                  for j in range(len(ctx))]
    ax2.bar(dates, vol, color=vol_colors, alpha=0.6, width=0.8)

    if "Vol_SMA20" in ctx.columns:
        vol_avg = ctx["Vol_SMA20"].values
        ax2.plot(dates, vol_avg, "b-", linewidth=1, alpha=0.7, label="20-day Avg Vol")
        # Show 150% threshold
        ax2.plot(dates, vol_avg * 1.5, "r--", linewidth=0.8, alpha=0.5, label="150% Threshold")

    ax2.set_ylabel("Volume")
    ax2.legend(loc="upper left", fontsize=8)
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

    plt.tight_layout()
    path = output_dir / f"setup_{chart_idx:02d}_{ticker}_{setup_date}.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return str(path)


def generate_validation_charts(
    data: dict[str, pd.DataFrame],
    start_date: str,
    n_charts: int = 10,
) -> list[str]:
    """
    Find setups across all tickers and generate N example charts.
    Picks setups spread across different tickers and dates.

    Returns list of saved chart paths.
    """
    print(f"\n  Generating {n_charts} validation setup charts...")

    # Compute indicators and find setups
    all_setups = []
    indicator_data = {}
    for ticker, df in data.items():
        idf = compute_indicators(df)
        indicator_data[ticker] = idf
        setups = scan_setups(idf, start_date)
        for s in setups:
            s["ticker"] = ticker
            all_setups.append(s)

    if not all_setups:
        print("  No setups found for validation!")
        return []

    # Sort by date, pick evenly spaced setups from different tickers
    all_setups.sort(key=lambda s: s["date"])

    # Try to pick diverse tickers
    selected = []
    seen_tickers = set()
    step = max(1, len(all_setups) // n_charts)

    for i in range(0, len(all_setups), step):
        s = all_setups[i]
        if s["ticker"] not in seen_tickers:
            selected.append(s)
            seen_tickers.add(s["ticker"])
        if len(selected) >= n_charts:
            break

    # If we don't have enough diverse tickers, fill in
    if len(selected) < n_charts:
        for s in all_setups:
            if s not in selected:
                selected.append(s)
            if len(selected) >= n_charts:
                break

    selected = selected[:n_charts]

    # Generate charts
    paths = []
    for idx, setup in enumerate(selected, 1):
        ticker = setup["ticker"]
        df = indicator_data[ticker]
        path = plot_setup_chart(df, ticker, setup, idx)
        if path:
            paths.append(path)
            print(f"    [{idx}/{n_charts}] {ticker} — {df.index[setup['bar_idx']].strftime('%Y-%m-%d')}")

    print(f"  Saved {len(paths)} validation charts to results/validation/")
    return paths
