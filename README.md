# GMX V2 Telegram Trading Bot

Automated on-chain trading bot for [GMX V2](https://gmx.io) on Arbitrum. Listens to Telegram signal channels, parses trading signals, and executes leveraged perpetual trades with full TP/SL management — all on-chain.

## Features

### Core Trading
- **Signal Parsing** — Parses LONG/SHORT signals from Telegram channels with entry range, 2-5 take profit levels, stop loss, and leverage
- **On-Chain Execution** — MarketIncrease orders via GMX V2 Exchange Router with automatic TP (LimitDecrease) and SL (StopLossDecrease) placement
- **Smart TP Distributions** — When signals don't specify close percentages, applies optimized defaults (e.g., 2 TPs: 33%/67%, 3 TPs: 20%/50%/30%)
- **Progressive Trailing Stop Loss** — After TP1 hits, SL moves to entry (breakeven). After TP2, SL moves to TP1 price. Continues for all levels
- **Position Close Detection** — Monitors on-chain state to detect when positions close via SL, liquidation, or final TP

### Dual Wallet Architecture
- **Two Wallets (W1/W2)** — Run two wallets simultaneously for the same signal. W1 is preferred; W2 is used when W1 already has an open position for that symbol
- **On-Chain Routing** — Queries live on-chain positions (not just internal tracking) to determine wallet assignment
- **Combined Pool Sizing** — Both wallets act as one pool. Trade size = 25% of combined USDC balance across both wallets
- **Auto-Rebalance** — After each trade open/close, USDC is automatically transferred from the richer wallet to equalize balances

### Gas Management
- **ETH Top-Up** — If either wallet's ETH balance drops below $2, automatically swaps $5 USDC to ETH via Uniswap V3 on Arbitrum
- **Both Wallets Covered** — Gas check runs on both wallets after every trade

### Price Feeds
- **CoinGecko** — Primary price source with automatic retry on rate limits (429)
- **Chainlink On-Chain Fallback** — If CoinGecko fails, reads directly from Chainlink price feed contracts on Arbitrum (BTC, ETH, SOL, LINK)

### Admin Commands (via Telegram)
| Command | Description |
|---------|-------------|
| `/status` | Bot status, wallets, uptime, trade stats |
| `/balance` | Per-wallet USDC/deployed breakdown + combined totals |
| `/positions` | All on-chain positions with TP/SL orders, PnL, wallet labels |
| `/close` | Interactive close flow — select positions to close |
| `/close all` | Close all open positions across both wallets |
| `/close BTC` | Close all BTC positions |
| `/winrate` | Win rate stats from trade history |
| `/pnl` | PnL breakdown (today / 30d / all-time) |
| `/health` | System health metrics |
| `/help` | Show all commands |

### Safety
- Configurable max leverage, max position size, min position size
- Signal validation (SL/TP sanity, price deviation from current, direction check)
- Require TP and SL on every trade (configurable)
- DRY_RUN mode for testing without real execution
- Halting mechanism for emergency trading pause

## Supported Markets

| Symbol | GMX V2 Market |
|--------|--------------|
| BTC | `0x47c031236e19d024b42f8ae6780e44a573170703` |
| ETH | `0x70d95587d40A2caf56bd97485aB3Eec10Bee6336` |
| SOL | `0x09400D9DB990D5ed3f35D7be61DfAEB900Af03C9` |
| LINK | `0x7f1fa204bb7e853D36994DA19F830b6Ad18455C` |

## Project Structure

```
gmx.py    — Main bot engine (Telegram handlers, signal processing, wallet routing,
             TP/SL monitoring, admin commands, rebalance, ETH top-up)
open.py   — Signal execution (parse signals, build GMX V2 orders, place TP/SL,
             cancel orders, CoinGecko + Chainlink price feeds)
close.py  — Position management (fetch on-chain positions, create close orders,
             position data structures)
```

## Setup

### 1. Clone & Install

```bash
git clone https://github.com/sheldonbarnesjr/gmxbotv3.git
cd gmxbotv3

python3 -m venv .venv
source .venv/bin/activate
pip install python-dotenv telethon web3 eth-account
```

### 2. Configure

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Required environment variables:

```env
# Telegram (get from https://my.telegram.org)
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_SESSION=tgsession
TELEGRAM_CHANNELS=channel_name
NOTIFY_CHAT=@your_username
ADMIN_CHAT=ME
ADMIN_USERNAMES=@your_username

# GMX V2 Contracts (Arbitrum)
GMX_V2_EXCHANGE_ROUTER=0x1C3fa76e6E1088bCE750f23a5BFcffa1efEF6A41
GMX_V2_ORDER_VAULT=0x31eF83a530Fde1B38EE9A18093A333D8Bbbc40D5
GMX_V2_MARKET=0x47c031236e19d024b42f8ae6780e44a573170703
GMX_V2_COLLATERAL_TOKEN=0xaf88d065e77c8cC2239327C5EDb3A432268e5831
GMX_V2_EXECUTION_FEE_WEI=200000000000000

# Wallets (use test wallets first!)
PRIVATE_KEY=your_wallet_1_private_key
PRIVATE_KEY_2=your_wallet_2_private_key
NETWORK=arbitrum
RPC_URL=https://arb1.arbitrum.io/rpc

# Trading
MAX_LEVERAGE=100
MAX_POSITION_USD=100
MIN_POSITION_USD=20
PORTFOLIO_PCT=0.25
SLIPPAGE_BPS=30

# Safety (start with DRY_RUN=true!)
DRY_RUN=true
REQUIRE_SL=true
REQUIRE_TP=true
```

### 3. Run

```bash
# Dry run first
# Set DRY_RUN=true in .env
python3 gmx.py

# Live trading (after testing)
# Set DRY_RUN=false in .env
python3 gmx.py
```

## How It Works

### Signal Flow

```
Telegram Channel → Parse Signal → Validate → Pick Wallet → Execute On-Chain
                                                              ├── MarketIncrease (open)
                                                              ├── LimitDecrease × N (TPs)
                                                              └── StopLossDecrease (SL)
```

### Wallet Routing

```
New BTC LONG signal arrives:
  1. Check W1 on-chain — has BTC position? → Skip W1
  2. Check W2 on-chain — has BTC position? → Skip W2
  3. If both have BTC → Reject signal
  4. First free wallet gets the trade
```

### TP Hit Detection & Trailing SL

```
Every 30s:
  1. Count TP orders on-chain for each position
  2. If count decreased → TP was executed by GMX keepers
  3. Verify with price check (avoid false positives)
  4. Move SL:
     - TP1 hit → SL to Entry (breakeven)
     - TP2 hit → SL to TP1 price
     - TP3 hit → SL to TP2 price
```

### Auto-Rebalance

```
After every trade open/close:
  1. Check USDC balance on both wallets
  2. If difference > $1:
     - Calculate half the difference
     - Transfer USDC from richer → poorer wallet
  3. Both wallets stay roughly equal
```

## Requirements

- Python 3.10+
- Arbitrum RPC endpoint
- Two funded wallets on Arbitrum (USDC + ETH for gas)
- Telegram API credentials
- Telegram signal channel to monitor

## Disclaimer

This bot executes real on-chain trades with real funds. Use at your own risk. Always start with `DRY_RUN=true` and test thoroughly before going live. The authors are not responsible for any financial losses.
