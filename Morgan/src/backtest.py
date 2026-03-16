"""
Main backtesting engine.

Iterates chronologically through trading days, manages multiple concurrent
positions, applies universe filters, detects breakouts, checks regime,
and tracks equity curve.
"""
from typing import Optional

import numpy as np
import pandas as pd

from .config import (
    STARTING_CAPITAL, RISK_PER_TRADE, MAX_CONCURRENT_POSITIONS,
    PARTIAL_SELL_THRESHOLD, PARTIAL_SELL_PCT, MAX_HOLD_DAYS, SLIPPAGE_PCT,
)
from .models import Trade, BacktestResult
from .scanner import compute_indicators, compute_relative_strength
from .signals import detect_breakouts
from .entries import apply_entry_slippage, apply_exit_slippage
from .position_sizing import calculate_position
from .exits import check_stop_loss, check_partial_profit, check_10sma_exit
from .regime import get_regime_at_date


# ─── Trade Simulation ───────────────────────────────────────────────────────

def simulate_trade(
    df: pd.DataFrame,
    signal: dict,
    capital: float,
    market_regime: pd.Series,
) -> Optional[Trade]:
    """Simulate a single trade from entry through exit, bar by bar."""
    raw_entry = signal["entry_price"]
    entry_price = apply_entry_slippage(raw_entry)
    stop_price = signal["stop_price"]
    entry_date = signal["date"]
    bar_idx = signal["bar_idx"]

    shares, position_value, risk_amount = calculate_position(capital, entry_price, stop_price)
    if shares <= 0:
        return None

    regime = get_regime_at_date(market_regime, entry_date)

    trade = Trade(
        ticker="",
        entry_date=entry_date.strftime("%Y-%m-%d"),
        entry_price=round(entry_price, 2),
        stop_price=round(stop_price, 2),
        shares=shares,
        position_value=position_value,
        risk_amount=risk_amount,
        market_regime=regime,
    )

    remaining_shares = shares
    partial_pnl = 0.0
    current_stop = stop_price
    partial_taken = False

    # Walk forward bar by bar
    for j in range(bar_idx + 1, min(bar_idx + 1 + MAX_HOLD_DAYS, len(df))):
        bar = df.iloc[j]
        bar_date = df.index[j]

        # Stop loss check (intraday low)
        if check_stop_loss(bar["Low"], current_stop):
            exit_price = apply_exit_slippage(current_stop)
            pnl = partial_pnl + remaining_shares * (exit_price - entry_price)
            trade.exit_date = bar_date.strftime("%Y-%m-%d")
            trade.exit_price = round(exit_price, 2)
            trade.pnl = round(pnl, 2)
            trade.pnl_pct = round(pnl / position_value * 100, 2)
            trade.r_multiple = round(pnl / risk_amount, 2) if risk_amount > 0 else 0
            trade.holding_days = (bar_date - entry_date).days
            trade.exit_reason = "stop_breakeven" if partial_taken else "stop_loss"
            trade.partial_sold = partial_taken
            trade.partial_pnl = round(partial_pnl, 2)
            return trade

        # Partial profit at 5x risk
        unrealized_per_share = bar["Close"] - entry_price
        unrealized_r = (unrealized_per_share * remaining_shares) / risk_amount if risk_amount > 0 else 0

        if check_partial_profit(unrealized_r, partial_taken):
            sell_shares = remaining_shares * PARTIAL_SELL_PCT
            sell_price = apply_exit_slippage(bar["Close"])
            partial_pnl += sell_shares * (sell_price - entry_price)
            remaining_shares -= sell_shares
            current_stop = entry_price  # breakeven
            partial_taken = True

        # 10 SMA trailing exit
        if check_10sma_exit(df, j):
            exit_price = apply_exit_slippage(bar["Close"])
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

    # Time exit
    last_idx = min(bar_idx + MAX_HOLD_DAYS, len(df) - 1)
    last_bar = df.iloc[last_idx]
    last_date = df.index[last_idx]
    exit_price = apply_exit_slippage(last_bar["Close"])
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


# ─── Portfolio Engine ────────────────────────────────────────────────────────

