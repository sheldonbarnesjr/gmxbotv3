# GMXBot Trading Strategy

How the bot manages trades from open to close.

---

## How a Trade Works

1. A signal comes in from the Telegram channel (e.g., "BTC LONG, Entry 95000-96000, TP1 97000, TP2 99000, TP3 102000, SL 93000, 50x")
2. The bot opens the position on-chain with your configured trade size
3. It places all TP (take profit) and SL (stop loss) orders on-chain at the same time
4. As each TP hits, the bot takes partial profit and moves your SL up to lock in gains
5. The position closes when either the final TP hits or the SL gets triggered

You don't have to do anything. The bot handles everything automatically 24/7.

---

## Take Profit (TP) Levels

Signals come with multiple TP levels (usually 2-4, up to 8). The bot doesn't close your entire position at the first target — it takes partial profits at each level.

### How the position closes at each TP

With 3 TPs:

| TP Level | % of Position Closed | What Happens |
|----------|---------------------|--------------|
| TP1 | 20% | Early profit secured |
| TP2 | 50% | Big chunk taken at the second target |
| TP3 | 30% | Rest of the position closes |

With 4 TPs:

| TP Level | % of Position Closed | What Happens |
|----------|---------------------|--------------|
| TP1 | 19% | Early profit |
| TP2 | 40% | Big take at second target |
| TP3 | 1% | Keeps position alive for final target |
| TP4 | 40% | Rest closes at the highest target |

With 5 TPs:

| TP Level | % of Position Closed | What Happens |
|----------|---------------------|--------------|
| TP1 | 19% | Early profit |
| TP2 | 40% | Big take at second target |
| TP3 | 1% | Keeps position alive |
| TP4 | 1% | Keeps position alive |
| TP5 | 39% | Rest closes at the highest target |

The strategy: take a solid chunk at TP1 and a large piece at TP2, then hold through the middle targets with minimal closes (1%) to keep the position alive, and close the rest at the final target. This maximizes profit if the trade runs to its highest target while still locking in gains early.

This pattern continues for 6, 7, and 8 TP signals — always heavy on TP1 + TP2, 1% dust on middle TPs, and the rest on the final TP.

These splits are configurable in your `.env` file. For example, to change the 3-TP split:

```env
TP_3_1=20
TP_3_2=50
TP_3_3=30
```

Or a 5-TP split:

```env
TP_5_1=19
TP_5_2=40
TP_5_3=1
TP_5_4=1
TP_5_5=39
```

Values are percentages and must add up to 100.

### Swing vs Scalp Splits

The bot classifies each signal as either **swing** (longer hold) or **scalp** (quick trade). You can set different TP splits for each:

```env
# Scalp splits (default)
TP_3_1=10
TP_3_2=35
TP_3_3=55

# Swing splits (more evenly distributed)
SWING_TP_3_1=30
SWING_TP_3_2=40
SWING_TP_3_3=30
```

---

## Stop Loss (SL) & Trailing Strategy

Every trade opens with a stop loss. As TPs get hit, the bot automatically moves the SL up to protect your profits. This is called a **trailing stop loss**.

### How the SL moves

| What Happened | SL Moves To | Why |
|---------------|-------------|-----|
| Trade just opened | Original SL from signal | Protect against reversal |
| TP1 hits | Entry price | You're now at **breakeven** — can't lose money on this trade |
| TP2 hits | Entry price (no change) | Let the position run |
| TP3 hits | TP1 price | Lock in TP1 profit level |
| TP4 hits | TP2 price | Lock in TP2 profit level |
| TP5+ hits | Trails 2 levels back | Always locks in profit 2 TPs behind |

### Example walkthrough

Say you open a BTC LONG at $95,000 with:
- SL: $93,000
- TP1: $97,000
- TP2: $99,000
- TP3: $102,000

Here's what happens step by step:

1. **Trade opens** — SL is at $93,000 (if price drops here, you lose ~2%)
2. **Price hits $97,000 (TP1)** — Bot closes 10% of position for profit. SL moves to $95,000 (your entry). Now even if price drops all the way back, you break even on the remaining position
3. **Price hits $99,000 (TP2)** — Bot closes 35% of position. SL stays at $95,000 (entry). Position keeps running
4. **Price hits $102,000 (TP3)** — Bot closes the remaining 55%. Trade is fully closed with profit at all 3 levels

**What if price reverses after TP1?** Your SL is at entry ($95,000). You already took 10% profit from TP1, and the rest closes at breakeven. You still come out ahead.

**What if price reverses after TP2?** Same idea — you've already banked profit from TP1 and TP2 (45% of position closed at profit). The remaining 55% closes at breakeven. Still a winning trade overall.

---

## Signal Classification

The bot sorts every signal into one of two categories:

| Type | How it's detected | What it means |
|------|-------------------|---------------|
| **Swing** | Keywords like "swing", "long term", "daily", "hold", "weekly" in the signal | Longer hold, goes to Wallet 1 |
| **Scalp** | Keywords like "scalp", "intraday", "quick", "5m", "1h" in the signal | Quick trade, goes to Wallets 2-4 |

If there are no keywords, the bot looks at leverage:
- Under 10x = swing
- 10x and above = scalp

### Why it matters

- Swing trades go to **Wallet 1** only (one swing per symbol at a time)
- Scalp trades go to **Wallets 2-4** (up to 3 scalp trades per symbol at once)
- Different TP splits can be configured for each type

---

## Position Sizing

The bot sizes every trade the same way:

1. Adds up the total value of all wallets (free USDC + money already in trades + unrealized PnL)
2. Multiplies by your `PORTFOLIO_PCT` setting (default 25%)
3. That's your position size for the trade

**Example:** You have $1,000 total across all wallets. `PORTFOLIO_PCT=0.25`. Each trade uses $250 as collateral (before leverage).

Guardrails:
- **Max Position USD** — No single trade can be larger than this (default $10,000)
- **Min Position USD** — Trades smaller than this are skipped (default $20)
- **Max Leverage** — Signal leverage is capped at this value (default 100x)

---

## Wallet Management

The bot manages all 4 wallets automatically:

- **USDC rebalance** — Every hour (and after every trade), USDC is equalized across all wallets so no single wallet runs dry
- **Auto-fund** — If a wallet needs USDC for a trade, the bot pulls from other wallets automatically
- **Gas top-up** — If any wallet's ETH drops below $5, the bot swaps $5 USDC to ETH via Uniswap so you never run out of gas

You just fund Wallet 1 with USDC and ETH. The bot distributes everything else.

---

## Summary

| Aspect | How it works |
|--------|-------------|
| **Entry** | Automatic when signal arrives from Telegram channel |
| **TP levels** | Partial closes at each level (configurable split %) |
| **Stop loss** | Set on open, then trails up as TPs hit |
| **After TP1** | SL moves to breakeven — worst case you break even |
| **After TP3+** | SL trails 2 TP levels behind — profit is locked in |
| **Position size** | % of total portfolio (default 25%) |
| **Wallets** | Swing = W1, Scalp = W2-W4, auto-rebalanced |
| **Gas** | Auto-topped up via Uniswap when low |
