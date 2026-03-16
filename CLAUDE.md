# CLAUDE.md — gmxbotv3

## What This Is

A **production trading bot** for GMX V2 (Arbitrum) + Bitunix Futures. It listens to Telegram signal channels, parses signals, and executes leveraged perp trades on-chain. A FastAPI REST API (`rest_api.py`) powers the **Multiply** iOS dashboard.

**Live server:** `http://187.77.200.149:8000`
**API key:** `gmx_Z8sRFT1Vp_rgIrn655LpAMTodIs3QTwH7bvtWgWVT0A`
**Wallet:** `0xe859A4b3623F2f61a1B2C429e0aBC55AfdAE863c`
**Exchange mode:** mirror (GMX + Bitunix in parallel)

---

## Rules

0. **Verify Xcode builds** — After ANY change to Swift files or `project.pbxproj`, run the Xcode build command below and fix all errors before considering the task done:
   ```bash
   xcodebuild -project /Users/sheldon-anariebarnes/Desktop/Folders/multiply/Multiply/Multiply.xcodeproj -scheme Multiply -configuration Debug -destination 'platform=iOS Simulator,name=iPhone 16,OS=latest' build 2>&1 | tail -30
   ```
   If the build fails, read the errors, fix them, and rebuild until it succeeds. A task is NOT complete until the build passes.
