# GMX V2 Telegram Trading Bot

Automated on-chain trading bot for [GMX V2](https://gmx.io) on Arbitrum. Listens to Telegram signal channels, parses trading signals, and executes leveraged perpetual trades with full TP/SL management — all on-chain.

## Features

### Core Trading
- **Signal Parsing** — Parses LONG/SHORT signals from Telegram channels with entry range, 2-8 take profit levels, stop loss, and leverage
- **On-Chain Execution** — MarketIncrease orders via GMX V2 Exchange Router with automatic TP (LimitDecrease) and SL (StopLossDecrease) placement
- **Configurable TP Splits** — Per-TP-count distributions in `.env` (e.g., 3 TPs: 10%/35%/55%). Separate scalp and swing split profiles
- **Swing vs Scalp Classification** — Signals are classified by keyword matching first (swing, long term, daily, scalp, short, intraday, etc.). No keywords? Leverage decides: under 10x = swing, 10x+ = scalp
- **Signal Deduplication** — MD5-based dedup with configurable time window (default 5 min) prevents double-opens from repeated messages
- **Failed Order Retry Queue** — TP/SL orders that fail on-chain are queued for automatic retry (up to 5 attempts with backoff)
- **Position Close Detection** — Monitors on-chain state to detect when positions close via SL, liquidation, or final TP. Classifies exit reason automatically
- **Update Message Filtering** — Ignores TP hit announcements, SL moved notifications, PnL updates, and other non-signal messages from channels

### Progressive Trailing Stop Loss
After take profits are hit, the SL is moved to lock in gains while letting the position run:

| TP Hit | SL Moves To | Purpose |
|--------|-------------|---------|
| TP1 | Entry price | Breakeven protection |
| TP2 | No move (stays at Entry) | Let the position run |
| TP3 | TP1 price | Lock in TP1 profit |
| TP4+ | TP2 price | Lock in TP2 profit |

SL moves are pre-validated via gas estimation before executing, avoiding stale order key failures.

### Multi-Wallet Architecture (Up to 4 Wallets)
- **W1 = Swing Trades** — Long-term / swing keyword signals route exclusively to Wallet 1
- **W2-W4 = Scalp Trades** — Scalp signals route to the first available scalp wallet without an open position for that symbol
- **On-Chain Routing** — Queries live on-chain positions (not just internal tracking) to determine wallet assignment
- **Combined Pool Sizing** — All wallets act as one pool. Trade size = `PORTFOLIO_PCT` of total portfolio value (free USDC + deployed collateral + unrealized PnL)
- **Auto-Rebalance** — After each trade open/close, USDC is equalized across all wallets using above/below-average transfer pairing
- **Consolidate** — `/consolidate` moves all free USDC from W2-W4 into W1 for easy withdrawals
- **Withdraw** — `/withdraw <amount>` sends USDC to an external Arbitrum address with consolidation (only if needed), address validation, and confirmation flow

### Gas Management
- **Auto ETH Top-Up** — If any wallet's ETH balance drops below $2, automatically swaps $5 USDC to ETH via Uniswap V3 on Arbitrum
- **All Wallets Covered** — Gas check runs on all wallets (W1-W4) after every trade
- **Manual Top-Up** — `/topup` command for manual gas refills with custom amounts

### Price Feeds
- **GMX Reader (Primary)** — Reads prices directly from GMX V2 Reader contract for open positions (same prices GMX uses for SL/TP execution)
- **Chainlink On-Chain Fallback** — Reads from Chainlink price feed contracts on Arbitrum (BTC, ETH, SOL, LINK)
- **Staleness Protection** — Prices older than 15s are considered stale; bot auto-halts if prices are stale for 120s+

### Analytics & Reporting
- **On-Chain Trade History** — Fetches realized PnL directly from GMX V2 EventEmitter logs (OrderExecuted + PositionDecrease events). Works for TPs, SLs, and manual closes — including those that happened while the bot was offline
- **Local Trade Storage** — On-chain trades are persisted to `onchain_trades.json` with `tx_hash:log_index` composite keys, so data survives beyond the 30-day RPC lookback window
- **PnL Reset** — `pnl_reset.json` stores a timestamp to permanently exclude old test/dust trades from all analytics
- **Dust Filtering** — Trades with PnL under $1 are excluded from `/pnl`, `/winrate`, `/pdf`, and hourly alerts
- **Trade History** — All closed trades recorded to `trade_history.json` with entry/exit, PnL, wallet, reason
- **PDF Export** — `/pdf` generates a PDF of all on-chain + local trades with color-coded PnL, sent as a Telegram file
- **Win Rate** — `/winrate` shows stats from on-chain data, filterable by symbol and last N trades
- **PnL Breakdown** — `/pnl` shows today / 30-day / all-time PnL for BTC, ETH, SOL from on-chain data with realized + unrealized
- **Hourly PnL Alerts** — Automated hourly PnL snapshot sent between 9 AM - 11 PM ET
- **24h Balance Tracking** — `/balance` shows portfolio change over the last 24 hours (hourly snapshots saved to `balance_snapshots.json`)

### Admin Commands (via Telegram)
| Command | Description |
|---------|-------------|
| `/addorder` | Manually add a SL or TP to an open position |
| `/balance` | Per-wallet USDC/deployed breakdown + 24h change |
| `/balance-wallets` | Manually rebalance USDC between wallets (W1-W4) |
| `/cancel` | Cancel a pending withdraw or close |
| `/cancelorder` | List & cancel individual SL/TP orders by number |
| `/close` | Interactive close flow — select positions to close |
| `/close all` | Close all positions + cancel all orders across all wallets |
| `/close BTC` | Close all BTC positions |
| `/confirm` | Confirm pending close or withdraw |
| `/consolidate` | Move all free USDC from W2-W4 into W1 (for withdrawals) |
| `/gas` | ETH gas balances for all wallets with low-balance warnings |
| `/halt [reason]` | Halt trading |
| `/health` | System health metrics |
| `/help` | Show all commands |
| `/increase` | Add collateral to an open position |
| `/lastmsg` | Print last message from monitored channel(s) |
| `/lastsignal` | Re-run the last parsed signal |
| `/pdf` | Download on-chain + local trade history as PDF |
| `/pnl` | PnL summary (today / 30d / all time) from on-chain data |
| `/positions` | Show on-chain positions with TP/SL orders and PnL |
| `/prices` | Live GMX & Chainlink prices for all tracked assets |
| `/reset` | Clear all trade history & PnL stats |
| `/resume [reason]` | Resume trading |
| `/retryqueue` | Show pending failed order retries |
| `/sl` | Move SL to entry or TP level (e.g., `/sl 1 entry`, `/sl 1 tp2`) |
| `/status` | Bot status, wallets, uptime, trade stats |
| `/sync` | Force re-sync positions from on-chain |
| `/summary` | Send daily summary now |
| `/topup` | Manual ETH top-up — per wallet or all (e.g., `/topup 3 10`) |
| `/tradesize` | View or change trade size % (e.g., `/tradesize 20` for 20%) |
| `/withdraw <amount>` | Withdraw USDC to an external Arbitrum address |
| `/winrate [SYMBOL] [N]` | Win rate stats (on-chain data) |

### Safety
- Configurable max leverage, max position size, min position size
- Signal validation (SL/TP sanity, price deviation from current, direction check)
- Require TP and SL on every trade (configurable)
- DRY_RUN mode for testing without real execution
- Halting mechanism for emergency trading pause with auto-resume
- SL order pre-validation via gas estimate before cancel attempts (avoids stale key failures)
- Duplicate position blocking — checks all wallets on-chain before opening
- **Safe startup** — Positions sync with `skip_sl_check=True` on restart to prevent unwanted SL moves. TP hit count prefers persisted state over inference
- **Withdraw safety** — Address validation (checksum, not zero, not bot wallet), 2-minute expiry, confirmation required

## Supported Markets

| Symbol | GMX V2 Market |
|--------|--------------|
| BTC | `0x47c031236e19d024b42f8ae6780e44a573170703` |
| ETH | `0x70d95587d40A2caf56bd97485aB3Eec10Bee6336` |
| SOL | `0x09400D9DB990D5ed3f35D7be61DfAEB900Af03C9` |
| LINK | `0x7f1fa204bb7e853D36994DA19F830b6Ad18455C` |

## Project Structure

```
gmx.py           — Main bot engine: GMXBot class, position tracking, signal processing,
                   TP hit detection, trailing SL, on-chain sync, startup/shutdown
telegram.py      — Telegram integration: command routing, event handlers, hourly PnL loop,
                   signal channel monitoring, close/withdraw flows
open.py          — Signal execution: parse signals, classify swing/scalp, build GMX V2
                   orders, place TP/SL, cancel orders, Chainlink price feeds
close.py         — Position management: fetch on-chain positions via Reader contract,
                   create close orders, GMXPosition data structure
history.py       — On-chain trade history: fetch realized PnL from GMX EventEmitter logs
                   (OrderExecuted + PositionDecrease events), wallet-filtered, deduped
risk.py          — Risk management: signal validation, SL/TP direction checks,
                   trailing SL strategy (determine_new_sl_target), exit classification
sl_tp.py         — SL/TP mixin: move_sl, cmd_sl, cmd_addorder, cmd_cancelorder
wallet_mgmt.py   — Multi-wallet mixin: balance, gas, rebalance, consolidate, topup,
                   withdraw, balance snapshots for 24h tracking
analytics.py     — Analytics mixin: trade recording, winrate, PnL, PDF export, health,
                   on-chain trade fetching & local storage
notifications.py — Notification mixin: Telegram message sending, position open alerts,
                   startup notification
price_feeds.py   — Price feeds mixin: GMX Reader + Chainlink prices, cmd_prices
config.py        — Configuration loading from .env with defaults
test.py          — On-chain E2E test (BTC SHORT open/close/TP cycle)
test_trailing_sl.py — Unit tests for trailing SL strategy (30 tests)

Data files (auto-generated):
onchain_trades.json   — Persistent local copy of on-chain trades (tx_hash:log_index keyed)
pnl_reset.json        — PnL reset timestamp (filters out trades before this time)
trade_history.json    — Bot-recorded trade history (entry/exit, PnL, wallet, reason)
position_state.json   — Persisted position state (TP hits, realized PnL, SL labels)
balance_snapshots.json — Hourly balance snapshots for 24h tracking
```

## Setup

### 1. Clone & Install

```bash
git clone https://github.com/sheldonbarnesjr/gmxbotv3.git
cd gmxbotv3

python3 -m venv .venv
source .venv/bin/activate
pip install python-dotenv telethon web3 eth-account fpdf2
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

# Wallets — up to 4 (W1=swing, W2-W4=scalp)
PRIVATE_KEY=your_wallet_1_private_key
PRIVATE_KEY_2=your_wallet_2_private_key
PRIVATE_KEY_3=your_wallet_3_private_key
PRIVATE_KEY_4=your_wallet_4_private_key
NETWORK=arbitrum
RPC_URL=https://arb1.arbitrum.io/rpc

# Trading
MAX_LEVERAGE=100
MAX_POSITION_USD=100
MIN_POSITION_USD=20
PORTFOLIO_PCT=0.20
SLIPPAGE_BPS=30

# TP Splits — scalp (per TP count, values are %, must sum to 100)
TP_2_1=20
TP_2_2=80
TP_3_1=10
TP_3_2=35
TP_3_3=55

# TP Splits — swing (only applied when signal has explicit swing keyword)
SWING_TP_2_1=40
SWING_TP_2_2=60
SWING_TP_3_1=30
SWING_TP_3_2=40
SWING_TP_3_3=30

# Signal Classification
SWING_KEYWORDS=swing,long term,long-term,hold,htf,weekly,daily,position trade,macro
SCALP_KEYWORDS=scalp,intraday,day trade,quick,ltf,15m,5m,1h,sniper,short term

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
Telegram Channel → Parse Signal → Dedup Check → Classify (swing/scalp) → Validate
                                                                            |
                                                    Pick Wallet (W1 swing / W2-W4 scalp)
                                                                            |
                                                                  Execute On-Chain
                                                                    +-- MarketIncrease (open)
                                                                    +-- LimitDecrease x N (TPs)
                                                                    +-- StopLossDecrease (SL)
```

### Wallet Routing

```
New BTC LONG signal arrives (classified as scalp):
  1. Check W2 on-chain — has BTC position? -> Skip
  2. Check W3 on-chain — has BTC position? -> Skip
  3. Check W4 on-chain — has BTC position? -> Skip
  4. First free scalp wallet gets the trade
  5. If all busy -> Reject signal

Swing signal -> always routes to W1 only
```

### TP Hit Detection & Trailing SL

```
Every 30s:
  1. Count TP orders on-chain for each position
  2. If count decreased -> TP was executed by GMX keepers
  3. Verify with price check (avoid false positives)
  4. Move SL based on trailing strategy:
     - TP1 hit -> SL to Entry (breakeven)
     - TP2 hit -> No SL move (stay at Entry, let it run)
     - TP3 hit -> SL to TP1 price
     - TP4+ hit -> SL to TP2 price
```

### Hourly Sync & Monitoring

```
Every hour:
  1. Save balance snapshot (for /balance 24h tracking)
  2. Re-sync positions from on-chain (under signal lock)
  3. Send hourly PnL alert (9 AM - 11 PM ET)
```

### Auto-Rebalance

```
After every trade open/close (and hourly):
  1. Fetch USDC balance on all wallets
  2. Calculate average balance
  3. Wallets above average send excess to wallets below average
  4. Minimum $0.50 transfer threshold to avoid dust
  5. All wallets stay roughly equal
```

## Requirements

- Python 3.10+
- Arbitrum RPC endpoint
- 1-4 funded wallets on Arbitrum (USDC + ETH for gas)
- Telegram API credentials
- Telegram signal channel to monitor

## Disclaimer

This bot executes real on-chain trades with real funds. Use at your own risk. Always start with `DRY_RUN=true` and test thoroughly before going live. The authors are not responsible for any financial losses.