def run_backtest(
    data: dict[str, pd.DataFrame],
    market_regime: pd.Series,
    start_date: str,
    end_date: str,
    skip_bear: bool = True,
    max_positions: int = MAX_CONCURRENT_POSITIONS,
) -> tuple[list[Trade], list[tuple[str, float]]]:
    """
    Run the full portfolio-level backtest.

    Returns (trades, equity_curve) where equity_curve is [(date_str, capital), ...].
    """
    print("\n  Computing indicators...")
    indicator_data = {}
    for ticker, df in data.items():
        indicator_data[ticker] = compute_indicators(df)

    print("  Detecting breakout signals...")
    all_signals = []
    for ticker, df in indicator_data.items():
        signals = detect_breakouts(df, start_date)
        for s in signals:
            s["ticker"] = ticker
            all_signals.append(s)

    all_signals.sort(key=lambda s: s["date"])
    print(f"  Found {len(all_signals)} raw breakout signals")

    # Compute relative strength monthly (avoid O(n^2))
    print("  Computing relative strength rankings...")
    rs_cache = {}
    trading_dates = sorted(set(s["date"] for s in all_signals))
    months_computed = set()
    for date in trading_dates:
        month_key = date.strftime("%Y-%m")
        if month_key not in months_computed:
            rs_tickers = compute_relative_strength(indicator_data, date)
            rs_cache[month_key] = rs_tickers
            months_computed.add(month_key)

    print("  Simulating trades...")
    capital = STARTING_CAPITAL
    trades: list[Trade] = []
    equity_curve: list[tuple[str, float]] = [(start_date, capital)]

    # Track active positions for concurrent position limits
    active_trades: list[dict] = []  # {ticker, entry_date, exit_date}

    for signal in all_signals:
        ticker = signal["ticker"]
        date = signal["date"]

        # Remove expired active trades
        active_trades = [
            t for t in active_trades
            if pd.Timestamp(t["exit_date"]) >= date
        ]

        # Skip if at max concurrent positions
        if len(active_trades) >= max_positions:
            continue

        # Skip if already holding this ticker
        if any(t["ticker"] == ticker for t in active_trades):
            continue

        # Market regime filter
        regime = get_regime_at_date(market_regime, date)
        if skip_bear and regime == "BEAR":
            continue

        # Relative strength filter
        month_key = date.strftime("%Y-%m")
        rs_set = rs_cache.get(month_key, set())
        if rs_set and ticker not in rs_set:
            continue

        # Capital guard
        if capital < STARTING_CAPITAL * 0.10:
            continue

        # Simulate trade
        df = indicator_data[ticker]
        trade = simulate_trade(df, signal, capital, market_regime)
        if trade is None:
            continue

        trade.ticker = ticker
        trades.append(trade)
        capital += trade.pnl
        equity_curve.append((trade.exit_date, round(capital, 2)))

        # Track for concurrency
        active_trades.append({
            "ticker": ticker,
            "entry_date": trade.entry_date,
            "exit_date": trade.exit_date,
        })

    print(f"  Executed {len(trades)} trades")
    return trades, equity_curve


# ─── Metrics ─────────────────────────────────────────────────────────────────

def compute_metrics(
    trades: list[Trade],
    equity_curve: list[tuple[str, float]],
    start_date: str,
    end_date: str,
) -> BacktestResult:
    """Compute all performance metrics from trade list and equity curve."""
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

    # CAGR
    years = (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days / 365.25
    if years > 0 and result.ending_capital > 0:
        result.cagr = round(
            ((result.ending_capital / STARTING_CAPITAL) ** (1 / years) - 1) * 100, 1
        )

    result.avg_holding_days = round(np.mean([t.holding_days for t in trades]), 1)
    result.trades_per_year = round(len(trades) / years, 1) if years > 0 else 0

    # Max drawdown
    capitals = [ec[1] for ec in equity_curve]
    peak = capitals[0]
    max_dd = 0
    for c in capitals:
        peak = max(peak, c)
        dd = (peak - c) / peak
        max_dd = max(max_dd, dd)
    result.max_drawdown_pct = round(max_dd * 100, 1)

    # Sharpe ratio
    trade_returns = [t.pnl / t.position_value for t in trades if t.position_value > 0]
    if len(trade_returns) > 1:
        avg_ret = np.mean(trade_returns)
        std_ret = np.std(trade_returns, ddof=1)
        trades_per_year = max(1, len(trades) / max(years, 1))
        if std_ret > 0:
            result.sharpe_ratio = round((avg_ret / std_ret) * np.sqrt(trades_per_year), 2)

    # Bull vs bear
    bull_trades = [t for t in trades if t.market_regime == "BULL"]
    bear_trades = [t for t in trades if t.market_regime == "BEAR"]
    result.trades_in_bull = len(bull_trades)
    result.trades_in_bear = len(bear_trades)
    if bull_trades:
        result.win_rate_bull = round(
            len([t for t in bull_trades if t.pnl > 0]) / len(bull_trades) * 100, 1
        )
    if bear_trades:
        result.win_rate_bear = round(
            len([t for t in bear_trades if t.pnl > 0]) / len(bear_trades) * 100, 1
        )

    return result