1. **Cross-project compatibility** — Changes must also work with the **multiply/** folder and the **mltply** app.
2. **Backend lives here** — Do NOT create or edit Python files inside `multiply/`. All backend code goes in this repo.
3. **Don't touch `commercial-trading-bot/`** — That's a separate distribution copy. Changes go to root files only.
4. **JSON files are crash-safe** — Always use `atomic_json_write()` / `safe_json_read()` from `state_io.py`. Never write JSON directly.
5. **API responses are snake_case** — The iOS app maps them to camelCase via Swift `CodingKeys`.
6. **Test API changes** — After editing `rest_api.py`, verify the endpoint works against the live server with the API key above.
7. **Don't break the signal pipeline** — The flow is: Telegram → parse → validate → deduplicate → route wallet → execute on-chain. Test changes against this full chain.

---

## Architecture

```
gmxbotv3/
├── gmx.py              — Main GMXBot class (mixin-based), signal pipeline, 9 async loops
├── rest_api.py          — FastAPI server (port 8000), all /api/v1/* endpoints
├── open.py              — Signal parsing, MarketIncrease, TP/SL order placement
├── close.py             — Position fetching (GMX Reader), MarketDecrease orders
├── config.py            — Loads .env into Config dataclass
├── state_io.py          — Atomic JSON persistence (crash-safe)
├── shared_cache.py      — Inter-process cache (rest_api ↔ gmx.py)
│
├── Mixins (composed into GMXBot):
│   ├── sl_tp.py         — TP hit detection, trailing SL logic
│   ├── wallet_mgmt.py   — Multi-wallet routing (W1=swing, W2-W4=scalp), rebalance, gas top-up
│   ├── analytics.py     — Trade recording, PnL, PDF export, win rate
│   ├── notifications.py — Telegram alerts
│   ├── price_feeds.py   — GMX Reader + Chainlink prices
│   ├── withdraw_mixin.py— USDC withdrawal flow
│   └── telegram.py      — Telegram command routing, 30+ admin commands
│
├── Bitunix:
│   ├── bitunix_api.py       — REST client (SHA256 double-signing)
│   ├── bitunix_executor.py  — Execute signals on Bitunix
│   ├── bitunix_monitor.py   — Position monitoring
│   └── bitunix_pairs.py     — Symbol mapping (BTC → BTCUSDT)
│
├── Utilities:
│   ├── risk.py              — Position sizing, validation, PnL calc
│   ├── history.py           — On-chain trade history from EventEmitter
│   ├── signal_store.py      — Persistent signal archive
│   ├── app_notifications.py — iOS notification queue
│   ├── signal_analyzer.py   — Signal performance metrics
│   ├── trade_rebuilder.py   — Trade data rebuild
│   └── bot_api.py           — Telegram Bot API helpers
│
├── json/                — Persistent state (position_state, trade_history, balance_snapshots, etc.)
├── .env                 — Secrets & config (Telegram, wallets, trading params, TP distributions)
├── commercial-trading-bot/ — Distribution copy for commercial users (do not edit)
└── Multiply/            — iOS app source (Swift/SwiftUI)
```

---

## REST API Endpoints (rest_api.py)

All endpoints require `Authorization: Bearer <api_key>`.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/health` | GET | Status, uptime, trade counts |
| `/api/v1/dashboard` | GET | Portfolio: balances, deployed capital, PnL, 24h change |
| `/api/v1/dashboard/reset` | POST | Reset chart history |
| `/api/v1/dashboard/chart` | GET | Balance/PnL chart data (60-90 day window) |
| `/api/v1/positions` | GET | All open positions (GMX + Bitunix) |
| `/api/v1/positions/{id}` | GET | Single position |
| `/api/v1/positions/{id}/close` | POST | Close position |
| `/api/v1/positions/{id}/strategy` | POST | Change TP distribution & SL rules |
| `/api/v1/trades` | GET | Trade history |
| `/api/v1/trades/stats` | GET | Win rate, PnL by symbol |
| `/api/v1/trades/pdf` | GET | PDF export |
| `/api/v1/wallet` | GET | Wallet balances (USDC + ETH per wallet) |
| `/api/v1/wallet/deposit-address` | GET | W1 deposit address |
| `/api/v1/wallet/withdraw` | POST | Withdraw USDC |
| `/api/v1/wallet/swap/quote` | POST | Uniswap quote |
| `/api/v1/wallet/swap/execute` | POST | Execute Uniswap swap |
| `/api/v1/prices` | GET | Current prices |
| `/api/v1/signals` | GET | Recent parsed signals |
| `/api/v1/notifications` | GET | App notifications |
| `/api/v1/config` | GET | Bot configuration |
| `/api/v1/config/update` | POST | Update trading params |
| `/api/v1/ws` | WS | Real-time updates (`?token=<key>`) |

---

## Key Concepts

**Signal flow:** Telegram channel → `open.py` parses → validates (risk.py) → deduplicates (MD5) → routes to wallet → executes MarketIncrease + TP orders + SL order

**Wallet routing:** W1 = swing trades, W2-W4 = scalp trades. Sizing uses combined pool: `PORTFOLIO_PCT × (free USDC + deployed + unrealized PnL)`

**TP/SL management:** TP orders placed as LimitDecrease on-chain. TP hits detected every ~5s via PositionDecrease events. Trailing SL moves up as TPs hit (TP1→breakeven, TP3→TP1 price, etc.)

**Exchange modes:** `gmx` (GMX only), `bitunix` (Bitunix only), `mirror` (both in parallel)

**Supported pairs:** BTC, ETH, SOL, LINK on GMX V2 Arbitrum

**Background loops (9):** price updates (10s), TP monitoring (5s), heartbeat (30s), order retry (backoff), rebalance (1h), gas check (1h), PnL alerts (60s), weekly summary (Sunday 10PM ET), bot API polling

---

## Persistent State (json/)

| File | What it stores |
|------|---------------|
| `position_state.json` | Open positions: TP/SL levels, verified TP hits, realized PnL |
| `trade_history.json` | Closed trades: entry/exit, PnL, wallet, exit reason |
| `onchain_trades.json` | Raw on-chain PositionDecrease events |
| `balance_snapshots.json` | Hourly portfolio snapshots (90-day rolling) |
| `signal_store.json` | Last 500 parsed signals |
| `api_keys.json` | API keys for iOS app |
| `user_config.json` | User overrides (TP distributions, trailing SL config) |
| `chart_config.json` | Chart reset timestamp |

---

## Multiply iOS App

**Xcode project:** `Multiply/Multiply.xcodeproj`
**App source:** `Multiply/GMXTradingBot/` → `Views/`, `ViewModels/`, `Models/`, `Services/`

### Adding New Swift Files to Xcode

When creating a `.swift` file, register it in `project.pbxproj` with 4 entries:
1. **PBXBuildFile** — `{isa = PBXBuildFile; fileRef = <REF_ID>; }`
2. **PBXFileReference** — `{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = <file>; sourceTree = "<group>"; }`
3. **PBXGroup children** — add ref ID to appropriate group
4. **PBXSourcesBuildPhase files** — add build file ID

Generate two unique 24-char hex IDs. Use the Edit tool for targeted insertions — NEVER rewrite the whole pbxproj. Validate with `plutil -lint` after editing.

### iOS ↔ Backend Patterns
- API: `snake_case` (Python) → `camelCase` (Swift) via `CodingKeys`
- WebSocket: `/api/v1/ws?token=<apiKey>`
- Dashboard auto-refreshes every 15s via `HomeViewModel.startAutoRefresh()`
- Positions: WebSocket primary + REST fallback (`fetchPositionsViaAPI()`)

---

## Deployment

**Bot:** `systemctl start tradingbot` (runs `gmx.py`)
**API:** `uvicorn rest_api:app --host 0.0.0.0 --port 8000` (or `python3 rest_api.py`)
**Both run on the VPS at `187.77.200.149`**

To generate a new API key: `python3 rest_api.py genkey`

---

## Tech Stack

Python 3.10+ · web3.py · telethon · FastAPI · uvicorn · fpdf2 · eth-account · python-dotenv · cryptography
Swift/SwiftUI (iOS app) · Xcode
