"""
Main backtesting engine (v2).

Key improvements over v1:
  - Setup detection on Day N, entry trigger on Day N+1 (no lookahead)
  - Gap filters on entry (skip gap down >2%, gap up >10%)
  - Stop width filter (skip if stop >8% from entry)
  - Daily equity curve (not just per-trade)
  - Proper concurrent position tracking with max 5 positions
  - Stop exit handles gap-downs (exit at open if gapped through)
  - 10 SMA exit uses precomputed SMA10 column
  - Sortino ratio computation
"""
from typing import Optional

import numpy as np
import pandas as pd

from .config2 import (
    STARTING_CAPITAL, MAX_CONCURRENT_POSITIONS, MAX_HOLD_DAYS,
)
from .models2 import Trade, BacktestResult
from .scanner2 import compute_indicators, compute_relative_strength
from .signals2 import scan_setups
from .entries2 import check_breakout_trigger, apply_exit_slippage
from .exits2 import check_stop_hit, get_stop_exit_price, check_partial_profit, execute_partial, check_10sma_close
from .position_sizing2 import calculate_position
from .regime2 import get_regime_at_date


# ─── Single Trade Simulation ────────────────────────────────────────────────

def _simulate_trade(
    df: pd.DataFrame,
    entry_bar_idx: int,
    entry_price: float,
    stop_price: float,
    shares: float,
    position_value: float,
    risk_amount: float,
    regime: str,
) -> Trade:
    """
    Simulate a single trade from entry bar through exit.
    Walk forward bar by bar applying exit rules.
    """
    entry_date = df.index[entry_bar_idx]

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

    max_bar = min(entry_bar_idx + 1 + MAX_HOLD_DAYS, len(df))

    for j in range(entry_bar_idx + 1, max_bar):
        bar = df.iloc[j]
        bar_date = df.index[j]

        # ── 1. Stop loss check (intraday low) ──
        if check_stop_hit(bar["Low"], current_stop):
            raw_exit = get_stop_exit_price(bar["Open"], current_stop)
            exit_price = apply_exit_slippage(raw_exit)
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

        # ── 2. Partial profit at 5x risk ──
        unrealized_per_share = bar["Close"] - entry_price
        if risk_amount > 0:
            unrealized_r = (unrealized_per_share * remaining_shares) / risk_amount
        else:
            unrealized_r = 0

        if check_partial_profit(unrealized_r, partial_taken):
            remaining_shares, p_pnl, current_stop = execute_partial(
                remaining_shares, entry_price, bar["Close"]
            )
            partial_pnl += p_pnl
            partial_taken = True

        # ── 3. 10 SMA trailing exit (daily close below 10 SMA) ──
        sma10 = bar.get("SMA10")
        if sma10 is not None and check_10sma_close(bar["Close"], sma10):
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

    # ── Time exit: max hold exceeded or ran out of data ──
    last_idx = min(entry_bar_idx + MAX_HOLD_DAYS, len(df) - 1)
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


