"""
Data models for trades and backtest results.
"""
from dataclasses import dataclass, field, asdict


@dataclass
class Trade:
    ticker: str
    entry_date: str
    entry_price: float
    stop_price: float
    shares: float
    position_value: float
    risk_amount: float = 0.0
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

    def to_dict(self) -> dict:
        return asdict(self)


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
    cagr: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_holding_days: float = 0.0
    total_pnl: float = 0.0
    ending_capital: float = 0.0
    trades_per_year: float = 0.0
    trades_in_bull: int = 0
    trades_in_bear: int = 0
    win_rate_bull: float = 0.0
    win_rate_bear: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)
