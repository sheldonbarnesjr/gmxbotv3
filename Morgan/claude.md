# Morgan Trades SAR Strategy Backtester

## Project Overview
This project implements and backtests the Morgan Trades SAR Swing Trading Strategy - a momentum-based breakout system for US equities. The strategy targets the top 2% relative strength stocks and uses Opening Range Breakouts (ORB) for precise entries with strict risk management.

## Strategy Summary

### Core Philosophy
- **Style**: Swing trading (hold days to weeks)
- **Edge**: Trading the strongest momentum stocks during favorable market conditions
- **Risk**: Max 1% account risk per trade, position sizes 10-40% of account

### The Three Setups
1. **The Breakout** (Primary) - Bull flag/consolidation breakouts on daily chart
2. **Episodic Pivots** - 10%+ gap-ups on major catalysts (earnings, news)
3. **Parabolic Puts** - Shorting extremely extended moves (secondary)

### Key Rules Reference

#### Stock Universe Filters
```
Price > $1.00
ADR% > 5%
Average Daily Dollar Volume > $3,500,000
Relative Strength: Top 2% gainers (1M, 3M, or 6M)
```

#### Breakout Entry Conditions
```
1. Prior move >= 30% over multiple days/weeks
2. 10 SMA slope > 0 (inclining)
3. 20 SMA slope > 0 (inclining)
4. Consolidation: Higher lows + Lower highs (tightening range)
5. Volume decreasing during pullback
6. Breakout on above-average volume
```

#### Entry Trigger
```
Entry: Break of 1-min or 5-min Opening Range High
Condition: Must coincide with daily chart breakout confirmation
Stop Loss: Low of Day (LOD) - NO EXCEPTIONS
```

#### Position Sizing
```python
risk_amount = account_value * 0.01  # 1% risk
shares = risk_amount / (entry_price - stop_price)
position_value = shares * entry_price  # Should be 10-40% of account
```

#### Exit Rules
```
1. Stop Loss: Sell 100% if LOD stop hit
2. First Profit: Sell 10-30% when profit >= 5x risk, move stop to breakeven
3. Full Exit: Sell 100% if daily close < 10 SMA
```

#### Market Regime Filter
```
Index: $IXIC (NASDAQ Composite) or $QQQ daily chart
BULLISH: 10 SMA > 20 SMA → Trade normally
BEARISH: 10 SMA < 20 SMA → Reduce size or don't trade
```

## Technical Implementation Notes

### Data Requirements
- Daily OHLCV data for US equities (preferably 5+ years)
- Intraday 1-min or 5-min data for entry precision (optional but recommended)
- Index data ($IXIC, $QQQ) for market regime filter
- Fundamental data for relative strength ranking (optional - can use price performance)

### Recommended Data Sources
- **Yahoo Finance** (yfinance) - Free, good for daily data
- **Polygon.io** - Better for intraday, has free tier
- **Alpha Vantage** - Free tier available
- **Tiingo** - Good free tier for daily data

### Key Libraries
```
pandas, numpy - Data manipulation
yfinance - Market data
ta / pandas-ta - Technical indicators
matplotlib, plotly - Visualization
```

### Backtesting Considerations
1. **Lookahead Bias**: Calculate relative strength rankings using only past data
2. **Survivorship Bias**: Include delisted stocks if possible
3. **Slippage**: Add 0.1-0.5% slippage on entries/exits
4. **Commission**: $0 for most brokers now, but can add if needed
5. **Position Limits**: Max 3-5 concurrent positions recommended

## File Structure
```
morgan_trades_backtest/
├── CLAUDE.md                 # This file
├── data/                     # Historical price data
├── src/
│   ├── scanner.py           # Stock universe filtering & RS ranking
│   ├── signals.py           # Breakout pattern detection
│   ├── entries.py           # ORB entry logic
│   ├── exits.py             # Stop loss & profit taking
│   ├── position_sizing.py   # Risk-based position sizing
│   ├── regime.py            # Market condition filter
│   └── backtest.py          # Main backtesting engine
├── results/                  # Backtest outputs
├── notebooks/               # Analysis notebooks
└── requirements.txt
```

## Performance Metrics to Track
- Win Rate (%)
- Average Win / Average Loss (R-multiple)
- Profit Factor
- Max Drawdown
- Sharpe Ratio
- Total Return (%)
- CAGR
- Number of Trades
- Average Holding Period
- Best/Worst Trade

## Development Priorities
1. Get basic breakout detection working on daily data
2. Implement position sizing and risk management
3. Add market regime filter
4. Run initial backtest on 2020-2024 data
5. Optimize and validate on out-of-sample data
6. Add intraday ORB entries for precision (optional enhancement)

## Notes
- Strategy PDF located at: `morgan_trades_sar_strategy.pdf`
- Original source: sartrading.io/strategy (Morgan Trades / @morgantrades)
- This is a momentum strategy - expect 40-60% win rate with large winners
- The edge comes from cutting losers fast (1% risk) and letting winners run (5x+ R)