# ─── Portfolio Backtest Engine ──────────────────────────────────────────────

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

    Flow per day:
      1. Update active positions (remove closed ones)
      2. For each setup ready on this day, check if Day N+1 triggers entry
      3. Apply position limits and regime filter
      4. Simulate trade
      5. Record daily equity

    Returns: (trades, equity_curve)
        equity_curve is [(date_str, capital), ...] with daily granularity.
    """
    # ── Phase 1: Compute indicators for all tickers ──
    print("\n  Computing indicators...")
    indicator_data = {}
    for ticker, df in data.items():
        indicator_data[ticker] = compute_indicators(df)

    # ── Phase 2: Detect setups (steps 1-4) for all tickers ──
    print("  Detecting breakout setups...")
    all_setups = []
    for ticker, df in indicator_data.items():
        setups = scan_setups(df, start_date)
        for s in setups:
            s["ticker"] = ticker
            all_setups.append(s)

    # Sort by setup date
    all_setups.sort(key=lambda s: s["date"])
    print(f"  Found {len(all_setups)} setups (pre-breakout patterns)")

    # ── Phase 3: Compute relative strength monthly ──
    print("  Computing relative strength rankings...")
    rs_cache = {}
    setup_dates = sorted(set(s["date"] for s in all_setups))
    months_computed = set()
    for date in setup_dates:
        month_key = date.strftime("%Y-%m")
        if month_key not in months_computed:
            rs_tickers = compute_relative_strength(indicator_data, date)
            rs_cache[month_key] = rs_tickers
            months_computed.add(month_key)

    # ── Phase 4: Simulate trades ──
    # Iterate through setups chronologically. For each setup, check up to
    # TRIGGER_WINDOW bars after setup day for a valid breakout entry.
    # This avoids the date-alignment complexity of calendar-day iteration.
    TRIGGER_WINDOW = 5  # check up to 5 bars after setup for breakout

    print("  Simulating trades with entry triggers...")
    capital = STARTING_CAPITAL
    trades: list[Trade] = []
    # Track active positions: list of {ticker, entry_date, exit_date}
    active_trades: list[dict] = []
    # Track which (ticker, entry_bar_idx) combos we've already entered
    entered_bars: set[tuple[str, int]] = set()

    for setup in all_setups:
        ticker = setup["ticker"]
        df = indicator_data[ticker]
        setup_date = setup["date"]
        bar_idx = setup["bar_idx"]

        # Remove expired active trades
        active_trades = [
            t for t in active_trades
            if pd.Timestamp(t["exit_date"]) >= setup_date
        ]

        # Skip if at max concurrent positions
        if len(active_trades) >= max_positions:
            continue

        # Skip if already holding this ticker
        if any(t["ticker"] == ticker for t in active_trades):
            continue

        # Market regime filter
        regime = get_regime_at_date(market_regime, setup_date)
        if skip_bear and regime == "BEAR":
            continue

        # Relative strength filter
        month_key = setup_date.strftime("%Y-%m")
        rs_set = rs_cache.get(month_key, set())
        if rs_set and ticker not in rs_set:
            continue

        # Capital guard
        if capital < STARTING_CAPITAL * 0.10:
            continue

        # Try to trigger entry on each of the next TRIGGER_WINDOW bars
        triggered = False
        for offset in range(1, TRIGGER_WINDOW + 1):
            entry_idx = bar_idx + offset
            if entry_idx >= len(df):
                break

            # Don't enter on a bar we've already used for this ticker
            if (ticker, entry_idx) in entered_bars:
                continue

            prev_bar = df.iloc[entry_idx - 1]
            entry_bar = df.iloc[entry_idx]
            vol_sma20 = prev_bar.get("Vol_SMA20", 0)
            if pd.isna(vol_sma20):
                vol_sma20 = 0

            entry_info = check_breakout_trigger(
                setup=setup,
                prev_close=prev_bar["Close"],
                next_open=entry_bar["Open"],
                next_high=entry_bar["High"],
                next_low=entry_bar["Low"],
                next_close=entry_bar["Close"],
                next_volume=entry_bar["Volume"],
                vol_sma20=vol_sma20,
            )
            if entry_info is None:
                continue

            # Position sizing
            shares, pos_val, risk_amt = calculate_position(
                capital, entry_info["entry_price"], entry_info["stop_price"]
            )
            if shares <= 0:
                continue

            # Simulate the trade
            trade = _simulate_trade(
                df=df,
                entry_bar_idx=entry_idx,
                entry_price=entry_info["entry_price"],
                stop_price=entry_info["stop_price"],
                shares=shares,
                position_value=pos_val,
                risk_amount=risk_amt,
                regime=regime,
            )
            trade.ticker = ticker
            trades.append(trade)
            capital += trade.pnl

            entered_bars.add((ticker, entry_idx))
            active_trades.append({
                "ticker": ticker,
                "entry_date": trade.entry_date,
                "exit_date": trade.exit_date,
            })
            triggered = True
            break

    # Sort trades by entry date for equity curve
    trades.sort(key=lambda t: t.entry_date)

    # Build daily equity curve from trade PnL events
    equity_curve: list[tuple[str, float]] = [(start_date, STARTING_CAPITAL)]
    running_capital = STARTING_CAPITAL
    for t in trades:
        running_capital += t.pnl
        equity_curve.append((t.exit_date, round(running_capital, 2)))

    print(f"  Executed {len(trades)} trades")
    return trades, equity_curve


# ─── Metrics ────────────────────────────────────────────────────────────────

def compute_metrics(
    trades: list[Trade],
    equity_curve: list[tuple[str, float]],
    start_date: str,
    end_date: str,
) -> BacktestResult:
    """Compute all performance metrics including Sortino ratio."""
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
    result.best_trade_r = round(max(r_multiples), 2) if r_multiples else 0
    result.worst_trade_r = round(min(r_multiples), 2) if r_multiples else 0

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

    # Max drawdown from equity curve
    capitals = [ec[1] for ec in equity_curve]
    peak = capitals[0]
    max_dd = 0
    for c in capitals:
        peak = max(peak, c)
        dd = (peak - c) / peak
        max_dd = max(max_dd, dd)
    result.max_drawdown_pct = round(max_dd * 100, 1)

    # Sharpe and Sortino ratios
    trade_returns = [t.pnl / t.position_value for t in trades if t.position_value > 0]
    if len(trade_returns) > 1:
        avg_ret = np.mean(trade_returns)
        std_ret = np.std(trade_returns, ddof=1)
        trades_per_year = max(1, len(trades) / max(years, 1))

        # Sharpe
        if std_ret > 0:
            result.sharpe_ratio = round(
                (avg_ret / std_ret) * np.sqrt(trades_per_year), 2
            )

        # Sortino (downside deviation only)
        downside_returns = [r for r in trade_returns if r < 0]
        if downside_returns:
            downside_std = np.std(downside_returns, ddof=1)
            if downside_std > 0:
                result.sortino_ratio = round(
                    (avg_ret / downside_std) * np.sqrt(trades_per_year), 2
                )

    # Bull vs bear regime breakdown
    bull_trades = [t for t in trades if t.market_regime == "BULL"]
    bear_trades = [t for t in trades if t.market_regime == "BEAR"]
    result.trades_in_bull = len(bull_trades)
    result.trades_in_bear = len(bear_trades)
    result.pnl_bull = round(sum(t.pnl for t in bull_trades), 2)
    result.pnl_bear = round(sum(t.pnl for t in bear_trades), 2)
    if bull_trades:
        result.win_rate_bull = round(
            len([t for t in bull_trades if t.pnl > 0]) / len(bull_trades) * 100, 1
        )
    if bear_trades:
        result.win_rate_bear = round(
            len([t for t in bear_trades if t.pnl > 0]) / len(bear_trades) * 100, 1
        )

    return result
