#!/usr/bin/env python3
"""
Production GMX V2 Telegram Trading Bot

Wired to real on-chain execution via open.py (MarketIncrease + TP/SL)
and close.py (MarketDecrease).

Features:
- Parses trading signals from Telegram channels (LONG & SHORT)
- Opens positions with on-chain TP (LimitDecrease) and SL (StopLossDecrease)
- Interactive /close via Telegram admin chat (no terminal prompts)
- Real CoinGecko price feeds
- Risk assessment, validation, analytics
- Heartbeat & health monitoring
"""

import os
import re
import time
import uuid
import asyncio
import logging
import statistics
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient, events
from web3 import Web3
from eth_account import Account

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IMPORT REAL EXECUTION FROM open.py AND close.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# "open" shadows the Python builtin — alias it so the IDE can resolve symbols
import open as _open_mod  # type: ignore[assignment]  # noqa: A004

# From open.py — signal parsing & execution
from open import (  # type: ignore[assignment]  # noqa: A004
    parse_signal,
    Signal,
    TakeProfit,
    execute_signal,
    create_market_increase_order,
    create_limit_increase_order,
    create_sl_order,
    create_tp_order,
    fetch_current_price,
    cancel_orders_for_market,
    cancel_all_orders,
    fetch_open_orders,
    scale_price,
    fetch_execution_price,
    classify_signal,
    COINGECKO_IDS,
    INDEX_TOKEN_DECIMALS,
    ERC20_ABI,
    EXCHANGE_ROUTER_ABI,
    ORDER_TYPE_STOP_LOSS_DECREASE,
    ORDER_TYPE_LIMIT_DECREASE,
)

# From close.py — position fetching & closing
from close import (
    fetch_positions as chain_fetch_positions,
    fetch_current_price as close_fetch_current_price,
    GMXPosition,
    create_close_order,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Telegram
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "gmx_advanced_session")
TELEGRAM_CHANNELS = [c.strip() for c in os.getenv("TELEGRAM_CHANNELS", "").split(",") if c.strip()]
NOTIFY_CHAT = os.getenv("NOTIFY_CHAT", "")
ADMIN_CHAT = os.getenv("ADMIN_CHAT", "")
ADMIN_USERNAMES = [u.strip().lstrip("@").lower() for u in os.getenv("ADMIN_USERNAMES", "").split(",") if u.strip()]

# Network & Web3
NETWORK = os.getenv("NETWORK", "arbitrum").lower()
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
PRIVATE_KEY_2 = os.getenv("PRIVATE_KEY_2", "")
PRIVATE_KEY_3 = os.getenv("PRIVATE_KEY_3", "")
PRIVATE_KEY_4 = os.getenv("PRIVATE_KEY_4", "")
RPC_URL = os.getenv("ARBITRUM_RPC_URL") or os.getenv("RPC_URL", "https://arb1.arbitrum.io/rpc")

# GMX V2 addresses
GMX_V2_EXCHANGE_ROUTER = os.getenv("GMX_V2_EXCHANGE_ROUTER", "").strip()
GMX_V2_ORDER_VAULT = os.getenv("GMX_V2_ORDER_VAULT", "").strip()
GMX_V2_MARKET = os.getenv("GMX_V2_MARKET", "").strip()  # default / BTC market
GMX_V2_COLLATERAL_TOKEN = os.getenv("GMX_V2_COLLATERAL_TOKEN", "").strip()

# Per-symbol GMX V2 market addresses on Arbitrum
# Each key can be overridden via env var GMX_V2_MARKET_BTC / _ETH / _SOL / etc.
GMX_V2_MARKETS = {
    "BTC":  os.getenv("GMX_V2_MARKET_BTC",  os.getenv("GMX_V2_MARKET", "0x47c031236e19d024b42f8ae6780e44a573170703")).strip(),
    "ETH":  os.getenv("GMX_V2_MARKET_ETH",  "0x70d95587d40A2caf56bd97485aB3Eec10Bee6336").strip(),
    "SOL":  os.getenv("GMX_V2_MARKET_SOL",  "0x09400D9DB990D5ed3f35D7be61DfAEB900Af03C9").strip(),
    "LINK": os.getenv("GMX_V2_MARKET_LINK", "0x7f1fa204bb700853D36994DA19F830b6Ad18455C").strip(),
}

# Allowed trading pairs
ALLOWED_SYMBOLS = {"BTC", "ETH", "SOL", "LINK"}

# Chainlink price feeds on Arbitrum (fallback when GMX Reader prices unavailable)
CHAINLINK_FEEDS = {
    "BTC":  "0x6ce185860a4963106506C203335A2910413708e9",
    "ETH":  "0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612",
    "SOL":  "0x24ceA4b8ce57cdA5058b924B9B9987992450590c",
    "LINK": "0x86E53CF1B870786351Da77A57575e79CB55812CB",
}
CHAINLINK_ABI = [
    {"name": "latestRoundData", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [{"name": "roundId", "type": "uint80"},
                 {"name": "answer", "type": "int256"},
                 {"name": "startedAt", "type": "uint256"},
                 {"name": "updatedAt", "type": "uint256"},
                 {"name": "answeredInRound", "type": "uint80"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
]

# Trading
MAX_LEVERAGE = float(os.getenv("MAX_LEVERAGE", "10"))
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "50000"))
MIN_POSITION_USD = float(os.getenv("MIN_POSITION_USD", "2"))
PORTFOLIO_PCT = float(os.getenv("PORTFOLIO_PCT", "0.20"))  # % of portfolio per trade (adjustable via /tradesize)
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "30"))
EXECUTION_FEE_WEI = int(os.getenv("GMX_V2_EXECUTION_FEE_WEI", str(Web3.to_wei(0.0002, "ether"))))

MAX_PRICE_DEVIATION = float(os.getenv("MAX_PRICE_DEVIATION", "0.05"))

# Safety
REQUIRE_SL = os.getenv("REQUIRE_SL", "true").lower() == "true"
REQUIRE_TP = os.getenv("REQUIRE_TP", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Price monitoring
PRICE_MAX_AGE_S = int(os.getenv("PRICE_MAX_AGE_S", "15"))  # tighter cache: 15s max staleness
PRICE_UPDATE_INTERVAL = float(os.getenv("PRICE_UPDATE_INTERVAL", "10.0"))

# Heartbeat
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "30"))
HALT_ON_PRICE_STALE = int(os.getenv("HALT_ON_PRICE_STALE", "120"))
AUTO_RESUME_AFTER = int(os.getenv("AUTO_RESUME_AFTER", "300"))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DATA STRUCTURES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class TakeProfitLevel:
    price: float
    percentage: float
    executed: bool = False
    executed_at: Optional[float] = None

@dataclass
class Position:
    id: str
    symbol: str
    side: str
    size_usd: float
    leverage: float
    entry_price: float
    current_price: float = 0.0

    stop_loss: Optional[float] = None
    take_profits: List[TakeProfitLevel] = field(default_factory=list)

    is_open: bool = True
    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0

    tx_hash: Optional[str] = None
    tp_tx_hashes: List[str] = field(default_factory=list)
    sl_tx_hash: Optional[str] = None
    exit_reason: Optional[str] = None

    # Which wallet this position belongs to (1=swing, 2-4=scalp)
    wallet_id: int = 1

    # On-chain tracking for TP-hit → move SL
    market_addr: Optional[str] = None
    sl_moved_to_entry: bool = False
    sl_move_label: Optional[str] = None  # e.g. "Entry", "TP1", "TP2" — where the SL was moved to
    sl_move_failed: bool = False  # True if a move_sl attempt failed — SL may be at wrong price
    tp_hits_count: int = 0  # how many TPs have been hit so far
    last_known_tp_count: int = 0  # track how many TP orders remain on-chain
    pending_fill: bool = False  # True if placed as limit order and not yet filled on-chain
    pending_fill_since: Optional[float] = None  # timestamp when limit order was placed
    current_sl_key: Optional[str] = None  # hex key of the SL order we placed (to avoid cancelling it accidentally)

    @property
    def short_id(self) -> str:
        return self.id[-6:]

    @property
    def duration_hours(self) -> float:
        end_time = self.closed_at or time.time()
        return (end_time - self.opened_at) / 3600

    @property
    def collateral_usd(self) -> float:
        """Collateral = size / leverage."""
        return self.size_usd / self.leverage if self.leverage else 0.0

    @property
    def pnl_percentage(self) -> float:
        """PnL as percentage of collateral (what was actually deposited)."""
        col = self.collateral_usd
        if col == 0:
            return 0.0
        return (self.unrealized_pnl / col) * 100

@dataclass
class PriceData:
    symbol: str
    price: float
    timestamp: float = field(default_factory=time.time)

    @property
    def is_fresh(self) -> bool:
        return time.time() - self.timestamp < PRICE_MAX_AGE_S

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

@dataclass
class TradeRecord:
    id: str
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    size_usd: float
    leverage: float
    duration_hours: float
    pnl_usd: float
    pnl_percentage: float
    exit_reason: str
    opened_at: float
    closed_at: float

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BOT ENGINE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GMXBot:
    """Production GMX V2 Trading Bot with real on-chain execution."""

    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self.w3: Optional[Web3] = None
        self.account: Optional[Account] = None
        self.account2: Optional[Account] = None
        self.account3: Optional[Account] = None
        self.account4: Optional[Account] = None

        # State
        self.positions: Dict[str, Position] = {}
        self.price_cache: Dict[str, PriceData] = {}
        self.trade_history: List[TradeRecord] = []

        # Close confirmation state: chat_id -> pending close info
        self.pending_closes: Dict[int, Dict[str, Any]] = {}

        # Increase position state: chat_id -> pending increase info
        self.pending_increase: Dict[int, Dict[str, Any]] = {}

        # System
        self.is_halted = False
        self.halt_reason = ""
        self.halt_time: Optional[float] = None
        self.last_heartbeat = time.time()
        self.health_stats = {
            "uptime_start": time.time(),
            "price_updates": 0,
            "signals_processed": 0,
            "trades_executed": 0,
            "errors": 0,
        }

        self.price_update_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.tp_monitor_task: Optional[asyncio.Task] = None
        self.daily_summary_task: Optional[asyncio.Task] = None
        self.rebalance_task: Optional[asyncio.Task] = None
        self.resolved_channels: Dict[int, str] = {}  # channel_id -> channel_name
        self.setup_logging()

    def setup_logging(self):
        logging.basicConfig(
            level=getattr(logging, LOG_LEVEL, logging.INFO),
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler("gmx_bot.log"),
            ],
        )
        self.logger = logging.getLogger("GMXBot")

    # ──────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────
    async def start(self):
        self.logger.info("Starting GMX V2 Trading Bot")
        await self.init_telegram()
        self.init_web3()

        # Sync on-chain positions into internal tracking (survives reboots)
        await self._sync_on_chain_positions()

        self.price_update_task = asyncio.create_task(self.price_update_loop())
        self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())
        self.tp_monitor_task = asyncio.create_task(self.tp_monitor_loop())
        self.daily_summary_task = asyncio.create_task(self.daily_summary_loop())
        self.rebalance_task = asyncio.create_task(self.rebalance_loop())

        self.logger.info("GMX Bot started successfully")

        # Send startup notification to admin
        await self.send_startup_notification()

        shutdown_reason = "Telegram disconnected"
        try:
            await self.client.run_until_disconnected()
        except (KeyboardInterrupt, asyncio.CancelledError):
            self.logger.info("Bot stopped by user")
            shutdown_reason = "Manual stop"
        except Exception as e:
            self.logger.error(f"Fatal error in bot: {e}")
            shutdown_reason = f"Fatal error: {e}"
        finally:
            await self.shutdown(shutdown_reason)

    async def shutdown(self, reason: str = "Bot stopped"):
        self.logger.info("Shutting down GMX Bot...")

        # Cancel background tasks first
        if self.price_update_task:
            self.price_update_task.cancel()
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
        if self.tp_monitor_task:
            self.tp_monitor_task.cancel()
        if self.daily_summary_task:
            self.daily_summary_task.cancel()
        if self.rebalance_task:
            self.rebalance_task.cancel()

        # Reconnect if needed so the offline message can be sent
        try:
            if self.client:
                if not self.client.is_connected():
                    await self.client.connect()
                await self.client.send_message(NOTIFY_CHAT, "🔴 Bot Offline")
                await self.client.disconnect()
        except Exception as e:
            self.logger.error(f"Failed to send offline notification: {e}")

        self.logger.info("Bot shutdown complete")

    async def init_telegram(self):
        if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
            raise ValueError("Missing Telegram API credentials")

        self.client = TelegramClient(TELEGRAM_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await self.client.start()

        # Pre-resolve channel entities so Telethon caches them before
        # registering event handlers (avoids "Cannot find any entity" error).
        resolved_channels = []
        for ch in TELEGRAM_CHANNELS:
            try:
                # Try as integer ID first (e.g. "-1001363986630")
                try:
                    ch_id = int(ch)
                except ValueError:
                    ch_id = ch
                entity = await self.client.get_entity(ch_id)
                name = getattr(entity, "title", None) or getattr(entity, "username", str(ch_id))
                resolved_channels.append(ch_id)
                self.resolved_channels[ch_id] = name
                self.logger.info(f"Connected to {name} ({ch}) successfully")
            except Exception as e:
                self.logger.error(f"Failed to resolve channel {ch}: {e}")

        if not resolved_channels:
            raise ValueError("Could not resolve any Telegram channels — check TELEGRAM_CHANNELS in .env")

        # Signal handler — messages from monitored channels
        @self.client.on(events.NewMessage(chats=resolved_channels))
        async def handle_signal(event):
            await self.process_signal(event.message.text)

        # Admin commands — messages starting with /
        @self.client.on(events.NewMessage(chats=[ADMIN_CHAT], pattern=r"^/"))
        async def handle_admin(event):
            # Check sender is an admin
            sender = await event.get_sender()
            username = (getattr(sender, "username", "") or "").lower()
            if ADMIN_USERNAMES and username not in ADMIN_USERNAMES:
                return
            await self.process_admin_command(event.message.text, event.chat_id)

        # Confirmation handler — non-command messages from admin for /close flow
        @self.client.on(events.NewMessage(chats=[ADMIN_CHAT]))
        async def handle_confirm(event):
            text = event.message.text.strip()
            if text.startswith("/"):
                return  # handled by admin handler
            sender = await event.get_sender()
            username = (getattr(sender, "username", "") or "").lower()
            if ADMIN_USERNAMES and username not in ADMIN_USERNAMES:
                return
            # Route to pending increase handler if active, otherwise close handler
            if event.chat_id in self.pending_increase:
                await self.handle_increase_reply(event.chat_id, text)
            else:
                await self.handle_close_confirmation(event.chat_id, text)

        self.logger.info(f"Telegram initialized, monitoring {len(resolved_channels)} channel(s)")

    def init_web3(self):
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        if PRIVATE_KEY:
            self.account = Account.from_key(PRIVATE_KEY)
            self.logger.info(f"Web3 on {NETWORK}, wallet 1 (swing): {self.account.address[:10]}...")
        else:
            self.logger.warning("No private key — read-only mode")
        if PRIVATE_KEY_2:
            self.account2 = Account.from_key(PRIVATE_KEY_2)
            self.logger.info(f"Wallet 2 (scalp): {self.account2.address[:10]}...")
        else:
            self.logger.info("No PRIVATE_KEY_2 — single wallet mode")
        if PRIVATE_KEY_3:
            self.account3 = Account.from_key(PRIVATE_KEY_3)
            self.logger.info(f"Wallet 3 (scalp): {self.account3.address[:10]}...")
        if PRIVATE_KEY_4:
            self.account4 = Account.from_key(PRIVATE_KEY_4)
            self.logger.info(f"Wallet 4 (scalp): {self.account4.address[:10]}...")

    async def _sync_on_chain_positions(self):
        """Scan on-chain positions for all wallets and add any that aren't
        already tracked internally. This makes the bot aware of positions
        opened manually or before a reboot."""
        MARKET_TO_SYMBOL = {v.lower(): k for k, v in GMX_V2_MARKETS.items()}

        wallets = self._all_wallets()

        synced = 0
        for wid, acct in wallets:
            try:
                chain_positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                chain_orders = await asyncio.to_thread(
                    fetch_open_orders, self.w3, acct.address
                )
            except Exception as e:
                self.logger.warning(f"Sync: failed to fetch wallet {wid}: {e}")
                continue

            for cp in chain_positions:
                market_lower = cp.market.lower()
                symbol = MARKET_TO_SYMBOL.get(market_lower, cp.symbol)
                side = "LONG" if cp.is_long else "SHORT"

                # Check if we already track this position
                already_tracked = any(
                    p.is_open
                    and p.market_addr
                    and p.market_addr.lower() == market_lower
                    and p.wallet_id == wid
                    and p.side == side
                    for p in self.positions.values()
                )
                if already_tracked:
                    continue

                # Count TP orders for this market on-chain
                tp_count = sum(
                    1 for o in chain_orders
                    if o["market"].lower() == market_lower
                    and o["order_type"] == ORDER_TYPE_LIMIT_DECREASE
                )

                # Build a Position from on-chain data
                pos = Position(
                    id=str(uuid.uuid4()),
                    symbol=symbol,
                    side=side,
                    size_usd=cp.size_usd,
                    leverage=cp.leverage,
                    entry_price=cp.entry_price,
                    current_price=cp.current_price,
                    unrealized_pnl=cp.unrealized_pnl,
                    market_addr=cp.market,
                    wallet_id=wid,
                    last_known_tp_count=0,  # will be set on first monitor poll
                )
                self.positions[pos.id] = pos
                synced += 1
                self.logger.info(
                    f"Synced on-chain position: {symbol} {side} "
                    f"${cp.size_usd:,.2f} @ {cp.leverage:.1f}x [W{wid}]"
                )

        if synced:
            self.logger.info(f"Synced {synced} on-chain position(s) into tracking")
        else:
            self.logger.info("No untracked on-chain positions found")

    def get_portfolio_value(self) -> float:
        """Get USDC collateral token balance in USD (portfolio value) for wallet 1."""
        return self._get_portfolio_value_for(self.account)

    def _get_portfolio_value_for(self, acct: Account) -> float:
        """Get USDC collateral token balance in USD for a specific wallet."""
        try:
            token = self.w3.eth.contract(
                address=Web3.to_checksum_address(GMX_V2_COLLATERAL_TOKEN),
                abi=ERC20_ABI,
            )
            decimals = token.functions.decimals().call()
            balance_raw = token.functions.balanceOf(acct.address).call()
            balance = balance_raw / (10 ** decimals)
            return balance
        except Exception as e:
            self.logger.error(f"Error getting portfolio value: {e}")
            return 0.0

    async def _get_combined_usdc(self) -> float:
        """Get combined FREE USDC balance across all wallets (not deployed).
        All wallets act as one pool for sizing trades."""
        total_usdc = 0.0
        all_wallets = [acct for _, acct in self._all_wallets()]
        for acct in all_wallets:
            try:
                usdc = await asyncio.to_thread(self._get_portfolio_value_for, acct)
                total_usdc += usdc
            except Exception:
                pass
        return total_usdc

    async def _get_total_portfolio_value(self) -> float:
        """Get total portfolio value: free USDC + deployed collateral + unrealized PnL.

        This gives the true account value including capital locked in open positions.
        Used for position sizing so trades are PORTFOLIO_PCT of TOTAL balance, not just free USDC.
        """
        # Free USDC across all wallets
        free_usdc = await self._get_combined_usdc()

        # Deployed collateral + PnL from on-chain positions
        deployed_value = 0.0
        all_wallets = [acct for _, acct in self._all_wallets()]
        for acct in all_wallets:
            try:
                positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                for pos in positions:
                    # Each position's value = collateral + unrealized PnL
                    deployed_value += pos.collateral_amount + pos.unrealized_pnl
            except Exception as e:
                self.logger.debug(f"Could not fetch positions for {acct.address[:10]}: {e}")

        total = free_usdc + deployed_value
        self.logger.debug(
            f"Portfolio: free=${free_usdc:.2f} + deployed=${deployed_value:.2f} = total=${total:.2f}"
        )
        return total

    async def _rebalance_wallets(self):
        """Equalize USDC across all configured wallets (up to 4).

        Calculates the average balance, then each wallet above average sends
        its excess to the wallets below average. Handles 2, 3, or 4 wallets
        in a single pass with multiple transfers if needed.

        Called after positions open/close and hourly to keep wallets balanced."""
        wallets = self._all_wallets()
        if len(wallets) < 2:
            return  # Single wallet mode — nothing to balance

        if DRY_RUN:
            self.logger.info("[DRY_RUN] Would rebalance wallets (skipped)")
            return

        try:
            # Fetch balances for all wallets
            balances = {}
            for wid, acct in wallets:
                bal = await asyncio.to_thread(self._get_portfolio_value_for, acct)
                balances[wid] = bal

            avg_bal = sum(balances.values()) / len(balances)

            # Find wallets above and below average
            above = {wid: bal - avg_bal for wid, bal in balances.items() if bal > avg_bal + 0.50}
            below = {wid: avg_bal - bal for wid, bal in balances.items() if bal < avg_bal - 0.50}

            if not above or not below:
                bal_str = ", ".join(f"W{wid}: ${bal:.2f}" for wid, bal in balances.items())
                self.logger.debug(f"Wallets balanced ({bal_str}, avg=${avg_bal:.2f})")
                return

            bal_str = ", ".join(f"W{wid}: ${bal:.2f}" for wid, bal in balances.items())
            self.logger.info(f"Rebalancing {len(wallets)} wallets (avg=${avg_bal:.2f}): {bal_str}")

            usdc_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(GMX_V2_COLLATERAL_TOKEN),
                abi=ERC20_ABI,
            )
            decimals = await asyncio.to_thread(lambda: usdc_contract.functions.decimals().call())

            # Build transfer pairs: from each above-avg wallet to below-avg wallets
            transfers = []
            above_list = sorted(above.items(), key=lambda x: x[1], reverse=True)
            below_list = sorted(below.items(), key=lambda x: x[1], reverse=True)

            ai = 0  # index into above_list
            bi = 0  # index into below_list
            above_remaining = {wid: excess for wid, excess in above_list}
            below_remaining = {wid: deficit for wid, deficit in below_list}

            while ai < len(above_list) and bi < len(below_list):
                sender_wid = above_list[ai][0]
                receiver_wid = below_list[bi][0]

                send_amount = min(above_remaining[sender_wid], below_remaining[receiver_wid])

                if send_amount >= 0.50:  # minimum $0.50 transfer to avoid dust
                    transfers.append((sender_wid, receiver_wid, send_amount))

                above_remaining[sender_wid] -= send_amount
                below_remaining[receiver_wid] -= send_amount

                if above_remaining[sender_wid] < 0.50:
                    ai += 1
                if below_remaining[receiver_wid] < 0.50:
                    bi += 1

            if not transfers:
                self.logger.debug("No transfers needed after computing pairs")
                return

            # Execute transfers sequentially (each is a separate on-chain tx)
            transfer_log = []
            for sender_wid, receiver_wid, amount in transfers:
                sender_acct = self._get_account(sender_wid)
                receiver_acct = self._get_account(receiver_wid)
                raw_amount = int(amount * (10 ** decimals))

                transfer_data = usdc_contract.encode_abi(
                    "transfer",
                    [Web3.to_checksum_address(receiver_acct.address), raw_amount],
                )

                try:
                    tx_hash = await asyncio.to_thread(
                        self._send_tx,
                        GMX_V2_COLLATERAL_TOKEN,
                        transfer_data,
                        0,
                        sender_acct,
                    )
                    receipt = await asyncio.to_thread(_open_mod.wait_receipt, self.w3, tx_hash)

                    if receipt.get("status") == 1:
                        transfer_log.append(f"W{sender_wid} → W{receiver_wid}: ${amount:.2f}")
                        self.logger.info(
                            f"Rebalance transfer OK: ${amount:.2f} W{sender_wid} → W{receiver_wid} (TX: {tx_hash})"
                        )
                    else:
                        self.logger.error(f"Rebalance tx reverted: W{sender_wid} → W{receiver_wid} (TX: {tx_hash})")
                        transfer_log.append(f"W{sender_wid} → W{receiver_wid}: FAILED")
                except Exception as e:
                    self.logger.error(f"Rebalance transfer error W{sender_wid} → W{receiver_wid}: {e}")
                    transfer_log.append(f"W{sender_wid} → W{receiver_wid}: ERROR")

            # Verify final balances
            new_bals = {}
            for wid, acct in wallets:
                new_bals[wid] = await asyncio.to_thread(self._get_portfolio_value_for, acct)
            new_str = " | ".join(f"W{wid}: ${bal:.2f}" for wid, bal in new_bals.items())

            self.logger.info(f"Rebalance complete: {new_str}")
            await self.notify(
                f"🔄 Wallets rebalanced ({len(transfers)} transfer(s))\n"
                + "\n".join(transfer_log) + "\n"
                + new_str
            )

        except Exception as e:
            self.logger.error(f"Wallet rebalance error: {e}")
            # Don't notify on rebalance failure — it's not critical

    async def rebalance_loop(self):
        """Check wallet balance every hour and auto-rebalance if needed."""
        while True:
            try:
                await asyncio.sleep(3600)  # 1 hour
                wallets = self._all_wallets()
                if len(wallets) < 2:
                    continue

                bals = {}
                for wid, acct in wallets:
                    bals[wid] = await asyncio.to_thread(self._get_portfolio_value_for, acct)

                max_bal = max(bals.values())
                min_bal = min(bals.values())
                diff = max_bal - min_bal

                bal_str = ", ".join(f"W{wid}: ${bal:.2f}" for wid, bal in bals.items())
                if diff > 1.0:
                    self.logger.info(f"Hourly rebalance check: {bal_str}, diff=${diff:.2f} — rebalancing")
                    await self._rebalance_wallets()
                else:
                    self.logger.debug(f"Hourly rebalance check: balanced ({bal_str})")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Rebalance loop error: {e}")
                await asyncio.sleep(3600)

    async def send_startup_notification(self):
        """Send status update to admin when bot comes online."""
        try:
            # Get balances for all wallets (combined pool)
            total_usdc = 0.0
            total_deployed = 0.0
            pos_count = 0
            wallet_lines = []

            wallet_roles = {1: "swing", 2: "scalp", 3: "scalp", 4: "scalp"}
            for wid, acct in self._all_wallets():
                usdc = await asyncio.to_thread(self._get_portfolio_value_for, acct)
                total_usdc += usdc

                try:
                    positions = await asyncio.to_thread(
                        chain_fetch_positions, self.w3, acct.address
                    )
                    deployed = sum(p.collateral_amount for p in positions) if positions else 0.0
                    n_pos = len(positions) if positions else 0
                except Exception:
                    deployed = 0.0
                    n_pos = 0
                total_deployed += deployed
                pos_count += n_pos

                addr = f"{acct.address[:8]}...{acct.address[-6:]}"
                role = wallet_roles.get(wid, "scalp")
                wallet_lines.append(f"W{wid} ({role}): {addr} — ${usdc:,.2f} USDC")

            collateral_per_trade = total_usdc * PORTFOLIO_PCT

            msg = (
                "🟢 **Bot Online**\n\n"
                + "\n".join(wallet_lines) + "\n"
                f"Network: {NETWORK.upper()}\n"
                f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}\n\n"
                f"Combined USDC: ${total_usdc:,.2f}\n"
                f"Deployed: ${total_deployed:,.2f}\n"
                f"Collateral/trade: ${collateral_per_trade:,.2f} ({PORTFOLIO_PCT:.0%} of USDC)\n"
                f"Open positions: {pos_count}\n\n"
                f"Max leverage: {MAX_LEVERAGE:.0f}x\n"
                f"Require TP/SL: {REQUIRE_TP}/{REQUIRE_SL}\n"
                f"Channels: {', '.join(TELEGRAM_CHANNELS)}"
            )
            await self.notify(msg)
        except Exception as e:
            self.logger.error(f"Startup notification error: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # Signal Processing
    # ──────────────────────────────────────────────────────────────────────
    async def process_signal(self, text: str):
        """Process a trading signal from a Telegram channel."""
        try:
            self.health_stats["signals_processed"] += 1

            if self.is_halted:
                self.logger.info("Signal ignored — bot is halted")
                return

            if not text or len(text) < 10:
                return

            # Skip update / status messages (e.g. TP hit, SL triggered, etc.)
            # These are channel updates about existing positions, NOT new signals.
            _UPDATE_PATTERNS = [
                # Target / TP hit announcements
                r"target\s*\d*\s*(?:was\s+)?(?:hit|reached|smashed|done|nailed|achieved|✅)",
                r"tp\s*\d*\s*(?:was\s+)?(?:hit|reached|smashed|done|nailed|achieved|✅)",
                r"(?:first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|final|last|all)\s*targets?\s*(?:was\s+)?(?:hit|reached|smashed|done)",
                r"all\s*(?:tp|targets?)\s*(?:hit|reached|done|smashed)",
                # Stop loss / stopped out
                r"stopped?\s*(?:out|loss)",
                r"sl\s*(?:was\s+)?(?:hit|triggered|reached|filled)",
                r"stop\s*loss\s*(?:was\s+)?(?:hit|triggered|reached|filled)",
                # SL moved / breakeven updates
                r"sl\s*(?:moved?|set|adjusted)\s*(?:to|at)",
                r"(?:move|moved|set|adjust)\s*(?:sl|stop\s*loss)\s*(?:to|at)",
                r"breakeven",
                r"break\s*even",
                # Position closed / profit taken
                r"closed?\s*(?:in|at|with|for)\s*(?:profit|loss|[\+\-])",
                r"position\s*(?:closed?|exited)",
                r"trade\s*(?:closed?|exited|done|finished)",
                r"(?:profit|loss)\s*(?:taken|booked|secured|locked)",
                # Running in profit / loss updates
                r"running\s*(?:in\s*)?(?:profit|loss|\+|\-)",
                # PnL result lines (e.g. "+350 pips", "PnL: +5.2%")
                r"pnl\s*[:=]",
                r"[\+\-]\s*\d+(?:\.\d+)?\s*(?:pips?|%|usd|usdt)",
                # Explicit "update" language
                r"(?:signal|trade)\s*update",
            ]
            lower = text.lower()
            for pat in _UPDATE_PATTERNS:
                if re.search(pat, lower):
                    self.logger.debug(f"Ignored update message (matched '{pat}'): {text[:80]}")
                    return

            # Try to parse with open.py's robust parser
            try:
                signal = parse_signal(text)
            except (ValueError, Exception) as e:
                self.logger.debug(f"Could not parse signal: {e}")
                return

            # Only trade BTC, ETH, SOL
            if signal.symbol not in ALLOWED_SYMBOLS:
                self.logger.debug(f"Ignored signal for {signal.symbol} — not in allowed pairs (BTC/ETH/SOL)")
                return

            self.logger.info(f"Signal parsed: {signal.symbol} {signal.side} [{signal.trade_type}]")

            # Validation
            if not await self.validate_signal(signal):
                return

            # Pick wallet based on trade type:
            #   swing → W1 only
            #   scalp → W2, W3, W4 (first available)
            wallet_id, acct = await self._pick_wallet(signal.symbol, signal.trade_type)
            if not acct:
                await self.notify(
                    f"Rejected {signal.symbol} {signal.side} [{signal.trade_type}]: "
                    "no available wallets"
                )
                return

            wallet_label = f" [W{wallet_id}]" if len(self._all_wallets()) > 1 else ""
            type_label = signal.trade_type.upper()

            # Determine collateral = PORTFOLIO_PCT of TOTAL portfolio (free USDC + deployed collateral + PnL)
            total_portfolio = await self._get_total_portfolio_value()
            free_usdc = await self._get_combined_usdc()

            if total_portfolio <= 0:
                await self.notify(f"Rejected {signal.symbol}: total portfolio value is $0")
                return

            # Cap leverage at MAX_LEVERAGE first so collateral calculation is correct
            signal.leverage = min(signal.leverage, MAX_LEVERAGE)

            collateral_usd = total_portfolio * PORTFOLIO_PCT
            size_usd = collateral_usd * signal.leverage

            if collateral_usd < MIN_POSITION_USD:
                await self.notify(
                    f"Rejected {signal.symbol}: collateral ${collateral_usd:.2f} "
                    f"({PORTFOLIO_PCT:.0%} of ${total_portfolio:.2f} portfolio) too small (min ${MIN_POSITION_USD:.0f})"
                )
                return

            # Notify that we're executing
            tp_list = ", ".join(f"${tp.price:,.0f} ({tp.close_pct:.0%})" for tp in signal.take_profits)
            await self.notify(
                f"Executing {signal.symbol} {signal.side}{wallet_label} [{type_label}]\n"
                f"Entry: ${signal.entry_low:,.0f}-${signal.entry_high:,.0f}\n"
                f"TP: {tp_list}\n"
                f"SL: ${signal.stop_loss:,.0f}\n"
                f"Portfolio: ${total_portfolio:.0f} (free: ${free_usdc:.0f} + deployed)\n"
                f"Collateral: ${collateral_usd:.0f} ({PORTFOLIO_PCT:.0%} of ${total_portfolio:.0f})\n"
                f"Size: ${size_usd:.0f} @ {signal.leverage:.0f}x\n"
                f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}"
            )

            # Check that the selected wallet has enough USDC for the collateral.
            # If not, rebalance first and re-check.
            wallet_usdc = await asyncio.to_thread(self._get_portfolio_value_for, acct)
            required_collateral = size_usd / signal.leverage if signal.leverage else size_usd
            if wallet_usdc < required_collateral:
                self.logger.warning(
                    f"W{wallet_id} has ${wallet_usdc:.2f} USDC but needs "
                    f"${required_collateral:.2f} collateral — rebalancing first"
                )
                await self.notify(
                    f"⚠️ W{wallet_id} low: ${wallet_usdc:.2f} USDC "
                    f"(need ${required_collateral:.2f}) — auto-rebalancing..."
                )
                await self._rebalance_wallets()

                # Re-check after rebalance
                wallet_usdc = await asyncio.to_thread(self._get_portfolio_value_for, acct)
                if wallet_usdc < required_collateral:
                    await self.notify(
                        f"Rejected {signal.symbol} {signal.side}: W{wallet_id} still only "
                        f"${wallet_usdc:.2f} USDC after rebalance (need ${required_collateral:.2f})"
                    )
                    return

            # Execute on-chain with the selected wallet
            position, order_type = await self.execute_open(signal, size_usd, acct, collateral_usd=collateral_usd)
            if position:
                position.wallet_id = wallet_id
                self.positions[position.id] = position
                self.health_stats["trades_executed"] += 1
                await self.notify_position_opened(position, order_type)
                # Top up ETH for gas if balance is low
                await self.topup_eth_if_needed()
                # Rebalance USDC between wallets after opening
                await self._rebalance_wallets()

        except Exception as e:
            self.logger.error(f"Error processing signal: {e}\n{traceback.format_exc()}")
            self.health_stats["errors"] += 1
            await self.notify(f"Error processing signal: {e}")

    def _get_account(self, wallet_id: int) -> Account:
        """Get the Account object for a given wallet_id (1-4)."""
        if wallet_id == 4 and self.account4:
            return self.account4
        if wallet_id == 3 and self.account3:
            return self.account3
        if wallet_id == 2 and self.account2:
            return self.account2
        return self.account

    def _all_wallets(self) -> List[tuple]:
        """Return list of (wallet_id, account) for all configured wallets."""
        wallets = [(1, self.account)]
        if self.account2:
            wallets.append((2, self.account2))
        if self.account3:
            wallets.append((3, self.account3))
        if self.account4:
            wallets.append((4, self.account4))
        return wallets

    def _scalp_wallets(self) -> List[tuple]:
        """Return list of (wallet_id, account) for scalp wallets (W2-W4)."""
        wallets = []
        if self.account2:
            wallets.append((2, self.account2))
        if self.account3:
            wallets.append((3, self.account3))
        if self.account4:
            wallets.append((4, self.account4))
        return wallets

    async def _pick_wallet(self, symbol: str, trade_type: str = "scalp") -> tuple:
        """Pick which wallet to use for a new position based on trade type.

        Routing logic:
          - swing/long-term → W1 only
          - scalp → W2, W3, W4 (first available without that symbol)

        Queries ON-CHAIN positions (not just internal tracking) so the bot
        is aware of positions opened manually or before a reboot.
        Returns (wallet_id, account) or (None, None) if all wallets busy."""
        market_addr = GMX_V2_MARKETS.get(symbol, "").lower()
        if not market_addr:
            return 1, self.account  # unknown symbol, default to W1

        if trade_type == "swing":
            # Swing trades → only W1
            wallets = [(1, self.account)]
        else:
            # Scalp trades → W2, W3, W4
            wallets = self._scalp_wallets()
            if not wallets:
                # No scalp wallets configured, fall back to all wallets
                wallets = self._all_wallets()

        for wid, acct in wallets:
            try:
                chain_positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                has_symbol = any(
                    cp.market.lower() == market_addr for cp in chain_positions
                )
                if not has_symbol:
                    self.logger.info(
                        f"Wallet {wid} ({acct.address[:10]}...) has no {symbol} position — selected [{trade_type}]"
                    )
                    return wid, acct
                else:
                    self.logger.info(
                        f"Wallet {wid} ({acct.address[:10]}...) already has {symbol} open on-chain"
                    )
            except Exception as e:
                self.logger.warning(f"Failed to check wallet {wid} on-chain: {e}")
                continue

        return None, None

    async def _close_existing_position(self, pos: 'Position', new_signal: Signal) -> bool:
        """Close an existing position + cancel its orders to make room for a new signal.
        Returns True if successfully closed, False on failure."""
        try:
            pos_acct = self._get_account(pos.wallet_id)
            side = pos.side
            await self.notify(
                f"Overriding {pos.symbol} {side} — closing for new {new_signal.side} signal"
            )

            # Fetch the on-chain position to get GMXPosition object for close
            positions = await asyncio.to_thread(
                chain_fetch_positions, self.w3, pos_acct.address
            )
            market_addr = GMX_V2_MARKETS.get(pos.symbol)
            gmx_pos = None
            for p in positions:
                if p.market.lower() == market_addr.lower():
                    gmx_pos = p
                    break

            if not gmx_pos:
                self.logger.warning(f"No on-chain position found for {pos.symbol} — marking closed")
                pos.is_open = False
                pos.closed_at = time.time()
                pos.exit_reason = "override_not_found"
                # Still cancel any leftover orders
                try:
                    exchange = self.w3.eth.contract(
                        address=Web3.to_checksum_address(GMX_V2_EXCHANGE_ROUTER),
                        abi=EXCHANGE_ROUTER_ABI,
                    )
                    await asyncio.to_thread(
                        cancel_orders_for_market, self.w3, pos_acct, exchange, market_addr, DRY_RUN
                    )
                except Exception:
                    pass
                return True

            # Close the position
            tx_hash = await self.execute_close(gmx_pos, 1.0, acct=pos_acct)
            if not tx_hash:
                self.logger.error(f"Failed to close {pos.symbol} for override")
                return False

            # Wait for position to disappear on-chain
            if not tx_hash.startswith("dry_run"):
                closed = await self.wait_for_position_closed(gmx_pos.market, gmx_pos.is_long, timeout=120, acct=pos_acct)
                if not closed:
                    self.logger.warning(f"{pos.symbol} did not close within timeout — verifying on-chain")
                    # Double-check: is the position really still there?
                    try:
                        remaining = await asyncio.to_thread(
                            chain_fetch_positions, self.w3, pos_acct.address
                        )
                        still_open = any(
                            p.market.lower() == gmx_pos.market.lower() and p.is_long == gmx_pos.is_long
                            for p in remaining
                        )
                    except Exception:
                        still_open = True  # assume still open if we can't verify
                    if still_open:
                        await self.notify(
                            f"⚠️ {pos.symbol} {side} close TX sent but position still on-chain after 120s.\n"
                            f"TX: {tx_hash}\nManual check recommended."
                        )
                        return False  # Don't mark closed if it's still there

            # Cancel all SL/TP orders for this market
            try:
                exchange = self.w3.eth.contract(
                    address=Web3.to_checksum_address(GMX_V2_EXCHANGE_ROUTER),
                    abi=EXCHANGE_ROUTER_ABI,
                )
                n_cancelled = await asyncio.to_thread(
                    cancel_orders_for_market, self.w3, pos_acct, exchange, gmx_pos.market, DRY_RUN
                )
                if n_cancelled:
                    self.logger.info(f"Cancelled {n_cancelled} orders for {pos.symbol} override")
            except Exception as e:
                self.logger.error(f"Failed to cancel orders during override: {e}")

            # Mark internal position as closed
            self._record_trade(pos, exit_reason="override")
            pos.is_open = False
            pos.closed_at = time.time()
            pos.exit_reason = "override"

            pnl_sign = "+" if gmx_pos.unrealized_pnl >= 0 else ""
            await self.notify(
                f"Closed {pos.symbol} {side} for override\n"
                f"PnL: {pnl_sign}${gmx_pos.unrealized_pnl:,.2f} ({pnl_sign}{gmx_pos.pnl_percentage:.1f}%)"
            )
            return True

        except Exception as e:
            self.logger.error(f"Error closing existing position for override: {e}")
            return False

    async def validate_signal(self, signal: Signal) -> bool:
        """Validate a parsed signal before execution."""
        # Require SL
        if REQUIRE_SL and (signal.stop_loss is None or signal.stop_loss <= 0):
            self.logger.warning("Rejected: no stop loss")
            await self.notify(f"Rejected {signal.symbol} {signal.side}: no stop loss")
            return False

        # Require TP
        if REQUIRE_TP and not signal.take_profits:
            self.logger.warning("Rejected: no take profit")
            await self.notify(f"Rejected {signal.symbol} {signal.side}: no take profit")
            return False

        # Note: wallet availability (on-chain position check) is done in
        # process_signal via _pick_wallet() after validation passes.

        # Price deviation check — reject if too far from signal entry
        try:
            current_price = await asyncio.to_thread(fetch_current_price, signal.symbol, self.w3)
            entry_avg = signal.entry_mid
            deviation = abs(current_price - entry_avg) / entry_avg
            max_deviation = 0.10  # 10% — reject if beyond this
            if deviation > max_deviation:
                self.logger.warning(
                    f"Price deviation {deviation:.1%} > {max_deviation:.0%} "
                    f"(current: ${current_price:,.0f}, entry: ${entry_avg:,.0f})"
                )
                await self.notify(
                    f"Rejected {signal.symbol}: price deviation {deviation:.1%} (>10%)\n"
                    f"Current: ${current_price:,.0f}, Signal entry: ${entry_avg:,.0f}"
                )
                return False
            elif deviation > 0.02:
                self.logger.info(
                    f"Price deviation {deviation:.1%} — executing at market price"
                )
        except Exception as e:
            self.logger.warning(f"Could not check price deviation: {e}")

        # Validate SL/TP direction
        if signal.is_long:
            if signal.stop_loss and signal.stop_loss >= signal.entry_low:
                self.logger.warning("LONG SL must be below entry")
                return False
            for tp in signal.take_profits:
                if tp.price <= signal.entry_high:
                    self.logger.warning(f"LONG TP ${tp.price:,.0f} must be above entry")
                    return False
        else:
            if signal.stop_loss and signal.stop_loss <= signal.entry_high:
                self.logger.warning("SHORT SL must be above entry")
                return False
            for tp in signal.take_profits:
                if tp.price >= signal.entry_low:
                    self.logger.warning(f"SHORT TP ${tp.price:,.0f} must be below entry")
                    return False

        return True

    # ──────────────────────────────────────────────────────────────────────
    # OPEN — Real on-chain execution via open.py
    # ──────────────────────────────────────────────────────────────────────
    async def execute_open(self, signal: Signal, size_usd: float, acct: Account = None, collateral_usd: float = None) -> tuple:
        """Execute a full open signal on-chain: MarketIncrease/LimitIncrease + TPs + SL.

        Returns (Position, order_type_str) or (None, None) on failure.
        """
        if acct is None:
            acct = self.account
        if collateral_usd is None:
            collateral_usd = size_usd / signal.leverage
        try:
            self.logger.info(
                f"Opening {signal.symbol} {signal.side} "
                f"${size_usd:.0f} @ {signal.leverage:.0f}x "
                f"(collateral: ${collateral_usd:.0f}, wallet: {acct.address[:10]}...)"
            )

            # Resolve the correct GMX V2 market address for this symbol
            market_addr = GMX_V2_MARKETS.get(signal.symbol, GMX_V2_MARKET)
            if not market_addr:
                raise ValueError(
                    f"No GMX V2 market address configured for {signal.symbol}. "
                    f"Set GMX_V2_MARKET_{signal.symbol} in your .env"
                )
            self.logger.info(f"Using market address for {signal.symbol}: {market_addr}")

            # Run synchronous web3 calls in a thread
            results = await asyncio.to_thread(
                execute_signal,
                self.w3,
                acct,
                signal,
                GMX_V2_EXCHANGE_ROUTER,
                GMX_V2_ORDER_VAULT,
                market_addr,
                GMX_V2_COLLATERAL_TOKEN,
                size_usd,
                collateral_usd,
                EXECUTION_FEE_WEI,
                SLIPPAGE_BPS,
                DRY_RUN,
            )

            order_type = results.get("order_type", "market")

            # Build internal Position record
            tp_levels = []
            for tp in signal.take_profits:
                tp_levels.append(TakeProfitLevel(price=tp.price, percentage=tp.close_pct))

            position = Position(
                id=str(uuid.uuid4()),
                symbol=signal.symbol,
                side=signal.side,
                size_usd=size_usd,
                leverage=signal.leverage,
                entry_price=signal.entry_mid,
                stop_loss=signal.stop_loss,
                take_profits=tp_levels,
                tx_hash=results.get("open"),
                tp_tx_hashes=[r.get("tx", "") for r in results.get("tp", []) if r.get("tx")],
                sl_tx_hash=results.get("sl"),
                market_addr=market_addr,
                last_known_tp_count=0,  # 0 = not yet verified on-chain; first poll will set actual count
                pending_fill=(order_type == "limit"),
                pending_fill_since=time.time() if order_type == "limit" else None,
            )

            self.logger.info(
                f"Position opened ({order_type}): {position.symbol} {position.side} "
                f"(tx: {position.tx_hash})"
            )
            return position, order_type

        except Exception as e:
            self.logger.error(f"Error executing open: {e}\n{traceback.format_exc()}")
            await self.notify(f"FAILED to open {signal.symbol} {signal.side}: {e}")
            return None, None

    # ──────────────────────────────────────────────────────────────────────
    # CLOSE — Real on-chain execution via close.py
    # ──────────────────────────────────────────────────────────────────────
    async def wait_for_position_closed(self, market: str, is_long: bool, timeout: int = 120, acct: Account = None) -> bool:
        """Poll chain until the position for (market, is_long) disappears or timeout (seconds).
        Returns True if position is gone, False if it still exists after timeout."""
        if acct is None:
            acct = self.account
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(5)
            try:
                positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                still_open = any(
                    p.market.lower() == market.lower() and p.is_long == is_long
                    for p in positions
                )
                if not still_open:
                    return True
            except Exception as e:
                self.logger.warning(f"Error polling position status: {e}")
        return False

    async def execute_close(self, gmx_pos: GMXPosition, percentage: float = 1.0, acct: Account = None) -> Optional[str]:
        """Execute a close order on-chain via close.py's create_close_order."""
        if acct is None:
            acct = self.account
        try:
            tx_hash = await asyncio.to_thread(
                create_close_order,
                self.w3,
                acct,
                gmx_pos,
                percentage,
                DRY_RUN,
                False,  # debug
            )
            return tx_hash if tx_hash else None
        except Exception as e:
            self.logger.error(f"Error executing close: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────
    # ETH gas top-up
    # ──────────────────────────────────────────────────────────────────────
    def _send_tx(self, to_addr: str, data: bytes, value: int, acct: Account = None) -> str:
        """Synchronous helper: build, sign, and send a transaction. Returns tx hash."""
        if acct is None:
            acct = self.account
        tx = _open_mod.build_tx(self.w3, acct.address, to_addr, data, value)
        return _open_mod.sign_send(self.w3, acct, tx, dry_run=False)

    async def topup_eth_if_needed(self):
        """After a trade, check ETH balance. If < $2 worth, swap $5 of USDC → ETH
        via Uniswap V3 on Arbitrum to ensure gas fees are always covered."""
        # Uniswap V3 SwapRouter02 on Arbitrum
        UNISWAP_ROUTER = Web3.to_checksum_address("0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")
        WETH_ARBITRUM   = Web3.to_checksum_address("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1")
        USDC_ARBITRUM   = Web3.to_checksum_address("0xaf88d065e77c8cC2239327C5EDb3A432268e5831")
        POOL_FEE        = 500  # 0.05% USDC/WETH pool

        UNISWAP_ABI = [
            {
                "name": "multicall",
                "type": "function",
                "stateMutability": "payable",
                "inputs": [{"name": "data", "type": "bytes[]"}],
                "outputs": [{"name": "", "type": "bytes[]"}],
            },
            {
                "name": "exactInputSingle",
                "type": "function",
                "stateMutability": "payable",
                "inputs": [{
                    "name": "params", "type": "tuple",
                    "components": [
                        {"name": "tokenIn",           "type": "address"},
                        {"name": "tokenOut",          "type": "address"},
                        {"name": "fee",               "type": "uint24"},
                        {"name": "recipient",         "type": "address"},
                        {"name": "amountIn",          "type": "uint256"},
                        {"name": "amountOutMinimum",  "type": "uint256"},
                        {"name": "sqrtPriceLimitX96", "type": "uint160"},
                    ],
                }],
                "outputs": [{"name": "amountOut", "type": "uint256"}],
            },
            {
                "name": "unwrapWETH9",
                "type": "function",
                "stateMutability": "payable",
                "inputs": [
                    {"name": "amountMinimum", "type": "uint256"},
                    {"name": "recipient",     "type": "address"},
                ],
                "outputs": [],
            },
        ]

        # Check and top up ALL wallets
        for wid, acct in self._all_wallets():
            await self._topup_eth_for_wallet(acct, f"W{wid}", UNISWAP_ROUTER, WETH_ARBITRUM, USDC_ARBITRUM, POOL_FEE, UNISWAP_ABI)

    async def _topup_eth_for_wallet(self, acct, label, UNISWAP_ROUTER, WETH_ARBITRUM, USDC_ARBITRUM, POOL_FEE, UNISWAP_ABI):
        """Top up ETH for a specific wallet if balance < $2."""
        try:
            wallet = acct.address

            # Check current ETH balance
            eth_bal = await asyncio.to_thread(self.w3.eth.get_balance, wallet)
            eth_amount = eth_bal / 10**18

            eth_price = await self.get_current_price("ETH")
            if not eth_price:
                try:
                    eth_price = await asyncio.to_thread(close_fetch_current_price, "ETH", self.w3)
                except Exception:
                    eth_price = 0.0
            if not eth_price:
                self.logger.warning(f"{label} ETH top-up: could not get ETH price, skipping")
                return

            eth_usd_value = eth_amount * eth_price
            if eth_usd_value >= 2.0:
                self.logger.debug(f"{label} ETH balance ${eth_usd_value:.2f} — no top-up needed")
                return

            topup_usd = 5.0
            self.logger.info(f"{label} ETH balance ${eth_usd_value:.2f} < $2 — topping up with ${topup_usd} of USDC")

            if DRY_RUN:
                self.logger.info(f"[DRY_RUN] Would swap $5 USDC → ETH for {label} gas top-up")
                await self.notify(f"[DRY RUN] {label} ETH balance low (${eth_usd_value:.2f}) — would swap $5 USDC → ETH")
                return

            # Check USDC balance
            usdc_token = self.w3.eth.contract(address=USDC_ARBITRUM, abi=ERC20_ABI)
            usdc_decimals = await asyncio.to_thread(lambda: usdc_token.functions.decimals().call())
            usdc_bal_raw  = await asyncio.to_thread(lambda: usdc_token.functions.balanceOf(wallet).call())
            usdc_bal      = usdc_bal_raw / (10 ** usdc_decimals)

            if usdc_bal < topup_usd:
                self.logger.warning(f"{label} ETH top-up: insufficient USDC (${usdc_bal:.2f} < ${topup_usd})")
                await self.notify(f"{label} ETH balance low (${eth_usd_value:.2f}) but not enough USDC to top up (${usdc_bal:.2f})")
                return

            usdc_amount_in = int(topup_usd * (10 ** usdc_decimals))

            # Approve Uniswap router for USDC if needed
            allowance = await asyncio.to_thread(
                lambda: usdc_token.functions.allowance(wallet, UNISWAP_ROUTER).call()
            )
            if allowance < usdc_amount_in:
                approve_data = usdc_token.encode_abi("approve", [UNISWAP_ROUTER, 2**256 - 1])
                approve_txh = await asyncio.to_thread(
                    self._send_tx, USDC_ARBITRUM, approve_data, 0, acct
                )
                await asyncio.to_thread(_open_mod.wait_receipt, self.w3, approve_txh)
                self.logger.info(f"{label} USDC approved for Uniswap: {approve_txh}")

            # Build exactInputSingle: USDC → WETH, recipient = router (for unwrap)
            router = self.w3.eth.contract(address=UNISWAP_ROUTER, abi=UNISWAP_ABI)
            swap_params = (
                USDC_ARBITRUM,   # tokenIn
                WETH_ARBITRUM,   # tokenOut
                POOL_FEE,        # fee
                UNISWAP_ROUTER,  # recipient = router so we can unwrap
                usdc_amount_in,  # amountIn
                0,               # amountOutMinimum (0 = no slippage guard, acceptable for small top-up)
                0,               # sqrtPriceLimitX96
            )
            swap_data   = router.encode_abi("exactInputSingle", [swap_params])
            unwrap_data = router.encode_abi("unwrapWETH9", [0, Web3.to_checksum_address(wallet)])
            call_data   = router.encode_abi("multicall", [[swap_data, unwrap_data]])

            txh = await asyncio.to_thread(self._send_tx, UNISWAP_ROUTER, call_data, 0, acct)
            receipt = await asyncio.to_thread(_open_mod.wait_receipt, self.w3, txh)

            if receipt.get("status") == 1:
                new_eth_bal = await asyncio.to_thread(self.w3.eth.get_balance, wallet)
                new_eth_usd = (new_eth_bal / 10**18) * eth_price
                self.logger.info(f"{label} ETH top-up complete. New ETH balance: ${new_eth_usd:.2f}")
                await self.notify(f"{label} ETH top-up: swapped $5 USDC → ETH (new balance ~${new_eth_usd:.2f})\nTX: {txh}")
            else:
                self.logger.error(f"{label} ETH top-up tx reverted: {txh}")
                await self.notify(f"{label} ETH top-up failed (tx reverted): {txh}")

        except Exception as e:
            self.logger.error(f"{label} ETH top-up error: {e}")

    async def cmd_topup(self, chat_id: int, arg: Optional[str] = None):
        """Manual ETH top-up command.

        Usage:
            /topup          — show ETH balances for all wallets
            /topup all      — top up all wallets with $5 USDC → ETH each
            /topup 1        — top up wallet 1 only
            /topup 2        — top up wallet 2 only
            /topup 3        — top up wallet 3 only
            /topup 4        — top up wallet 4 only
            /topup 1 10     — top up wallet 1 with $10 worth of USDC → ETH
            /topup all 15   — top up all wallets with $15 each
        """
        # Uniswap constants (same as topup_eth_if_needed)
        UNISWAP_ROUTER = Web3.to_checksum_address("0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")
        WETH_ARBITRUM   = Web3.to_checksum_address("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1")
        USDC_ARBITRUM   = Web3.to_checksum_address("0xaf88d065e77c8cC2239327C5EDb3A432268e5831")
        POOL_FEE        = 500

        UNISWAP_ABI = [
            {
                "name": "multicall", "type": "function", "stateMutability": "payable",
                "inputs": [{"name": "data", "type": "bytes[]"}],
                "outputs": [{"name": "", "type": "bytes[]"}],
            },
            {
                "name": "exactInputSingle", "type": "function", "stateMutability": "payable",
                "inputs": [{"name": "params", "type": "tuple", "components": [
                    {"name": "tokenIn",           "type": "address"},
                    {"name": "tokenOut",          "type": "address"},
                    {"name": "fee",               "type": "uint24"},
                    {"name": "recipient",         "type": "address"},
                    {"name": "amountIn",          "type": "uint256"},
                    {"name": "amountOutMinimum",  "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ]}],
                "outputs": [{"name": "amountOut", "type": "uint256"}],
            },
            {
                "name": "unwrapWETH9", "type": "function", "stateMutability": "payable",
                "inputs": [
                    {"name": "amountMinimum", "type": "uint256"},
                    {"name": "recipient",     "type": "address"},
                ],
                "outputs": [],
            },
        ]

        all_wallets = self._all_wallets()

        # Get ETH price for display
        eth_price = await self.get_current_price("ETH")
        if not eth_price:
            try:
                eth_price = await asyncio.to_thread(close_fetch_current_price, "ETH", self.w3)
            except Exception:
                eth_price = 0.0

        # ── No args: just show balances ──
        if not arg:
            lines = ["**ETH Balances (Gas)**\n"]
            for wid, acct in all_wallets:
                try:
                    eth_bal = await asyncio.to_thread(self.w3.eth.get_balance, acct.address)
                    eth_amount = eth_bal / 10**18
                    eth_usd = eth_amount * eth_price if eth_price else 0
                    lines.append(
                        f"W{wid}: {eth_amount:.6f} ETH (~${eth_usd:.2f})"
                    )
                except Exception as e:
                    lines.append(f"W{wid}: error fetching balance ({e})")
            lines.append(
                "\nUsage: /topup <1|2|3|4|all> [amount_usd]\n"
                "Default: $5 USDC → ETH"
            )
            await self.send_message(chat_id, "\n".join(lines))
            return

        # ── Parse args ──
        parts = arg.strip().split()
        target = parts[0].lower()
        topup_usd = 5.0  # default

        if len(parts) >= 2:
            try:
                topup_usd = float(parts[1].replace("$", ""))
            except ValueError:
                await self.send_message(chat_id, f"Invalid amount: {parts[1]}\nUsage: /topup <1|2|3|4|all> [amount_usd]")
                return

        if topup_usd < 1 or topup_usd > 100:
            await self.send_message(chat_id, "Amount must be between $1 and $100")
            return

        # Determine which wallets to top up
        wallet_map = {
            "1": (1, self.account), "w1": (1, self.account),
            "2": (2, self.account2), "w2": (2, self.account2),
            "3": (3, self.account3), "w3": (3, self.account3),
            "4": (4, self.account4), "w4": (4, self.account4),
        }
        if target == "all":
            targets = all_wallets
        elif target in wallet_map:
            wid, acct = wallet_map[target]
            if not acct:
                await self.send_message(chat_id, f"Wallet {target} not configured")
                return
            targets = [(wid, acct)]
        else:
            await self.send_message(chat_id, f"Unknown target: {target}\nUsage: /topup <1|2|3|4|all> [amount_usd]")
            return

        if DRY_RUN:
            wallet_names = ", ".join(f"W{wid}" for wid, _ in targets)
            await self.send_message(chat_id, f"[DRY RUN] Would swap ${topup_usd:.0f} USDC → ETH for {wallet_names}")
            return

        # ── Execute top-ups ──
        results = []
        for wid, acct in targets:
            label = f"W{wid}"
            try:
                wallet = acct.address

                # Check USDC balance
                usdc_token = self.w3.eth.contract(address=USDC_ARBITRUM, abi=ERC20_ABI)
                usdc_decimals = await asyncio.to_thread(lambda: usdc_token.functions.decimals().call())
                usdc_bal_raw = await asyncio.to_thread(lambda: usdc_token.functions.balanceOf(wallet).call())
                usdc_bal = usdc_bal_raw / (10 ** usdc_decimals)

                if usdc_bal < topup_usd:
                    results.append(f"{label}: insufficient USDC (${usdc_bal:.2f} < ${topup_usd:.0f})")
                    continue

                usdc_amount_in = int(topup_usd * (10 ** usdc_decimals))

                # Approve if needed
                allowance = await asyncio.to_thread(
                    lambda: usdc_token.functions.allowance(wallet, UNISWAP_ROUTER).call()
                )
                if allowance < usdc_amount_in:
                    approve_data = usdc_token.encode_abi("approve", [UNISWAP_ROUTER, 2**256 - 1])
                    approve_txh = await asyncio.to_thread(
                        self._send_tx, USDC_ARBITRUM, approve_data, 0, acct
                    )
                    await asyncio.to_thread(_open_mod.wait_receipt, self.w3, approve_txh)

                # Swap USDC → ETH via Uniswap
                router = self.w3.eth.contract(address=UNISWAP_ROUTER, abi=UNISWAP_ABI)
                swap_params = (
                    USDC_ARBITRUM, WETH_ARBITRUM, POOL_FEE,
                    UNISWAP_ROUTER,  # recipient = router for unwrap
                    usdc_amount_in, 0, 0,
                )
                swap_data   = router.encode_abi("exactInputSingle", [swap_params])
                unwrap_data = router.encode_abi("unwrapWETH9", [0, Web3.to_checksum_address(wallet)])
                call_data   = router.encode_abi("multicall", [[swap_data, unwrap_data]])

                txh = await asyncio.to_thread(self._send_tx, UNISWAP_ROUTER, call_data, 0, acct)
                receipt = await asyncio.to_thread(_open_mod.wait_receipt, self.w3, txh)

                if receipt.get("status") == 1:
                    new_eth_bal = await asyncio.to_thread(self.w3.eth.get_balance, wallet)
                    new_eth_usd = (new_eth_bal / 10**18) * (eth_price or 0)
                    results.append(f"{label}: swapped ${topup_usd:.0f} USDC → ETH (balance: ${new_eth_usd:.2f})")
                else:
                    results.append(f"{label}: swap TX reverted ({txh[:18]}...)")

            except Exception as e:
                results.append(f"{label}: error — {e}")

        await self.send_message(chat_id, "**ETH Top-Up**\n\n" + "\n".join(results))

    # ──────────────────────────────────────────────────────────────────────
    # Price feeds — GMX Reader + Chainlink fallback
    # ──────────────────────────────────────────────────────────────────────
    async def price_update_loop(self):
        while True:
            try:
                await self.update_all_prices()
                await asyncio.sleep(PRICE_UPDATE_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Price update error: {e}")
                await asyncio.sleep(PRICE_UPDATE_INTERVAL)

    async def update_all_prices(self):
        active_symbols = {pos.symbol for pos in self.positions.values() if pos.is_open}

        # ── Primary: fetch prices from GMX chain positions (Reader contract) ──
        # This gives us the same prices GMX uses for its own PnL/SL/TP calculations.
        chain_prices = {}  # symbol -> current_price from GMX Reader
        open_positions = [p for p in self.positions.values() if p.is_open and p.market_addr]
        if open_positions:
            wallet_ids = {p.wallet_id for p in open_positions}
            for wid in wallet_ids:
                try:
                    acct = self._get_account(wid)
                    chain_pos_list = await asyncio.to_thread(
                        chain_fetch_positions, self.w3, acct.address
                    )
                    for cp in chain_pos_list:
                        sym = cp.symbol.upper().split("/")[0]
                        if cp.current_price and cp.current_price > 0:
                            chain_prices[sym] = cp.current_price
                except Exception as e:
                    self.logger.debug(f"GMX Reader price fetch failed for W{wid}: {e}")

        # ── Fallback: Chainlink for any symbols not covered by chain positions ──
        for symbol in active_symbols:
            if symbol not in chain_prices:
                price = await self.fetch_price(symbol)
                if price:
                    chain_prices[symbol] = price

        # Update cache with whatever prices we got
        for symbol, price in chain_prices.items():
            if price and price > 0:
                self.price_cache[symbol] = PriceData(symbol=symbol, price=price)
                self.health_stats["price_updates"] += 1

        # Update P&L for all open positions using GMX-sourced prices
        for pos in self.positions.values():
            if pos.is_open and pos.symbol in self.price_cache:
                pos.current_price = self.price_cache[pos.symbol].price
                if pos.side == "LONG":
                    change = pos.current_price - pos.entry_price
                else:
                    change = pos.entry_price - pos.current_price
                # size_usd already includes leverage — don't multiply again
                pos.unrealized_pnl = (change / pos.entry_price) * pos.size_usd

    async def get_current_price(self, symbol: str) -> Optional[float]:
        if symbol in self.price_cache and self.price_cache[symbol].is_fresh:
            return self.price_cache[symbol].price
        price = await self.fetch_price(symbol)
        if price:
            self.price_cache[symbol] = PriceData(symbol=symbol, price=price)
            return price
        return None

    async def fetch_price(self, symbol: str) -> Optional[float]:
        """Fetch price via Chainlink on-chain feed (primary), CoinGecko fallback."""
        try:
            price = await asyncio.to_thread(fetch_current_price, symbol, self.w3)
            if price and price > 0:
                return price
        except Exception as e:
            self.logger.debug(f"Price fetch failed for {symbol}: {e}")
        return None

    # ──────────────────────────────────────────────────────────────────────
    # Heartbeat
    # ──────────────────────────────────────────────────────────────────────
    async def heartbeat_loop(self):
        while True:
            try:
                await self.perform_health_check()
                self.last_heartbeat = time.time()
                await asyncio.sleep(HEARTBEAT_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Heartbeat error: {e}")
                await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def perform_health_check(self):
        # Prices are only fetched on-demand when a signal arrives, so a stale
        # price cache is normal and should never halt trading.
        pass

    # ──────────────────────────────────────────────────────────────────────
    # Daily summary — 10 PM ET every day
    # ──────────────────────────────────────────────────────────────────────
    async def daily_summary_loop(self):
        """Sleep until 10 PM ET, send summary, repeat daily."""
        ET = ZoneInfo("America/New_York")
        while True:
            try:
                now = datetime.now(ET)
                # Next 10 PM ET
                target = now.replace(hour=22, minute=0, second=0, microsecond=0)
                if now >= target:
                    target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                self.logger.info(
                    f"Daily summary scheduled for {target.strftime('%Y-%m-%d %I:%M %p %Z')} "
                    f"({wait_seconds / 3600:.1f}h from now)"
                )
                await asyncio.sleep(wait_seconds)
                await self.send_daily_summary()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Daily summary error: {e}")
                await asyncio.sleep(3600)  # retry in 1h on failure

    async def send_daily_summary(self):
        """Build and send the end-of-day summary to admin."""
        ET = ZoneInfo("America/New_York")
        now = datetime.now(ET)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_cutoff = today_start.timestamp()

        PNL_SYMBOLS = {"BTC", "ETH", "SOL"}

        # ── Today's closed trades ──
        todays_trades = [
            t for t in self.trade_history
            if t.closed_at >= today_cutoff and t.symbol in PNL_SYMBOLS
        ]
        all_trades = [
            t for t in self.trade_history
            if t.symbol in PNL_SYMBOLS
        ]

        # Daily PnL
        daily_pnl = sum(t.pnl_usd for t in todays_trades)
        daily_wins = sum(1 for t in todays_trades if t.pnl_usd > 0)
        daily_losses = sum(1 for t in todays_trades if t.pnl_usd < 0)
        daily_count = len(todays_trades)

        # Lifetime PnL
        lifetime_pnl = sum(t.pnl_usd for t in all_trades)
        lifetime_wins = sum(1 for t in all_trades if t.pnl_usd > 0)
        lifetime_losses = sum(1 for t in all_trades if t.pnl_usd < 0)
        lifetime_count = len(all_trades)
        lifetime_winrate = (lifetime_wins / lifetime_count * 100) if lifetime_count else 0.0

        # Daily win rate
        daily_winrate = (daily_wins / daily_count * 100) if daily_count else 0.0

        # Per-symbol daily breakdown
        symbol_lines = []
        for sym in ("BTC", "ETH", "SOL"):
            sym_trades = [t for t in todays_trades if t.symbol == sym]
            if sym_trades:
                sym_pnl = sum(t.pnl_usd for t in sym_trades)
                sym_sign = "+" if sym_pnl >= 0 else ""
                sym_w = sum(1 for t in sym_trades if t.pnl_usd > 0)
                symbol_lines.append(
                    f"  {sym}: {sym_sign}${sym_pnl:,.2f} ({sym_w}/{len(sym_trades)} wins)"
                )

        # Account balances
        balance_lines = []
        total_usdc = 0.0
        total_deployed = 0.0
        try:
            for wid, acct in self._all_wallets():
                usdc = await asyncio.to_thread(self._get_portfolio_value_for, acct)
                total_usdc += usdc
                try:
                    positions = await asyncio.to_thread(
                        chain_fetch_positions, self.w3, acct.address
                    )
                    deployed = sum(p.collateral_amount for p in positions) if positions else 0.0
                except Exception:
                    deployed = 0.0
                total_deployed += deployed
                addr = f"{acct.address[:8]}...{acct.address[-6:]}"
                balance_lines.append(f"  W{wid} ({addr}): ${usdc:,.2f} USDC")
        except Exception as e:
            self.logger.warning(f"Daily summary: balance fetch failed: {e}")
            balance_lines.append("  (could not fetch balances)")

        # Open positions unrealized PnL
        open_pnl = 0.0
        open_count = 0
        try:
            for _, acct in self._all_wallets():
                cps = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                for cp in cps:
                    open_pnl += cp.unrealized_pnl
                    open_count += 1
        except Exception:
            pass

        # Build message
        d_sign = "+" if daily_pnl >= 0 else ""
        l_sign = "+" if lifetime_pnl >= 0 else ""
        o_sign = "+" if open_pnl >= 0 else ""

        msg = (
            f"📊 Daily Summary — {now.strftime('%b %d, %Y')}\n\n"
            f"Today ({daily_count} trades):\n"
            f"  PnL: {d_sign}${daily_pnl:,.2f}\n"
            f"  Win Rate: {daily_winrate:.0f}% ({daily_wins}W / {daily_losses}L)\n"
        )
        if symbol_lines:
            msg += "\n".join(symbol_lines) + "\n"

        msg += (
            f"\nLifetime ({lifetime_count} trades):\n"
            f"  PnL: {l_sign}${lifetime_pnl:,.2f}\n"
            f"  Win Rate: {lifetime_winrate:.0f}% ({lifetime_wins}W / {lifetime_losses}L)\n"
        )

        if open_count:
            msg += (
                f"\nOpen Positions ({open_count}):\n"
                f"  Unrealized: {o_sign}${open_pnl:,.2f}\n"
            )

        msg += (
            "\nAccount Balance:\n"
            + "\n".join(balance_lines) + "\n"
            f"  Total USDC: ${total_usdc:,.2f}\n"
            f"  Deployed: ${total_deployed:,.2f}"
        )

        await self.notify(msg)
        self.logger.info(f"Daily summary sent: daily PnL={d_sign}${daily_pnl:,.2f}")

    # ──────────────────────────────────────────────────────────────────────
    # TP-hit monitoring — move SL to entry when any TP fires
    # ──────────────────────────────────────────────────────────────────────
    async def tp_monitor_loop(self):
        """Poll on-chain every 30s. Detects:
          - TP hits (order count decreased) → move SL to entry
          - Position closed (SL hit / liquidation / all TPs filled) → report PnL
        """
        while True:
            try:
                await asyncio.sleep(30)
                await self.check_pending_fills()
                await self.check_position_closed()
                await self.check_tp_hits()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"TP monitor error: {e}")
                await asyncio.sleep(30)

    async def check_pending_fills(self):
        """Check if limit-order positions have filled on-chain yet.
        If the position now exists → mark pending_fill=False (active).
        If the limit order has disappeared (cancelled/expired) and no
        position exists → mark closed and notify admin."""
        pending = [p for p in self.positions.values()
                   if p.is_open and p.pending_fill and p.market_addr]
        if not pending:
            return

        for pos in pending:
            acct = self._get_account(pos.wallet_id)
            try:
                # Check if a real position exists on-chain now
                chain_positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                is_long = pos.side == "LONG"
                position_exists = any(
                    cp.market.lower() == pos.market_addr.lower() and cp.is_long == is_long
                    for cp in chain_positions
                )

                if position_exists:
                    # Limit order filled — position is now live on-chain
                    pos.pending_fill = False
                    self.logger.info(
                        f"Limit order filled: {pos.symbol} {pos.side} now active on-chain"
                    )
                    await self.notify(
                        f"✅ Limit order filled — {pos.symbol} {pos.side} is now active on-chain"
                    )
                    continue

                # Position doesn't exist yet — check if the limit order is still pending
                orders = await asyncio.to_thread(
                    fetch_open_orders, self.w3, acct.address
                )
                # Limit increase orders are type 2 or 3
                has_pending_order = any(
                    o["market"].lower() == pos.market_addr.lower()
                    and o["order_type"] in (2, 3)
                    and o["is_long"] == is_long
                    for o in orders
                )

                if not has_pending_order:
                    # Limit order is gone AND no position exists → cancelled/expired
                    pos.is_open = False
                    pos.closed_at = time.time()
                    pos.exit_reason = "Limit order cancelled/expired"
                    self.logger.info(
                        f"Limit order gone without fill: {pos.symbol} {pos.side}"
                    )
                    await self.notify(
                        f"⚠️ {pos.symbol} {pos.side} — Limit order cancelled or expired (never filled)"
                    )
                # else: order still pending, keep waiting

            except Exception as e:
                self.logger.debug(f"check_pending_fills error for {pos.symbol}: {e}")

            # Staleness guard: warn if limit order has been pending too long (30 min)
            if (pos.pending_fill and pos.is_open
                    and pos.pending_fill_since
                    and time.time() - pos.pending_fill_since > 1800):
                # Only warn once — reset the timer so we don't spam
                elapsed_min = (time.time() - pos.pending_fill_since) / 60
                self.logger.warning(
                    f"Limit order for {pos.symbol} {pos.side} pending for {elapsed_min:.0f} min"
                )
                await self.notify(
                    f"⏳ {pos.symbol} {pos.side} limit order still pending after {elapsed_min:.0f} min.\n"
                    f"Entry target: ${pos.entry_price:,.2f}"
                )
                pos.pending_fill_since = time.time()  # reset to avoid repeat alerts every 30s

    async def check_position_closed(self):
        """Detect when a tracked open position disappears from chain
        (SL hit, liquidation, or all TPs filled) and report PnL.

        Safety: if a position appears gone, we do a SECOND confirmation
        fetch before declaring it closed — avoids false closures from
        transient RPC errors or stale reads."""
        open_positions = [p for p in self.positions.values()
                          if p.is_open and p.market_addr and not p.pending_fill]
        if not open_positions:
            return

        # Fetch chain positions per wallet (with 1 retry on failure)
        chain_keys_by_wallet: Dict[int, set] = {}
        for wid in {p.wallet_id for p in open_positions}:
            acct = self._get_account(wid)
            for attempt in range(2):
                try:
                    cps = await asyncio.to_thread(
                        chain_fetch_positions, self.w3, acct.address
                    )
                    chain_keys_by_wallet[wid] = {
                        (cp.market.lower(), cp.is_long) for cp in cps
                    }
                    break  # success
                except Exception as e:
                    if attempt == 0:
                        self.logger.debug(f"check_position_closed: fetch attempt 1 failed for wallet {wid}: {e}, retrying...")
                        await asyncio.sleep(3)
                    else:
                        self.logger.warning(f"check_position_closed: fetch failed twice for wallet {wid}: {e}")
                        chain_keys_by_wallet[wid] = None  # skip this wallet

        for pos in open_positions:
            chain_keys = chain_keys_by_wallet.get(pos.wallet_id)
            if chain_keys is None:
                continue  # fetch failed for this wallet, skip
            key = (pos.market_addr.lower(), pos.side == "LONG")
            if key not in chain_keys:
                # Position appears gone — do a CONFIRMATION fetch before acting.
                # This prevents false closures from transient RPC issues.
                await asyncio.sleep(5)
                try:
                    acct = self._get_account(pos.wallet_id)
                    confirm_cps = await asyncio.to_thread(
                        chain_fetch_positions, self.w3, acct.address
                    )
                    confirm_keys = {(cp.market.lower(), cp.is_long) for cp in confirm_cps}
                    if key in confirm_keys:
                        self.logger.info(
                            f"Position {pos.symbol} {pos.side} reappeared on confirmation check — skipping close"
                        )
                        continue  # false alarm, position is still there
                except Exception as e:
                    self.logger.warning(
                        f"Confirmation fetch failed for {pos.symbol}: {e} — skipping close to be safe"
                    )
                    continue  # can't confirm, don't close

                # CONFIRMED: position is gone from chain

                # ── SAFETY CHECK: verify no active orders remain for this market ──
                # If SL/TP orders still exist, the position may still be partially open
                # (GMX hasn't executed them yet). This catches edge cases where the
                # position appears gone momentarily during keeper execution.
                try:
                    acct = self._get_account(pos.wallet_id)
                    remaining_orders = await asyncio.to_thread(
                        fetch_open_orders, self.w3, acct.address
                    )
                    market_orders = [
                        o for o in remaining_orders
                        if o["market"].lower() == pos.market_addr.lower()
                        and o["order_type"] in (
                            ORDER_TYPE_LIMIT_DECREASE,       # TP
                            ORDER_TYPE_STOP_LOSS_DECREASE,   # SL
                        )
                    ]
                    if market_orders:
                        self.logger.info(
                            f"Position {pos.symbol} {pos.side} appears gone but "
                            f"{len(market_orders)} SL/TP orders still exist on-chain. "
                            "Waiting for next cycle (orders will be auto-cancelled if truly closed)."
                        )
                        # Don't declare closed yet — if position is truly closed,
                        # keepers will cancel remaining orders and next cycle will
                        # find both position AND orders gone → proper close detection.
                        continue
                except Exception as e:
                    self.logger.debug(f"Could not check remaining orders: {e}")
                    # If we can't check, proceed with close detection (don't block indefinitely)

                # ── FETCH ACTUAL EXECUTION PRICE from on-chain events ──
                # Try to get the real fill price from GMX PositionDecrease events
                # instead of relying on current Chainlink price (which can drift)
                execution_price = None
                try:
                    acct = self._get_account(pos.wallet_id)
                    execution_price = await asyncio.to_thread(
                        fetch_execution_price,
                        self.w3,
                        acct.address,
                        pos.market_addr,
                        pos.side == "LONG",
                        300,  # ~5 min lookback on Arbitrum
                    )
                    if execution_price:
                        self.logger.info(
                            f"Got actual execution price for {pos.symbol} {pos.side}: "
                            f"${execution_price:,.2f}"
                        )
                except Exception as e:
                    self.logger.debug(f"Could not fetch execution price from events: {e}")

                # Fall back to Chainlink price if event parsing failed
                current_price = execution_price
                if not current_price:
                    current_price = await self.fetch_price(pos.symbol)
                if not current_price:
                    current_price = await self.get_current_price(pos.symbol)

                # ── PRICE VERIFICATION: only needed when we DON'T have the actual fill price ──
                # If we got the execution price from events, we know the close is real.
                # Only do price plausibility checks when falling back to Chainlink.
                if not execution_price and current_price and pos.stop_loss:
                    is_long = pos.side == "LONG"
                    sl_price = pos.stop_loss
                    tolerance = sl_price * 0.02  # 2% tolerance

                    # For a LONG: SL triggers when price drops BELOW SL
                    # For a SHORT: SL triggers when price rises ABOVE SL
                    sl_could_have_hit = False
                    if is_long and current_price <= sl_price + tolerance:
                        sl_could_have_hit = True
                    elif not is_long and current_price >= sl_price - tolerance:
                        sl_could_have_hit = True

                    # Check if any TP could have been hit (all TPs filled = position closed)
                    tp_could_have_hit = False
                    if pos.take_profits:
                        sorted_tps = sorted(pos.take_profits, key=lambda t: t.price,
                                            reverse=(pos.side == "SHORT"))
                        last_tp = sorted_tps[-1].price if sorted_tps else None
                        if last_tp:
                            if is_long and current_price >= last_tp - (last_tp * 0.02):
                                tp_could_have_hit = True
                            elif not is_long and current_price <= last_tp + (last_tp * 0.02):
                                tp_could_have_hit = True

                    if not sl_could_have_hit and not tp_could_have_hit:
                        # Price doesn't justify this close — do a THIRD check
                        self.logger.warning(
                            f"Position {pos.symbol} {pos.side} disappeared but price "
                            f"${current_price:,.2f} doesn't justify close. "
                            f"SL=${sl_price:,.2f}, doing 3rd verification..."
                        )
                        await asyncio.sleep(5)
                        try:
                            acct = self._get_account(pos.wallet_id)
                            third_check = await asyncio.to_thread(
                                chain_fetch_positions, self.w3, acct.address
                            )
                            third_keys = {(cp.market.lower(), cp.is_long) for cp in third_check}
                            if key in third_keys:
                                self.logger.info(
                                    f"Position {pos.symbol} {pos.side} reappeared on 3rd check — not closed"
                                )
                                continue
                        except Exception as e:
                            self.logger.warning(f"3rd verification failed: {e} — skipping close")
                            continue

                        # Position is truly gone but price doesn't match.
                        # Try one more time to get the actual execution price from events
                        # (maybe the event wasn't indexed yet on the first try).
                        if not execution_price:
                            self.logger.info(
                                f"Retrying execution price fetch for {pos.symbol} after 3rd check..."
                            )
                            try:
                                acct = self._get_account(pos.wallet_id)
                                execution_price = await asyncio.to_thread(
                                    fetch_execution_price,
                                    self.w3, acct.address, pos.market_addr,
                                    pos.side == "LONG", 500,  # wider lookback
                                )
                                if execution_price:
                                    current_price = execution_price
                                    self.logger.info(
                                        f"Got execution price on retry: ${execution_price:,.2f}"
                                    )
                            except Exception as e:
                                self.logger.debug(f"Retry execution price fetch failed: {e}")

                        # Still no matching price — flag it in the notification
                        if not execution_price:
                            self.logger.warning(
                                f"Position {pos.symbol} {pos.side} confirmed closed but "
                                f"price ${current_price:,.2f} doesn't match SL=${sl_price:,.2f}. "
                                "Proceeding with close (may be a liquidation or keeper execution)."
                            )

                # Determine exit reason — use actual SL price + TP hit count
                is_long = pos.side == "LONG"
                if pos.tp_hits_count > 0 and pos.last_known_tp_count == 0:
                    exit_reason = "All TPs filled"
                elif pos.sl_moved_to_entry and pos.stop_loss:
                    # SL was moved after TP hits. Check where it was moved to.
                    sl_label = pos.sl_move_label or "Entry"
                    sl_price = pos.stop_loss

                    # Is exit price near the SL price? (2% tolerance)
                    sl_tol = sl_price * 0.02
                    sl_triggered = False
                    if current_price:
                        if is_long and current_price <= sl_price + sl_tol:
                            sl_triggered = True
                        elif not is_long and current_price >= sl_price - sl_tol:
                            sl_triggered = True

                    if sl_triggered:
                        if sl_label == "Entry":
                            exit_reason = "SL (breakeven)"
                        else:
                            # SL was at a TP level — this means profit was locked in
                            exit_reason = f"SL at {sl_label} (${sl_price:,.2f})"
                    else:
                        # Price is NOT near SL — likely a TP fill or liquidation
                        if pos.tp_hits_count > 0:
                            exit_reason = f"TP/SL hit ({pos.tp_hits_count} TPs filled)"
                        else:
                            exit_reason = "SL/TP hit"
                elif pos.tp_hits_count > 0:
                    exit_reason = f"Closed ({pos.tp_hits_count} TPs hit)"
                elif pos.last_known_tp_count > 0:
                    exit_reason = "SL/TP/liquidation"
                else:
                    exit_reason = "SL/liquidation"

                # Calculate PnL from current price
                if current_price:
                    pos.current_price = current_price
                    if pos.side == "LONG":
                        change = current_price - pos.entry_price
                    else:
                        change = pos.entry_price - current_price
                    # size_usd already includes leverage — don't multiply again
                    pos.unrealized_pnl = (change / pos.entry_price) * pos.size_usd

                pnl_sign = "+" if pos.unrealized_pnl >= 0 else ""
                price_source = "fill" if execution_price else "est"
                price_str = f"${current_price:,.2f}" if current_price else "N/A"

                self.logger.info(
                    f"Position closed on-chain: {pos.symbol} {pos.side} "
                    f"PnL={pnl_sign}${pos.unrealized_pnl:,.2f} reason={exit_reason} "
                    f"price_source={price_source}"
                )

                # Determine emoji based on outcome
                if pos.unrealized_pnl > 0:
                    outcome_emoji = "🟢"
                elif pos.unrealized_pnl == 0:
                    outcome_emoji = "⚪"
                else:
                    outcome_emoji = "🔴"

                # Show "(fill)" for actual execution price, "(est)" for Chainlink fallback
                exit_label = f"Exit ({price_source}): {price_str}"

                # Warn if SL move had failed during this position's lifetime
                sl_warning = ""
                if pos.sl_move_failed:
                    sl_warning = "\n⚠️ Note: SL move failed earlier — exit may have been at original SL"

                await self.notify(
                    f"{outcome_emoji} {pos.symbol} {pos.side} — {exit_reason}\n"
                    f"Entry: ${pos.entry_price:,.2f}  |  {exit_label}\n"
                    f"PnL: {pnl_sign}${pos.unrealized_pnl:,.2f} ({pnl_sign}{pos.pnl_percentage:.1f}%)"
                    f"{sl_warning}"
                )

                # Record trade for /winrate and /pnl stats
                self._record_trade(pos, exit_reason=exit_reason)

                # Mark position as closed
                pos.is_open = False
                pos.closed_at = time.time()
                pos.exit_reason = exit_reason

                # Cancel any remaining SL/TP orders for this market
                try:
                    exchange = self.w3.eth.contract(
                        address=Web3.to_checksum_address(GMX_V2_EXCHANGE_ROUTER),
                        abi=EXCHANGE_ROUTER_ABI,
                    )
                    pos_acct = self._get_account(pos.wallet_id)
                    n_cancelled = await asyncio.to_thread(
                        cancel_orders_for_market,
                        self.w3,
                        pos_acct,
                        exchange,
                        pos.market_addr,
                        DRY_RUN,
                    )
                    if n_cancelled:
                        self.logger.info(
                            f"Cancelled {n_cancelled} orphaned order(s) for {pos.symbol}"
                        )
                        await self.notify(
                            f"Cancelled {n_cancelled} remaining order(s) for {pos.symbol}"
                        )
                except Exception as e:
                    self.logger.warning(f"Failed to cancel orphaned orders for {pos.symbol}: {e} — retrying in 10s")
                    # Retry once after a short delay
                    await asyncio.sleep(10)
                    try:
                        n_cancelled = await asyncio.to_thread(
                            cancel_orders_for_market,
                            self.w3,
                            pos_acct,
                            exchange,
                            pos.market_addr,
                            DRY_RUN,
                        )
                        if n_cancelled:
                            self.logger.info(f"Retry: cancelled {n_cancelled} orphaned order(s) for {pos.symbol}")
                            await self.notify(f"Cancelled {n_cancelled} remaining order(s) for {pos.symbol}")
                    except Exception as e2:
                        self.logger.error(f"Retry also failed for {pos.symbol}: {e2}")
                        await self.notify(f"⚠️ Could not cancel orphaned orders for {pos.symbol} — manual cleanup needed")

                # Top up ETH for gas if balance is low
                await self.topup_eth_if_needed()
                # Rebalance USDC between wallets after position closes
                await self._rebalance_wallets()

    async def check_tp_hits(self):
        """For each open position, count on-chain TP orders.
        If fewer than before → a TP was hit → move SL to entry.

        Key safeguards:
          - last_known_tp_count starts at 0 (unverified). The first poll
            that sees TPs on-chain sets the baseline — no action taken.
          - Before declaring a TP hit, we verify the position still exists
            on-chain. If it doesn't, the orders vanished because keepers
            auto-cancelled them (position rejected / already closed).
          - If ALL TPs vanish at once (count drops to 0 from >1), that's
            almost certainly keeper auto-cancellation, not multiple TP hits.
            We only act when count drops by 1 at a time.
        """
        open_positions = [p for p in self.positions.values()
                          if p.is_open and p.market_addr and not p.pending_fill]
        if not open_positions:
            return

        # Fetch orders per wallet
        orders_by_wallet: Dict[int, list] = {}
        for wid in {p.wallet_id for p in open_positions}:
            acct = self._get_account(wid)
            try:
                orders_by_wallet[wid] = await asyncio.to_thread(
                    fetch_open_orders, self.w3, acct.address
                )
            except Exception as e:
                self.logger.debug(f"check_tp_hits: fetch_open_orders failed for wallet {wid}: {e}")
                orders_by_wallet[wid] = None

        for pos in open_positions:
            orders = orders_by_wallet.get(pos.wallet_id)
            if orders is None:
                continue  # fetch failed for this wallet
            pos_acct = self._get_account(pos.wallet_id)
            market_lower = pos.market_addr.lower()
            is_long = pos.side == "LONG"

            # Count TP orders (type 5 = LimitDecrease) for this position's market
            tp_orders = [
                o for o in orders
                if o["market"].lower() == market_lower
                and o["order_type"] == ORDER_TYPE_LIMIT_DECREASE
            ]
            current_tp_count = len(tp_orders)

            if pos.last_known_tp_count == 0:
                # First poll — set baseline from actual on-chain count.
                # No action taken, just record what's really there.
                if current_tp_count > 0:
                    pos.last_known_tp_count = current_tp_count
                    self.logger.info(
                        f"TP baseline set: {pos.symbol} {pos.side} has "
                        f"{current_tp_count} TP order(s) on-chain"
                    )
                continue  # never act on the first poll

            if current_tp_count > pos.last_known_tp_count:
                # TP count went UP — this means a previous read was stale.
                # Restore baseline to the higher (correct) count.
                self.logger.info(
                    f"TP count increased for {pos.symbol} {pos.side}: "
                    f"{pos.last_known_tp_count} → {current_tp_count} (restoring baseline)"
                )
                pos.last_known_tp_count = current_tp_count
                continue

            if current_tp_count < pos.last_known_tp_count:
                hit_count = pos.last_known_tp_count - current_tp_count

                # ── Confirmation: re-fetch after 5s to rule out transient RPC stale reads ──
                self.logger.info(
                    f"TP count dropped for {pos.symbol} {pos.side}: "
                    f"{pos.last_known_tp_count} → {current_tp_count}. "
                    "Confirming in 5s..."
                )
                await asyncio.sleep(5)
                try:
                    confirm_orders = await asyncio.to_thread(
                        fetch_open_orders, self.w3, pos_acct.address
                    )
                    confirm_tp_count = len([
                        o for o in confirm_orders
                        if o["market"].lower() == market_lower
                        and o["order_type"] == ORDER_TYPE_LIMIT_DECREASE
                    ])
                except Exception as e:
                    self.logger.warning(f"TP confirmation fetch failed: {e} — skipping")
                    continue

                if confirm_tp_count >= pos.last_known_tp_count:
                    # False alarm — TPs are back. Transient RPC issue.
                    self.logger.info(
                        f"TP count restored for {pos.symbol} {pos.side}: "
                        f"confirmed {confirm_tp_count} TPs (was {pos.last_known_tp_count}). "
                        "False alarm — skipping."
                    )
                    pos.last_known_tp_count = confirm_tp_count
                    continue

                # Use the confirmed count
                current_tp_count = confirm_tp_count
                hit_count = pos.last_known_tp_count - current_tp_count

                # Safety: if ALL TPs vanished at once, verify position exists.
                # Keepers auto-cancel all orders when a position is rejected or
                # doesn't exist. A real TP execution removes 1 order at a time.
                if current_tp_count == 0 and hit_count > 1:
                    self.logger.info(
                        f"All {hit_count} TPs vanished for {pos.symbol} {pos.side} — "
                        "verifying position still exists on-chain..."
                    )
                    try:
                        chain_positions = await asyncio.to_thread(
                            chain_fetch_positions, self.w3, pos_acct.address
                        )
                        chain_pos = next(
                            (cp for cp in chain_positions
                             if cp.market.lower() == market_lower and cp.is_long == is_long),
                            None,
                        )
                    except Exception as e:
                        self.logger.warning(f"check_tp_hits: position check failed: {e} — skipping TP check")
                        continue

                    if not chain_pos:
                        self.logger.warning(
                            f"Position {pos.symbol} {pos.side} not found on-chain. "
                            "TPs were auto-cancelled (position rejected or already closed). "
                            "NOT treating as TP hit."
                        )
                        pos.last_known_tp_count = 0
                        continue

                # ── TP count dropped and re-fetch agrees. Now VERIFY PRICE ──
                # before committing the hit count, sending notifications, or moving SL.
                # This prevents acting on cancelled orders or stale event-log reads.

                sorted_tps = sorted(pos.take_profits, key=lambda t: t.price,
                                    reverse=(pos.side == "SHORT"))
                tentative_hits = pos.tp_hits_count + hit_count
                first_hit_idx = pos.tp_hits_count  # first new TP index

                # Wait for chain state to reflect the TP execution
                await asyncio.sleep(3)

                # Fetch fresh price + position data from chain
                chain_price = None
                chain_pos = None
                pnl_line = ""
                try:
                    chain_positions = await asyncio.to_thread(
                        chain_fetch_positions, self.w3, pos_acct.address
                    )
                    chain_pos = next(
                        (cp for cp in chain_positions
                         if cp.market.lower() == market_lower and cp.is_long == is_long),
                        None,
                    )
                    if chain_pos:
                        chain_price = chain_pos.current_price
                except Exception:
                    pass

                current_price = await self.get_current_price(pos.symbol)
                best_price = chain_price or current_price

                # ── PRICE VERIFICATION: did price actually reach the TP level? ──
                # GMX wouldn't execute a TP without the oracle price reaching it,
                # but our read could be stale. Verify against the FIRST TP that
                # supposedly hit (most conservative check).
                price_verified = True
                verified_hit_count = hit_count

                if first_hit_idx < len(sorted_tps) and not best_price:
                    # No price available — can't verify. Treat as stale.
                    self.logger.warning(
                        f"TP count dropped for {pos.symbol} {pos.side} but no "
                        "current price available — skipping TP verification."
                    )
                    pos.last_known_tp_count = current_tp_count
                    continue

                if first_hit_idx < len(sorted_tps) and best_price:
                    # Check the FIRST new TP that was supposedly hit
                    first_tp_price = sorted_tps[first_hit_idx].price
                    tolerance = first_tp_price * 0.03  # 3% tolerance

                    first_reached = False
                    if is_long and best_price >= first_tp_price - tolerance:
                        first_reached = True
                    elif not is_long and best_price <= first_tp_price + tolerance:
                        first_reached = True

                    if not first_reached:
                        # Price hasn't reached even the FIRST TP — this might be a
                        # cancelled order, not an execution. Don't act on it.
                        self.logger.warning(
                            f"TP count dropped for {pos.symbol} {pos.side} but price "
                            f"${best_price:,.2f} hasn't reached TP{first_hit_idx + 1} "
                            f"@ ${first_tp_price:,.2f}. Possible manual cancel or stale read. "
                            "NOT treating as TP hit — updating baseline."
                        )
                        # Update baseline but don't increment hit count or move SL
                        pos.last_known_tp_count = current_tp_count
                        continue

                    # First TP verified. Now check the last TP if multiple hit.
                    if hit_count > 1:
                        last_hit_idx = tentative_hits - 1
                        if last_hit_idx < len(sorted_tps):
                            last_tp_price = sorted_tps[last_hit_idx].price
                            last_tol = last_tp_price * 0.03

                            last_reached = False
                            if is_long and best_price >= last_tp_price - last_tol:
                                last_reached = True
                            elif not is_long and best_price <= last_tp_price + last_tol:
                                last_reached = True

                            if not last_reached:
                                # Price reached first TP but not the last — count fewer hits.
                                # Walk backwards to find how many TPs price actually reached.
                                verified_hit_count = 0
                                for h in range(hit_count):
                                    idx = first_hit_idx + h
                                    if idx < len(sorted_tps):
                                        tp_p = sorted_tps[idx].price
                                        tp_tol = tp_p * 0.03
                                        reached = False
                                        if is_long and best_price >= tp_p - tp_tol:
                                            reached = True
                                        elif not is_long and best_price <= tp_p + tp_tol:
                                            reached = True
                                        if reached:
                                            verified_hit_count += 1
                                        else:
                                            break  # stop at first unverified TP

                                if verified_hit_count == 0:
                                    verified_hit_count = 1  # at least first TP was verified

                                self.logger.info(
                                    f"Price verified {verified_hit_count} of {hit_count} "
                                    f"TP hits for {pos.symbol} (price ${best_price:,.2f})"
                                )

                # ── Verified: commit the TP hit count ──
                pos.tp_hits_count += verified_hit_count
                self.logger.info(
                    f"TP HIT verified: {pos.symbol} {pos.side} — "
                    f"{verified_hit_count} TP(s) confirmed "
                    f"(count was {pos.last_known_tp_count}, now {current_tp_count}), "
                    f"total hits: {pos.tp_hits_count}"
                )

                # Build PnL line for notification
                if chain_pos:
                    orig_collateral = pos.size_usd / pos.leverage if pos.leverage else 0
                    if orig_collateral > 0:
                        pnl_pct = (chain_pos.unrealized_pnl / orig_collateral) * 100
                    else:
                        pnl_pct = chain_pos.pnl_percentage
                    pnl_sign = "+" if chain_pos.unrealized_pnl >= 0 else ""
                    pnl_line = (
                        f"Current PnL: {pnl_sign}${chain_pos.unrealized_pnl:,.2f} "
                        f"({pnl_sign}{pnl_pct:.1f}%)\n"
                        f"Remaining size: ${chain_pos.size_usd:,.2f}\n"
                    )

                # Build TP info for notification
                tp_lines = []
                for h in range(verified_hit_count):
                    idx = first_hit_idx + h
                    if idx < len(sorted_tps):
                        tp_lines.append(f"TP{idx + 1} @ ${sorted_tps[idx].price:,.2f}")
                tp_info = ", ".join(tp_lines) + "\n" if tp_lines else ""

                if verified_hit_count == 1:
                    header = f"🎯 TP{pos.tp_hits_count} Hit: {pos.symbol} {pos.side}"
                else:
                    header = (
                        f"🎯 {verified_hit_count} TPs Hit: {pos.symbol} {pos.side} "
                        f"(TP{first_hit_idx + 1}–TP{first_hit_idx + verified_hit_count})"
                    )

                # Price verification tag
                price_tag = f"(verified @ ${best_price:,.2f})" if best_price else ""

                await self.notify(
                    f"{header} {price_tag}\n"
                    f"{tp_info}"
                    f"{pnl_line}"
                    f"TPs remaining: {current_tp_count}"
                )

                # ── Move SL (always — price is already verified above) ──
                hits = pos.tp_hits_count
                if hits <= 1:
                    new_sl_target = pos.entry_price
                    sl_label = "Entry"
                elif hits - 2 < len(sorted_tps):
                    new_sl_target = sorted_tps[hits - 2].price
                    sl_label = f"TP{hits - 1}"
                else:
                    new_sl_target = sorted_tps[-1].price if sorted_tps else pos.entry_price
                    sl_label = f"TP{len(sorted_tps)}" if sorted_tps else "Entry"

                await self.notify(
                    f"📊 SL moving → {sl_label} (${new_sl_target:,.2f}) "
                    f"for {pos.symbol} {pos.side}"
                )
                await self.move_sl(pos, orders, new_sl_target, sl_label)

                pos.last_known_tp_count = current_tp_count

    async def move_sl(self, pos: "Position", orders: list,
                      new_sl_price: float, sl_label: str = "entry"):
        """Cancel ALL existing SL orders for this market, VERIFY they are gone,
        then place a new one. Aborts if old SLs can't be confirmed cancelled.

        Progressive trailing:
          TP1 hit → SL to entry (breakeven)
          TP(N) hit (N>=2) → SL to TP(N-1) price
        """
        try:
            pos_acct = self._get_account(pos.wallet_id)
            market_lower = pos.market_addr.lower()
            is_long = pos.side == "LONG"

            exchange = self.w3.eth.contract(
                address=Web3.to_checksum_address(GMX_V2_EXCHANGE_ROUTER),
                abi=EXCHANGE_ROUTER_ABI,
            )

            # Re-fetch fresh orders to ensure we have current state
            try:
                fresh_orders = await asyncio.to_thread(
                    fetch_open_orders, self.w3, pos_acct.address
                )
            except Exception as e:
                self.logger.warning(f"move_sl: could not re-fetch orders, aborting to be safe: {e}")
                await self.notify(f"⚠️ Could not fetch orders for {pos.symbol} — SL NOT moved. Check manually.")
                return

            # Find ALL SL orders for this market (cancel every one)
            sl_orders = [
                o for o in fresh_orders
                if o["market"].lower() == market_lower
                and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
            ]

            self.logger.info(
                f"move_sl: found {len(sl_orders)} SL order(s) to cancel for {pos.symbol}"
            )

            # ── PRE-VALIDATE: check which SL orders are still active via gas estimate ──
            # Event-log-based order fetching can return stale keys (already executed/cancelled).
            # Gas estimate for cancelOrder will fail if the order is gone → skip those.
            validated_sls = []
            stale_count = 0
            last_error = ""
            for sl in sl_orders:
                if not sl.get("key_hex"):
                    self.logger.warning("  SL order has no key_hex, skipping")
                    continue
                key_bytes = bytes.fromhex(sl["key_hex"])
                try:
                    def _validate_key(kb=key_bytes, _acct=pos_acct):
                        exchange.functions.cancelOrder(kb).estimate_gas(
                            {"from": _acct.address}
                        )
                    await asyncio.to_thread(_validate_key)
                    validated_sls.append(sl)
                    self.logger.info(f"  SL 0x{sl['key_hex'][:16]}... validated (still active)")
                except Exception as e:
                    stale_count += 1
                    last_error = str(e)
                    self.logger.info(
                        f"  SL 0x{sl['key_hex'][:16]}... already gone (gas estimate failed): {e}"
                    )

            if stale_count > 0 and not validated_sls:
                # ALL SL orders were stale — they've already been executed/cancelled.
                # Safe to proceed directly to placing the new SL.
                self.logger.info(
                    f"move_sl: all {stale_count} old SL(s) already gone — proceeding to place new SL"
                )

            # Cancel validated (still-active) SL orders
            cancelled_count = 0
            cancelled_keys = set()
            for sl in validated_sls:
                key_bytes = bytes.fromhex(sl["key_hex"])
                self.logger.info(f"Cancelling old SL 0x{sl['key_hex'][:16]}...")
                try:
                    def _cancel_sl(kb=key_bytes, _acct=pos_acct):
                        data = exchange.encode_abi("cancelOrder", [kb])
                        tx = _open_mod.build_tx(
                            self.w3, _acct.address, exchange.address, data, value=0
                        )
                        txh = _open_mod.sign_send(self.w3, _acct, tx, dry_run=DRY_RUN)
                        if not DRY_RUN and not txh.startswith("dry_run"):
                            receipt = _open_mod.wait_receipt(self.w3, txh)
                            return txh, receipt.get("status") == 1
                        return txh, True
                    txh, receipt_ok = await asyncio.to_thread(_cancel_sl)
                    if receipt_ok:
                        self.logger.info(f"Old SL cancelled (receipt OK): {txh}")
                        cancelled_count += 1
                        cancelled_keys.add(sl["key_hex"])
                    else:
                        last_error = f"TX reverted: {txh}"
                        self.logger.warning(f"SL cancel TX reverted: {txh}")
                except Exception as e:
                    last_error = str(e)
                    self.logger.warning(f"Failed to cancel old SL: {e}")

            if validated_sls and cancelled_count == 0:
                # ── RETRY: wait and try cancelling again ──
                self.logger.warning(
                    f"Could not cancel any of {len(validated_sls)} validated SL orders — "
                    "retrying after 10s..."
                )
                await asyncio.sleep(10)
                try:
                    retry_orders = await asyncio.to_thread(
                        fetch_open_orders, self.w3, pos_acct.address
                    )
                    retry_sls = [
                        o for o in retry_orders
                        if o["market"].lower() == market_lower
                        and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
                        and o.get("key_hex")
                    ]
                    for sl in retry_sls:
                        kb = bytes.fromhex(sl["key_hex"])
                        # Re-validate before retrying
                        try:
                            def _revalidate(kb=kb, _acct=pos_acct):
                                exchange.functions.cancelOrder(kb).estimate_gas(
                                    {"from": _acct.address}
                                )
                            await asyncio.to_thread(_revalidate)
                        except Exception:
                            # Order gone — count as resolved
                            cancelled_count += 1
                            cancelled_keys.add(sl["key_hex"])
                            self.logger.info(f"Retry: SL 0x{sl['key_hex'][:16]}... now gone")
                            continue

                        try:
                            def _retry_cancel2(kb=kb, _acct=pos_acct):
                                data = exchange.encode_abi("cancelOrder", [kb])
                                tx = _open_mod.build_tx(
                                    self.w3, _acct.address, exchange.address, data, value=0
                                )
                                txh = _open_mod.sign_send(self.w3, _acct, tx, dry_run=DRY_RUN)
                                if not DRY_RUN and not txh.startswith("dry_run"):
                                    receipt = _open_mod.wait_receipt(self.w3, txh)
                                    return txh, receipt.get("status") == 1
                                return txh, True
                            txh, ok = await asyncio.to_thread(_retry_cancel2)
                            if ok:
                                cancelled_count += 1
                                cancelled_keys.add(sl["key_hex"])
                                self.logger.info(f"Retry cancel succeeded: {txh}")
                        except Exception as e2:
                            last_error = str(e2)
                            self.logger.warning(f"Retry cancel also failed: {e2}")
                except Exception as e:
                    last_error = str(e)
                    self.logger.warning(f"Retry fetch failed: {e}")

                if cancelled_count == 0:
                    self.logger.error(
                        f"Could not cancel any of {len(validated_sls)} old SL orders after retry! "
                        "Aborting new SL placement to avoid duplicates."
                    )
                    pos.sl_move_failed = True
                    await self.notify(
                        f"⚠️ Could not remove old SL for {pos.symbol} after retries. "
                        f"New SL NOT placed.\nError: {last_error}\n"
                        "Use /cancelorder to remove old SL, then /addorder sl to set new one."
                    )
                    return

            # ── VERIFY old SLs are actually gone ──
            # Event-log-based fetch_open_orders can lag behind the chain state.
            # If all cancel TXs got successful receipts, we trust that and proceed
            # even if the verification fetch still shows stale orders.
            all_receipts_ok = (cancelled_count == len([s for s in sl_orders if s.get("key_hex")]))

            if cancelled_count > 0 and not DRY_RUN:
                await asyncio.sleep(5)  # wait for event logs to catch up
                try:
                    verify_orders = await asyncio.to_thread(
                        fetch_open_orders, self.w3, pos_acct.address
                    )
                    remaining_sls = [
                        o for o in verify_orders
                        if o["market"].lower() == market_lower
                        and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
                        and o.get("key_hex") not in cancelled_keys  # exclude keys we successfully cancelled
                    ]
                    if remaining_sls:
                        self.logger.warning(
                            f"move_sl: {len(remaining_sls)} unknown SL(s) still exist after cancellation. "
                            "Retrying cancellation..."
                        )
                        # Retry cancelling only truly remaining ones (not stale reads of already-cancelled keys)
                        for sl in remaining_sls:
                            if not sl.get("key_hex"):
                                continue
                            try:
                                kb = bytes.fromhex(sl["key_hex"])
                                def _retry_cancel(kb=kb, _acct=pos_acct):
                                    data = exchange.encode_abi("cancelOrder", [kb])
                                    tx = _open_mod.build_tx(
                                        self.w3, _acct.address, exchange.address, data, value=0
                                    )
                                    txh = _open_mod.sign_send(self.w3, _acct, tx, dry_run=DRY_RUN)
                                    if not DRY_RUN and not txh.startswith("dry_run"):
                                        _open_mod.wait_receipt(self.w3, txh)
                                    return txh
                                await asyncio.to_thread(_retry_cancel)
                                cancelled_keys.add(sl["key_hex"])
                            except Exception as e:
                                self.logger.error(f"Retry cancel failed: {e}")
                except Exception as e:
                    self.logger.warning(f"move_sl: verification fetch failed: {e}")
                    # Continue — cancel TXs were confirmed via receipts

            # If all original cancel TXs had successful receipts, proceed regardless
            # of what the event-log-based order fetch shows (it can lag)
            if all_receipts_ok:
                self.logger.info(
                    f"move_sl: all {cancelled_count} cancel TX(s) confirmed via receipts — proceeding"
                )

            # Fetch remaining position size from chain for the new SL
            try:
                chain_positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, pos_acct.address
                )
                chain_pos = next(
                    (cp for cp in chain_positions
                     if cp.market.lower() == market_lower and cp.is_long == is_long),
                    None,
                )
                if not chain_pos:
                    self.logger.warning("Position no longer exists on-chain, skipping SL placement")
                    pos.sl_moved_to_entry = True
                    return
                remaining_size_usd = chain_pos.size_usd
            except Exception as e:
                self.logger.warning(f"Could not fetch remaining size, using original: {e}")
                remaining_size_usd = pos.size_usd

            self.logger.info(
                f"Placing new SL at {sl_label} ${new_sl_price:,.2f} "
                f"for ${remaining_size_usd:,.2f} size"
            )

            order_vault = Web3.to_checksum_address(GMX_V2_ORDER_VAULT)
            collateral_token = Web3.to_checksum_address(GMX_V2_COLLATERAL_TOKEN)

            # Place new SL — if this fails, try to restore old SL at previous price
            old_sl_price = pos.stop_loss  # save current SL price for fallback
            try:
                txh = await asyncio.to_thread(
                    create_sl_order,
                    self.w3,
                    pos_acct,
                    exchange,
                    pos_acct.address,
                    pos.market_addr,
                    collateral_token,
                    order_vault,
                    new_sl_price,
                    remaining_size_usd,
                    pos.symbol,
                    is_long,
                    SLIPPAGE_BPS,
                    EXECUTION_FEE_WEI,
                    DRY_RUN,
                )
            except Exception as place_err:
                self.logger.error(
                    f"Failed to place new SL at ${new_sl_price:,.2f}: {place_err}"
                )
                # CRITICAL: we cancelled old SL but failed to place new one.
                # Try to restore SL at the old price so position isn't unprotected.
                if old_sl_price and old_sl_price > 0:
                    self.logger.info(
                        f"Attempting to restore SL at old price ${old_sl_price:,.2f}..."
                    )
                    try:
                        restore_txh = await asyncio.to_thread(
                            create_sl_order,
                            self.w3, pos_acct, exchange, pos_acct.address,
                            pos.market_addr, collateral_token, order_vault,
                            old_sl_price, remaining_size_usd, pos.symbol, is_long,
                            SLIPPAGE_BPS, EXECUTION_FEE_WEI, DRY_RUN,
                        )
                        self.logger.info(f"Restored old SL at ${old_sl_price:,.2f}: {restore_txh}")
                        await self.notify(
                            f"⚠️ Failed to move SL to {sl_label} for {pos.symbol}. "
                            f"Restored SL at ${old_sl_price:,.2f}."
                        )
                    except Exception as restore_err:
                        self.logger.error(f"CRITICAL: Could not restore old SL either: {restore_err}")
                        pos.sl_move_failed = True
                        await self.notify(
                            f"🚨 {pos.symbol} {pos.side} HAS NO STOP LOSS!\n"
                            "Failed to place new SL AND failed to restore old one.\n"
                            "Use /addorder sl to set one immediately!"
                        )
                else:
                    pos.sl_move_failed = True
                    await self.notify(
                        f"🚨 {pos.symbol} {pos.side} HAS NO STOP LOSS!\n"
                        f"Failed to place SL at {sl_label} (${new_sl_price:,.2f}).\n"
                        "Use /addorder sl to set one immediately!"
                    )
                return

            pos.stop_loss = new_sl_price
            pos.sl_moved_to_entry = True
            pos.sl_move_label = sl_label  # "Entry", "TP1", "TP2", etc.

            # Fetch the new SL's order key so we can protect it from accidental cancellation
            if not DRY_RUN:
                await asyncio.sleep(3)
                try:
                    new_orders = await asyncio.to_thread(
                        fetch_open_orders, self.w3, pos_acct.address
                    )
                    new_sl_orders = [
                        o for o in new_orders
                        if o["market"].lower() == market_lower
                        and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
                        and o.get("key_hex")
                        and o["key_hex"] not in cancelled_keys
                    ]
                    if new_sl_orders:
                        pos.current_sl_key = new_sl_orders[0]["key_hex"]
                        self.logger.info(
                            f"Stored new SL key: 0x{pos.current_sl_key[:16]}..."
                        )
                    else:
                        self.logger.warning("Could not find new SL order key — may have been executed already")
                except Exception as e:
                    self.logger.debug(f"Could not fetch new SL key: {e}")

            self.logger.info(
                f"SL moved to {sl_label} ${new_sl_price:,.2f} for {pos.symbol} {pos.side} "
                f"size=${remaining_size_usd:,.2f} tx={txh}"
            )

            await self.notify(
                f"✅ SL placed at {sl_label} (${new_sl_price:,.2f}) for {pos.symbol} {pos.side}"
            )

        except Exception as e:
            self.logger.error(f"move_sl failed: {e}\n{traceback.format_exc()}")
            await self.notify(f"⚠️ Failed to move SL for {pos.symbol}: {e}")

    async def halt_trading(self, reason: str):
        self.is_halted = True
        self.halt_reason = reason
        self.halt_time = time.time()
        self.logger.warning(f"TRADING HALTED: {reason}")
        await self.notify(f"TRADING HALTED\n{reason}")

    async def resume_trading(self, reason: str):
        self.is_halted = False
        self.halt_reason = ""
        self.halt_time = None
        self.logger.info(f"Trading resumed: {reason}")
        await self.notify(f"TRADING RESUMED\n{reason}")

    # ──────────────────────────────────────────────────────────────────────
    # Analytics
    # ──────────────────────────────────────────────────────────────────────
    def calculate_win_rate(self, symbol: str = None, n: int = None) -> Dict[str, Any]:
        trades = self.trade_history
        if symbol:
            trades = [t for t in trades if t.symbol == symbol]
        if n:
            trades = trades[-n:]
        if not trades:
            return {"error": "No trades found"}
        wins = [t for t in trades if t.pnl_usd > 0]
        losses = [t for t in trades if t.pnl_usd < 0]
        gp = sum(t.pnl_usd for t in wins)
        gl = abs(sum(t.pnl_usd for t in losses))
        return {
            "total": len(trades), "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / len(trades) * 100,
            "profit_factor": gp / gl if gl > 0 else float("inf"),
            "gross_profit": gp, "gross_loss": gl,
            "avg_win": statistics.mean([t.pnl_usd for t in wins]) if wins else 0,
            "avg_loss": statistics.mean([t.pnl_usd for t in losses]) if losses else 0,
            "net_pnl": gp - gl,
        }

    # ──────────────────────────────────────────────────────────────────────
    # Admin Commands
    # ──────────────────────────────────────────────────────────────────────
    async def process_admin_command(self, text: str, chat_id: int):
        try:
            parts = text.strip().split()
            cmd = parts[0].lower()

            if cmd == "/help":
                await self.cmd_help(chat_id)
            elif cmd == "/status":
                await self.cmd_status(chat_id)
            elif cmd == "/positions":
                await self.cmd_positions(chat_id)
            elif cmd == "/close":
                arg = parts[1] if len(parts) > 1 else None
                await self.cmd_close(chat_id, arg)
            elif cmd == "/confirm":
                await self.handle_close_confirmation(chat_id, "YES")
            elif cmd == "/halt":
                reason = " ".join(parts[1:]) if len(parts) > 1 else "Manual halt"
                await self.halt_trading(reason)
            elif cmd == "/resume":
                reason = " ".join(parts[1:]) if len(parts) > 1 else "Manual resume"
                await self.resume_trading(reason)
            elif cmd == "/winrate":
                sym = parts[1].upper() if len(parts) > 1 else None
                n = int(parts[2]) if len(parts) > 2 else None
                await self.cmd_winrate(chat_id, sym, n)
            elif cmd == "/health":
                await self.cmd_health(chat_id)
            elif cmd == "/balance":
                await self.cmd_balance(chat_id)
            elif cmd == "/pnl":
                await self.cmd_pnl(chat_id)
            elif cmd == "/reset":
                await self.cmd_reset(chat_id)
            elif cmd == "/summary":
                await self.send_daily_summary()
            elif cmd in ("/balance-wallets", "/rebalance"):
                await self.cmd_balance_wallets(chat_id)
            elif cmd == "/lastmsg":
                await self.cmd_lastmsg(chat_id)
            elif cmd == "/increase":
                arg = parts[1] if len(parts) > 1 else None
                await self.cmd_increase(chat_id, arg)
            elif cmd == "/cancelorder":
                arg = " ".join(parts[1:]) if len(parts) > 1 else None
                await self.cmd_cancelorder(chat_id, arg)
            elif cmd == "/addorder":
                arg = " ".join(parts[1:]) if len(parts) > 1 else None
                await self.cmd_addorder(chat_id, arg)
            elif cmd == "/topup":
                arg = " ".join(parts[1:]) if len(parts) > 1 else None
                await self.cmd_topup(chat_id, arg)
            elif cmd == "/prices":
                await self.cmd_prices(chat_id)
            elif cmd == "/sl":
                arg = " ".join(parts[1:]) if len(parts) > 1 else None
                await self.cmd_sl(chat_id, arg)
            elif cmd == "/gas":
                await self.cmd_gas(chat_id)
            elif cmd == "/tradesize":
                arg = " ".join(parts[1:]) if len(parts) > 1 else None
                await self.cmd_tradesize(chat_id, arg)
            else:
                await self.send_message(chat_id, "Unknown command. Type /help")

        except Exception as e:
            self.logger.error(f"Admin command error: {e}\n{traceback.format_exc()}")
            await self.send_message(chat_id, f"Command error: {e}")

    async def cmd_help(self, chat_id: int):
        msg = """**GMX V2 Bot Commands**

/status — Bot status & mode
/positions — Show on-chain positions
/close — Show positions + open orders
/close all — Close all positions + cancel all orders
/close BTC — Close by symbol
/confirm — Confirm pending close
/sl — Move SL to entry or TP level
/sl 1 entry — Move #1 SL to entry (breakeven)
/sl 1 tp2 — Move #1 SL to TP2 price
/balance — Wallet ETH & token balance
/halt [reason] — Halt trading
/resume [reason] — Resume trading
/winrate [SYMBOL] [N] — Win rate stats
/pnl — PnL summary (today / 30d / all time) for BTC, SOL, ETH
/summary — Send daily summary now
/reset — Clear all trade history & PnL stats
/increase — Add collateral to an open position
/cancelorder — List & cancel individual SL/TP orders by number
/addorder — Manually add a SL or TP to an open position
/prices — Live GMX & Chainlink prices for all tracked assets
/gas — ETH gas balances for all wallets
/tradesize — Show/change trade size (e.g. /tradesize 20 for 20%)
/topup — Manual ETH top-up (swap USDC → ETH for gas)
/balance-wallets — Manually rebalance USDC between wallets (W1-W4)
/lastmsg — Print last message from monitored channel(s)
/health — System health
/help — This message

**Wallets:** W1=swing, W2-W4=scalps"""
        await self.send_message(chat_id, msg)

    async def cmd_status(self, chat_id: int):
        health = self.get_health_report()
        status = "HALTED" if health["status"] == "HALTED" else "ACTIVE"
        wallet_roles = {1: "swing", 2: "scalp", 3: "scalp", 4: "scalp"}
        wallet_lines = []
        for wid, acct in self._all_wallets():
            role = wallet_roles.get(wid, "scalp")
            wallet_lines.append(f"W{wid} ({role}): {acct.address[:8]}...{acct.address[-6:]}")
        wallet_str = "\n".join(wallet_lines) if wallet_lines else "N/A"
        msg = (
            "**GMX V2 Bot Status**\n\n"
            f"Status: {status}\n"
            f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}\n"
            f"{wallet_str}\n"
            f"Network: {NETWORK.upper()}\n"
            f"Uptime: {health['uptime_hours']:.1f}h\n\n"
            f"Positions: {health['open_positions']}\n"
            f"Exposure: ${health['total_exposure']:.0f}\n"
            f"Signals: {health['signals']}\n"
            f"Trades: {health['trades']}\n"
            f"Errors: {health['errors']}"
        )
        if health["status"] == "HALTED":
            msg += f"\n\nHalt reason: {self.halt_reason}"
        await self.send_message(chat_id, msg)

    async def _fetch_all_positions_and_orders(self):
        """Fetch positions and orders from all wallets, merged.
        Each GMXPosition gets a _wallet_acct and _wallet_id attribute for close routing."""
        all_positions = []
        all_orders = []
        for wid, acct in self._all_wallets():
            try:
                pos, ords = await asyncio.gather(
                    asyncio.to_thread(chain_fetch_positions, self.w3, acct.address),
                    asyncio.to_thread(fetch_open_orders, self.w3, acct.address),
                )
                for p in pos:
                    p._wallet_acct = acct  # tag with owning wallet
                    p._wallet_id = wid
                all_positions.extend(pos)
                for o in ords:
                    o["_wallet_acct"] = acct  # tag orders with owning wallet too
                    o["_wallet_id"] = wid
                all_orders.extend(ords)
            except Exception as e:
                self.logger.warning(f"Failed to fetch from W{wid} {acct.address[:10]}: {e}")
        return all_positions, all_orders

    async def cmd_positions(self, chat_id: int):
        """Show on-chain positions with their associated limit/SL/TP orders."""
        await self.send_message(chat_id, "Fetching positions and orders...")

        try:
            positions, orders = await self._fetch_all_positions_and_orders()
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching data: {e}")
            return

        if not positions and not orders:
            await self.send_message(chat_id, "No open positions or orders on-chain.")
            return

        msg = ""
        open_pos_markets = {p.market.lower() for p in positions}

        # ── Open Positions ──────────────────────────────────────────────
        if positions:
            msg += f"**Positions ({len(positions)})**\n"
            for i, pos in enumerate(positions, 1):
                side = "LONG" if pos.is_long else "SHORT"

                # Use GMX Reader current_price from the chain position data
                display_price = pos.current_price

                # Recalculate PnL with fresh GMX price
                if display_price and pos.entry_price:
                    if pos.is_long:
                        price_diff = display_price - pos.entry_price
                    else:
                        price_diff = pos.entry_price - display_price
                    pnl = (price_diff / pos.entry_price) * pos.size_usd
                    pnl_pct = (pnl / pos.collateral_amount) * 100 if pos.collateral_amount else 0
                else:
                    pnl = pos.unrealized_pnl
                    pnl_pct = pos.pnl_percentage
                pnl_icon = "+" if pnl >= 0 else ""

                # Orders tied to this position (same market)
                pos_orders = [
                    o for o in orders
                    if o["market"].lower() == pos.market.lower()
                ]
                tp_orders    = sorted([o for o in pos_orders if o["order_type"] == 5],
                                      key=lambda o: o["trigger_price"])
                sl_orders    = [o for o in pos_orders if o["order_type"] == 6]
                limit_orders = [o for o in pos_orders if o["order_type"] in (2, 3)]

                # Determine wallet label
                wid_label = ""
                if hasattr(pos, '_wallet_id'):
                    wid_label = f" [W{pos._wallet_id}]"
                elif hasattr(pos, '_wallet_acct'):
                    for wid_check, acct_check in self._all_wallets():
                        if pos._wallet_acct.address == acct_check.address:
                            wid_label = f" [W{wid_check}]"
                            break

                msg += (
                    f"\n**#{i} {pos.symbol} {side}{wid_label}**\n"
                    f"  Size:    ${pos.size_usd:,.2f} @ {pos.leverage:.1f}x\n"
                    f"  Collateral: ${pos.collateral_amount:,.2f}\n"
                    f"  Entry:   ${pos.entry_price:,.2f}\n"
                    f"  Current: ${display_price:,.2f}\n"
                    f"  PnL:     {pnl_icon}${pnl:.2f} ({pnl_icon}{pnl_pct:.1f}%)\n"
                )

                if sl_orders or tp_orders:
                    msg += "  SL & TP:\n"
                    for o in sl_orders:
                        tp_price = o["trigger_price"]
                        if tp_price and pos.entry_price:
                            if pos.is_long:
                                proj = ((tp_price - pos.entry_price) / pos.entry_price) * pos.size_usd
                            else:
                                proj = ((pos.entry_price - tp_price) / pos.entry_price) * pos.size_usd
                            proj_sign = "+" if proj >= 0 else ""
                            msg += f"    SL  @ ${tp_price:,.2f}  ({proj_sign}${proj:,.2f} projected)\n"
                        else:
                            msg += f"    SL  @ ${tp_price:,.2f}\n"
                    for j, o in enumerate(tp_orders, 1):
                        tp_price = o["trigger_price"]
                        if tp_price and pos.entry_price:
                            if pos.is_long:
                                proj = ((tp_price - pos.entry_price) / pos.entry_price) * pos.size_usd
                            else:
                                proj = ((pos.entry_price - tp_price) / pos.entry_price) * pos.size_usd
                            proj_sign = "+" if proj >= 0 else ""
                            msg += f"    TP{j} @ ${tp_price:,.2f}  ({proj_sign}${proj:,.2f} projected)\n"
                        else:
                            msg += f"    TP{j} @ ${tp_price:,.2f}\n"

                if limit_orders:
                    msg += "  Limit Orders:\n"
                    for o in limit_orders:
                        price_str = f"${o['trigger_price']:,.2f}" if o["trigger_price"] else "market"
                        msg += f"    Limit @ {price_str}  (${o['size_usd']:,.2f})\n"

            total_pnl  = sum(p.unrealized_pnl for p in positions)
            total_size = sum(p.size_usd for p in positions)
            msg += f"\nTotal Size: ${total_size:,.2f}  |  Total PnL: ${total_pnl:+.2f}\n"

        # ── Pending Limit Entry Orders (not yet filled — no open position) ──
        pending_entries = [
            o for o in orders
            if o["order_type"] in (2, 3)
            and o["market"].lower() not in open_pos_markets
        ]
        if pending_entries:
            msg += f"\n**Limit Orders ({len(pending_entries)})** _(pending entry)_\n"
            for o in pending_entries:
                side = "LONG" if o["is_long"] else "SHORT"
                price_str = f"${o['trigger_price']:,.2f}" if o["trigger_price"] else "market"
                msg += f"  {o['symbol']} {side} @ {price_str}  (${o['size_usd']:,.2f})\n"

        await self.send_message(chat_id, msg)

    async def cmd_close(self, chat_id: int, arg: Optional[str]):
        """Interactive close flow via Telegram.

        /close             — list positions + open orders, ask which to close
        /close all         — close all positions AND cancel all open orders
        /close BTC         — close BTC position (with confirm)
        """
        # Fetch on-chain positions AND open orders from all wallets
        await self.send_message(chat_id, "Fetching positions and orders...")
        try:
            positions, orders = await self._fetch_all_positions_and_orders()
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching data: {e}")
            return

        if not positions and not orders:
            await self.send_message(chat_id, "No open positions or orders to close.")
            return

        if arg is None:
            # Show positions with grouped orders (like /positions), ask which to close
            msg = ""
            open_pos_markets = {p.market.lower() for p in positions} if positions else set()

            if positions:
                msg += f"**Positions ({len(positions)})**\n"
                for i, pos in enumerate(positions, 1):
                    side = "LONG" if pos.is_long else "SHORT"

                    # Orders tied to this position (same market)
                    pos_orders = [
                        o for o in orders
                        if o["market"].lower() == pos.market.lower()
                    ]
                    tp_orders = sorted([o for o in pos_orders if o["order_type"] == 5],
                                       key=lambda o: o["trigger_price"])
                    sl_orders = [o for o in pos_orders if o["order_type"] == 6]

                    msg += (
                        f"\n**#{i} {pos.symbol} {side}**\n"
                        f"  Size: ${pos.size_usd:,.2f} @ {pos.leverage:.1f}x\n"
                        f"  PnL:  ${pos.unrealized_pnl:+.2f}\n"
                    )

                    if sl_orders or tp_orders:
                        for o in sl_orders:
                            msg += f"    SL  @ ${o['trigger_price']:,.2f}\n"
                        for j, o in enumerate(tp_orders, 1):
                            msg += f"    TP{j} @ ${o['trigger_price']:,.2f}\n"

            # Orphaned orders (not tied to any open position)
            orphaned = [
                o for o in orders
                if o["market"].lower() not in open_pos_markets
            ] if orders else []
            if orphaned:
                order_type_names = {2: "MarketIncrease", 3: "LimitIncrease", 4: "MarketDecrease",
                                    5: "TP", 6: "SL"}
                msg += f"\n**Orphaned Orders ({len(orphaned)}):**\n"
                for o in orphaned:
                    label = order_type_names.get(o["order_type"], f"Type{o['order_type']}")
                    msg += f"  {o['symbol']} {label} @ ${o['trigger_price']:,.2f}\n"

            msg += "\nReply with:\n"
            if positions:
                msg += "  /close BTC — close by symbol\n"
            msg += "  /close all — close all positions + cancel all orders"

            await self.send_message(chat_id, msg)
            return

        # Determine which positions to close
        arg_upper = arg.upper()
        to_close: List[GMXPosition] = []
        also_cancel_orders = False

        if arg_upper == "ALL":
            to_close = positions if positions else []
            also_cancel_orders = True  # also cancel all open orders
        else:
            # Match by symbol
            if positions:
                for pos in positions:
                    if arg_upper in pos.symbol.upper():
                        to_close.append(pos)
            if not to_close:
                await self.send_message(chat_id, f"No position found matching '{arg}'")
                return

        # Show confirmation
        msg = "**Confirm close:**\n\n"
        if to_close:
            for pos in to_close:
                side = "LONG" if pos.is_long else "SHORT"
                msg += f"  {pos.symbol} {side} — ${pos.size_usd:,.2f} — PnL: ${pos.unrealized_pnl:+.2f}\n"
        if also_cancel_orders and orders:
            msg += f"\n  + Cancel {len(orders)} open order(s) (SL/TP/Limit)\n"
        elif also_cancel_orders and not to_close:
            msg += f"  Cancel {len(orders)} open order(s) (SL/TP/Limit)\n"
        msg += "\nSend /confirm to execute or /cancel to abort."

        self.pending_closes[chat_id] = {
            "positions": to_close,
            "also_cancel_orders": also_cancel_orders,
            "created_at": time.time(),
        }
        await self.send_message(chat_id, msg)

    async def handle_close_confirmation(self, chat_id: int, text: str):
        """Handle confirmation reply for pending close."""
        text_upper = text.strip().upper()

        if chat_id not in self.pending_closes:
            return  # No pending close, ignore

        pending = self.pending_closes[chat_id]

        # Expire after 2 minutes
        if time.time() - pending["created_at"] > 120:
            del self.pending_closes[chat_id]
            await self.send_message(chat_id, "Close request expired (2min). Use /close again.")
            return

        if text_upper in ("YES", "Y", "CONFIRM"):
            del self.pending_closes[chat_id]
            positions_to_close = pending["positions"]
            also_cancel_orders = pending.get("also_cancel_orders", False)

            # ── Build initial status message ─────────────────────────────
            if also_cancel_orders and not positions_to_close:
                # /close all with only orphaned orders
                await self.send_message(chat_id, "Closing all open orders...")
            elif also_cancel_orders and positions_to_close:
                # /close all with positions + orders
                await self.send_message(chat_id, "Closing all open positions & orders...")
            elif positions_to_close:
                # /close 1 or /close btc — single position + its SL/TP
                labels = []
                for pos in positions_to_close:
                    side = "LONG" if pos.is_long else "SHORT"
                    labels.append(f"{pos.symbol} {side}")
                await self.send_message(chat_id, f"Closing {', '.join(labels)} & SL/TP...")

            # ── Close positions ────────────────────────────────────────────
            close_failed = False
            if positions_to_close:
                for pos in positions_to_close:
                    side = "LONG" if pos.is_long else "SHORT"
                    pos_acct = getattr(pos, '_wallet_acct', self.account)
                    tx_hash = await self.execute_close(pos, 1.0, acct=pos_acct)
                    if tx_hash:
                        arb_url = f"https://arbiscan.io/tx/{tx_hash}" if not tx_hash.startswith("dry_run") else "DRY RUN"
                        pnl_sign = "+" if pos.unrealized_pnl >= 0 else ""
                        await self.send_message(
                            chat_id,
                            f"{pos.symbol} {side}\n"
                            f"Entry: ${pos.entry_price:,.2f}  |  Current: ${pos.current_price:,.2f}\n"
                            f"PnL: {pnl_sign}${pos.unrealized_pnl:,.2f} ({pnl_sign}{pos.pnl_percentage:.1f}%)\n"
                            f"TX: {tx_hash}\n{arb_url}"
                        )

                        # Record trade for /winrate and /pnl stats
                        self._record_trade(pos, exit_reason="manual")

                        # Wait for the position to actually disappear on-chain before
                        # cancelling SL/TP orders. If we cancel too early (while the
                        # market decrease is still pending), GMX may reject the cancel
                        # because the position still technically exists.
                        if not tx_hash.startswith("dry_run"):
                            closed = await self.wait_for_position_closed(pos.market, pos.is_long, timeout=120, acct=pos_acct)
                            if not closed:
                                await self.send_message(
                                    chat_id,
                                    f"Warning: {pos.symbol} {side} did not close "
                                    "within 2 minutes. Order cancellation may fail."
                                )
                    else:
                        close_failed = True
                        await self.send_message(chat_id, f"FAILED to close {pos.symbol} {side}")

            # ── Cancel open orders ─────────────────────────────────────────
            n_cancelled = 0
            try:
                exchange = self.w3.eth.contract(
                    address=Web3.to_checksum_address(GMX_V2_EXCHANGE_ROUTER),
                    abi=EXCHANGE_ROUTER_ABI,
                )
                if also_cancel_orders:
                    # /close all — cancel every open order on all wallets
                    for _, acct in self._all_wallets():
                        n_cancelled += await asyncio.to_thread(
                            cancel_all_orders,
                            self.w3,
                            acct,
                            exchange,
                            DRY_RUN,
                        )
                elif positions_to_close:
                    # /close btc — cancel orders for those markets on all wallets
                    for pos in positions_to_close:
                        for _, acct in self._all_wallets():
                            n_cancelled += await asyncio.to_thread(
                                cancel_orders_for_market,
                                self.w3,
                                acct,
                                exchange,
                                pos.market,
                                DRY_RUN,
                            )
            except Exception as e:
                self.logger.error(f"Failed to cancel orders: {e}")
                await self.send_message(chat_id, f"Warning: could not cancel orders: {e}")

            # ── Final success message ─────────────────────────────────────
            if close_failed:
                return  # already reported failure above

            if also_cancel_orders and not positions_to_close:
                await self.send_message(chat_id,
                    f"Successfully cancelled {n_cancelled} open order(s).")
            elif also_cancel_orders and positions_to_close:
                await self.send_message(chat_id,
                    "Successfully closed all positions & orders.")
            elif positions_to_close:
                labels = []
                for pos in positions_to_close:
                    side = "LONG" if pos.is_long else "SHORT"
                    labels.append(f"{pos.symbol} {side}")
                await self.send_message(chat_id,
                    f"Successfully closed {', '.join(labels)} & SL/TP.")

            # Top up ETH for gas if balance is low after closing
            if positions_to_close:
                await self.topup_eth_if_needed()
                # Rebalance wallets after manual close
                await self._rebalance_wallets()

        elif text_upper in ("NO", "N", "CANCEL"):
            del self.pending_closes[chat_id]
            await self.send_message(chat_id, "Close cancelled.")

    async def cmd_prices(self, chat_id: int):
        """Show live prices from GMX Reader and Chainlink for all tracked assets."""
        try:
            lines = ["**Live Prices**\n"]

            # ── GMX Reader prices (from open positions) ──
            gmx_prices = {}
            for _, acct in self._all_wallets():
                try:
                    positions = await asyncio.to_thread(
                        chain_fetch_positions, self.w3, acct.address
                    )
                    for cp in positions:
                        sym = cp.symbol.upper().split("/")[0]
                        if cp.current_price and cp.current_price > 0:
                            gmx_prices[sym] = cp.current_price
                except Exception:
                    pass

            # ── Chainlink prices for all tracked feeds ──
            chainlink_prices = {}
            for symbol in CHAINLINK_FEEDS:
                try:
                    price = await asyncio.to_thread(fetch_current_price, symbol, self.w3)
                    if price and price > 0:
                        chainlink_prices[symbol] = price
                except Exception:
                    pass

            # ── Combine and display ──
            all_symbols = sorted(set(list(gmx_prices.keys()) + list(chainlink_prices.keys())))

            if not all_symbols:
                await self.send_message(chat_id, "No prices available.")
                return

            for sym in all_symbols:
                gmx_p = gmx_prices.get(sym)
                cl_p = chainlink_prices.get(sym)

                if gmx_p and cl_p:
                    diff = abs(gmx_p - cl_p) / cl_p * 100
                    diff_str = f" (Δ {diff:.2f}%)" if diff > 0.05 else ""
                    lines.append(
                        f"**{sym}**\n"
                        f"  GMX: ${gmx_p:,.2f} | Chainlink: ${cl_p:,.2f}{diff_str}"
                    )
                elif gmx_p:
                    lines.append(f"**{sym}**\n  GMX: ${gmx_p:,.2f}")
                elif cl_p:
                    lines.append(f"**{sym}**\n  Chainlink: ${cl_p:,.2f}")

            # ── Cached prices (from price_cache) ──
            cached_syms = [s for s in self.price_cache if s not in all_symbols]
            if cached_syms:
                lines.append("\n_Cached:_")
                for sym in sorted(cached_syms):
                    pd = self.price_cache[sym]
                    age = pd.age_seconds
                    age_str = f"{int(age)}s ago" if age < 120 else f"{int(age/60)}m ago"
                    lines.append(f"  {sym}: ${pd.price:,.2f} ({age_str})")

            await self.send_message(chat_id, "\n".join(lines))

        except Exception as e:
            self.logger.error(f"cmd_prices error: {e}")
            await self.send_message(chat_id, f"Error fetching prices: {e}")

    async def cmd_sl(self, chat_id: int, arg: Optional[str]):
        """Move stop loss for a position to entry or a TP level.

        Syntax:
            /sl              → Show positions with SL targets
            /sl 1 entry      → Move position #1 SL to entry price
            /sl 1 tp2        → Move position #1 SL to TP2 price
            /sl 2 tp5        → Move position #2 SL to TP5 price
        """
        try:
            positions, orders = await self._fetch_all_positions_and_orders()

            if not positions:
                await self.send_message(chat_id, "No open positions.")
                return

            # If no args, show positions with SL options
            if not arg or not arg.strip():
                msg = "**Move Stop Loss**\n\nUsage: `/sl <#> <target>`\n\n"
                for i, pos in enumerate(positions, 1):
                    side = "LONG" if pos.is_long else "SHORT"
                    wid_label = f" [W{pos._wallet_id}]" if hasattr(pos, '_wallet_id') else ""

                    # Find current SL
                    market_lower = pos.market.lower()
                    sl_orders = [o for o in orders
                                 if o["market"].lower() == market_lower
                                 and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE]
                    current_sl = f"${sl_orders[0]['trigger_price']:,.2f}" if sl_orders else "None"

                    # Find tracked TPs from internal positions
                    internal_pos = None
                    for p in self.positions.values():
                        if (p.is_open and p.market_addr
                                and p.market_addr.lower() == market_lower
                                and p.side == side):
                            internal_pos = p
                            break

                    targets = ["entry"]
                    if internal_pos and internal_pos.take_profits:
                        sorted_tps = sorted(internal_pos.take_profits,
                                            key=lambda t: t.price,
                                            reverse=(side == "SHORT"))
                        for j, tp in enumerate(sorted_tps, 1):
                            targets.append(f"tp{j} (${tp.price:,.2f})")

                    msg += (
                        f"**#{i} {pos.symbol} {side}{wid_label}**\n"
                        f"  Entry: ${pos.entry_price:,.2f} | SL: {current_sl}\n"
                        f"  Targets: {', '.join(targets)}\n\n"
                    )

                msg += "Example: `/sl 1 entry` or `/sl 1 tp2`"
                await self.send_message(chat_id, msg)
                return

            # Parse: /sl <pos_number> <target>
            parts = arg.strip().split()
            if len(parts) < 2:
                await self.send_message(chat_id, "Usage: `/sl <#> <target>`\nExample: `/sl 1 entry` or `/sl 1 tp2`")
                return

            try:
                pos_num = int(parts[0])
            except ValueError:
                await self.send_message(chat_id, f"Invalid position number: {parts[0]}")
                return

            target = parts[1].lower().strip()

            if pos_num < 1 or pos_num > len(positions):
                await self.send_message(chat_id, f"Position #{pos_num} not found. Use /sl to see available positions.")
                return

            chain_pos = positions[pos_num - 1]
            side = "LONG" if chain_pos.is_long else "SHORT"
            market_lower = chain_pos.market.lower()

            # Find the internal tracked position for TP prices
            internal_pos = None
            for p in self.positions.values():
                if (p.is_open and p.market_addr
                        and p.market_addr.lower() == market_lower
                        and p.side == side):
                    internal_pos = p
                    break

            if not internal_pos:
                await self.send_message(
                    chat_id,
                    f"Position #{pos_num} ({chain_pos.symbol} {side}) not tracked internally. "
                    "Cannot resolve TP prices — try using /addorder to set TP levels first."
                )
                return

            # Resolve target price
            if target == "entry":
                new_sl_price = internal_pos.entry_price
                sl_label = "Entry"
            elif target.startswith("tp"):
                try:
                    tp_num = int(target[2:])
                except ValueError:
                    await self.send_message(chat_id, f"Invalid target: {target}. Use 'entry' or 'tp1'-'tp8'.")
                    return

                sorted_tps = sorted(internal_pos.take_profits,
                                    key=lambda t: t.price,
                                    reverse=(side == "SHORT"))

                if tp_num < 1 or tp_num > len(sorted_tps):
                    await self.send_message(
                        chat_id,
                        f"TP{tp_num} not found. Position has {len(sorted_tps)} TP(s)."
                    )
                    return

                new_sl_price = sorted_tps[tp_num - 1].price
                sl_label = f"TP{tp_num}"
            else:
                await self.send_message(chat_id, f"Invalid target: {target}. Use 'entry' or 'tp1'-'tp8'.")
                return

            # Validate direction: SL should be below current price for LONG, above for SHORT
            current_price = chain_pos.current_price or await self.get_current_price(chain_pos.symbol)
            if current_price:
                if side == "LONG" and new_sl_price >= current_price:
                    await self.send_message(
                        chat_id,
                        f"⚠️ Warning: SL ${new_sl_price:,.2f} is above current price "
                        f"${current_price:,.2f} for a LONG. This would close the position immediately.\n"
                        "Proceeding anyway..."
                    )
                elif side == "SHORT" and new_sl_price <= current_price:
                    await self.send_message(
                        chat_id,
                        f"⚠️ Warning: SL ${new_sl_price:,.2f} is below current price "
                        f"${current_price:,.2f} for a SHORT. This would close the position immediately.\n"
                        "Proceeding anyway..."
                    )

            # Execute the SL move
            await self.notify(
                f"📊 Manual SL move: {internal_pos.symbol} {side} → {sl_label} (${new_sl_price:,.2f})"
            )

            # Fetch fresh orders for this wallet
            pos_acct = self._get_account(internal_pos.wallet_id)
            try:
                fresh_orders = await asyncio.to_thread(
                    fetch_open_orders, self.w3, pos_acct.address
                )
            except Exception as e:
                await self.send_message(chat_id, f"Error fetching orders: {e}")
                return

            await self.move_sl(internal_pos, fresh_orders, new_sl_price, sl_label)

            await self.send_message(
                chat_id,
                f"✅ SL moved for #{pos_num} {internal_pos.symbol} {side} → {sl_label} (${new_sl_price:,.2f})"
            )

        except Exception as e:
            self.logger.error(f"cmd_sl error: {e}\n{traceback.format_exc()}")
            await self.send_message(chat_id, f"Error: {e}")

    async def cmd_balance(self, chat_id: int):
        """Show wallet balances and trade sizing (combined pool)."""
        try:
            total_usdc = 0.0
            total_deployed = 0.0
            wallet_lines = []
            wallet_roles = {1: "swing", 2: "scalp", 3: "scalp", 4: "scalp"}

            for wid, acct in self._all_wallets():
                usdc = await asyncio.to_thread(self._get_portfolio_value_for, acct)

                try:
                    positions = await asyncio.to_thread(
                        chain_fetch_positions, self.w3, acct.address
                    )
                    deployed = sum(p.collateral_amount for p in positions)
                    n_pos = len(positions)
                except Exception:
                    deployed = 0.0
                    n_pos = 0

                total_usdc += usdc
                total_deployed += deployed

                role = wallet_roles.get(wid, "scalp")
                label = f"W{wid} ({role})"
                addr = f"{acct.address[:10]}...{acct.address[-6:]}"
                dep_str = f"${deployed:,.2f}" if deployed > 0 else "$0.00"

                wallet_lines.append(
                    f"**{label}** {addr}\n"
                    f"  USDC: ${usdc:,.2f} | Deployed: {dep_str} | Positions: {n_pos}"
                )

            # Total portfolio = free USDC + deployed collateral + unrealized PnL
            total_pnl = 0.0
            for _, acct in self._all_wallets():
                try:
                    positions = await asyncio.to_thread(
                        chain_fetch_positions, self.w3, acct.address
                    )
                    total_pnl += sum(p.unrealized_pnl for p in positions)
                except Exception:
                    pass

            total_portfolio = total_usdc + total_deployed + total_pnl
            collateral_per_trade = total_portfolio * PORTFOLIO_PCT

            pnl_sign = "+" if total_pnl >= 0 else ""
            msg = (
                "**Wallet Balance**\n\n"
                + "\n".join(wallet_lines)
                + "\n\n**Combined**\n"
                f"Free USDC: ${total_usdc:,.2f}\n"
                f"Deployed: ${total_deployed:,.2f}\n"
                f"Unrealized PnL: {pnl_sign}${total_pnl:,.2f}\n"
                f"**Total Portfolio: ${total_portfolio:,.2f}**\n"
                f"Collateral/trade: ${collateral_per_trade:,.2f} ({PORTFOLIO_PCT:.0%} of ${total_portfolio:,.2f})"
            )
            await self.send_message(chat_id, msg)
        except Exception as e:
            await self.send_message(chat_id, f"Error: {e}")

    async def cmd_winrate(self, chat_id: int, symbol: Optional[str], n: Optional[int]):
        stats = self.calculate_win_rate(symbol, n)
        if "error" in stats:
            label = f" for {symbol}" if symbol else ""
            await self.send_message(chat_id, f"No closed trades recorded{label} yet.\n\nTrades are recorded when you use /close to manually close a position.")
            return
        title = "Win Rate"
        if symbol:
            title += f" — {symbol}"
        if n:
            title += f" (last {n})"
        pf = stats['profit_factor']
        pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"
        msg = (
            f"**{title}**\n\n"
            f"Win Rate: {stats['win_rate']:.1f}% ({stats['wins']}/{stats['total']})\n"
            f"Profit Factor: {pf_str}\n"
            f"Net PnL: ${stats['net_pnl']:.2f}\n"
            f"Avg Win: ${stats['avg_win']:.2f}\n"
            f"Avg Loss: ${stats['avg_loss']:.2f}"
        )
        await self.send_message(chat_id, msg)

    async def cmd_reset(self, chat_id: int):
        """Clear all trade history and PnL stats."""
        count = len(self.trade_history)
        self.trade_history.clear()
        self.health_stats["trades_executed"] = 0
        self.health_stats["signals_processed"] = 0
        self.logger.info(f"Trade history reset: cleared {count} trade(s)")
        await self.send_message(
            chat_id,
            f"Trade history cleared ({count} trade records removed).\n"
            "PnL and win rate stats have been reset to zero."
        )

    async def cmd_gas(self, chat_id: int):
        """Show ETH gas balance for all wallets."""
        try:
            eth_price = await self.get_current_price("ETH")
            if not eth_price:
                try:
                    eth_price = await asyncio.to_thread(close_fetch_current_price, "ETH", self.w3)
                except Exception:
                    eth_price = 0.0

            wallet_roles = {1: "swing", 2: "scalp", 3: "scalp", 4: "scalp"}
            lines = ["**Gas Balances (ETH)**\n"]
            total_eth = 0.0
            low_wallets = []

            for wid, acct in self._all_wallets():
                try:
                    eth_bal = await asyncio.to_thread(self.w3.eth.get_balance, acct.address)
                    eth_amount = eth_bal / 10**18
                    eth_usd = eth_amount * eth_price if eth_price else 0
                    total_eth += eth_amount
                    role = wallet_roles.get(wid, "scalp")
                    status = "⚠️" if eth_usd < 2.0 else "✅"
                    lines.append(
                        f"{status} W{wid} ({role}): {eth_amount:.6f} ETH (${eth_usd:.2f})"
                    )
                    if eth_usd < 2.0:
                        low_wallets.append(f"W{wid}")
                except Exception as e:
                    lines.append(f"❌ W{wid}: error ({e})")

            total_usd = total_eth * eth_price if eth_price else 0
            lines.append(f"\nTotal: {total_eth:.6f} ETH (${total_usd:.2f})")

            if low_wallets:
                lines.append(f"\n⚠️ Low gas: {', '.join(low_wallets)} — use /topup to refill")

            await self.send_message(chat_id, "\n".join(lines))
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching gas balances: {e}")

    async def cmd_tradesize(self, chat_id: int, arg: Optional[str] = None):
        """Show or adjust the trade size percentage (PORTFOLIO_PCT).

        Usage:
            /tradesize          — show current trade size and what it means in $
            /tradesize 15       — set to 15% of portfolio per trade
            /tradesize 0.15     — same thing (accepts decimal)
        """
        global PORTFOLIO_PCT
        try:
            if not arg or not arg.strip():
                # Show current trade size
                total_portfolio = await self._get_total_portfolio_value()
                collateral = total_portfolio * PORTFOLIO_PCT
                pct_display = PORTFOLIO_PCT * 100

                msg = (
                    "**Trade Size**\n\n"
                    f"Portfolio: ${total_portfolio:,.2f}\n"
                    f"Trade size: {pct_display:.0f}% → ${collateral:,.2f} collateral per trade\n\n"
                    "Usage: `/tradesize <percent>` to change\n"
                    "Example: `/tradesize 15` → 15% per trade"
                )
                await self.send_message(chat_id, msg)
                return

            # Parse new percentage
            new_val = float(arg.strip().replace("%", ""))

            # Accept both "20" (percent) and "0.20" (decimal)
            if new_val > 1.0:
                new_pct = new_val / 100.0
            else:
                new_pct = new_val

            if new_pct < 0.01 or new_pct > 0.50:
                await self.send_message(chat_id, "Trade size must be between 1% and 50%.")
                return

            old_pct = PORTFOLIO_PCT
            PORTFOLIO_PCT = new_pct

            total_portfolio = await self._get_total_portfolio_value()
            new_collateral = total_portfolio * PORTFOLIO_PCT

            await self.send_message(
                chat_id,
                f"✅ Trade size updated: {old_pct*100:.0f}% → {new_pct*100:.0f}%\n"
                f"New collateral per trade: ${new_collateral:,.2f} "
                f"(of ${total_portfolio:,.2f} portfolio)"
            )
            self.logger.info(f"PORTFOLIO_PCT changed: {old_pct} → {new_pct}")

        except ValueError:
            await self.send_message(chat_id, f"Invalid value: {arg}\nUsage: `/tradesize 20` for 20%")
        except Exception as e:
            await self.send_message(chat_id, f"Error: {e}")

    async def cmd_balance_wallets(self, chat_id: int):
        """Manually trigger wallet rebalance and report results."""
        wallets = self._all_wallets()
        if len(wallets) < 2:
            await self.send_message(chat_id, "Single wallet mode — nothing to rebalance.")
            return

        # Show current state
        before_bals = {}
        for wid, acct in wallets:
            before_bals[wid] = await asyncio.to_thread(self._get_portfolio_value_for, acct)

        before_str = "\n".join(f"  W{wid}: ${bal:,.2f}" for wid, bal in before_bals.items())
        diff = max(before_bals.values()) - min(before_bals.values())

        await self.send_message(
            chat_id,
            f"Before:\n{before_str}\n  Spread: ${diff:,.2f}\n\nRebalancing..."
        )

        await self._rebalance_wallets()

        # Show after state
        after_bals = {}
        for wid, acct in wallets:
            after_bals[wid] = await asyncio.to_thread(self._get_portfolio_value_for, acct)

        after_str = "\n".join(f"  W{wid}: ${bal:,.2f}" for wid, bal in after_bals.items())
        new_diff = max(after_bals.values()) - min(after_bals.values())

        if new_diff < diff:
            await self.send_message(
                chat_id,
                f"After:\n{after_str}\n  Spread: ${new_diff:,.2f}\n\n✅ Wallets rebalanced"
            )
        elif diff < 1.0:
            await self.send_message(
                chat_id,
                f"Wallets already balanced (spread ${diff:,.2f} < $1.00)"
            )
        else:
            await self.send_message(
                chat_id,
                f"After:\n{after_str}\n\n⚠️ Rebalance may have failed — check logs"
            )

    async def cmd_lastmsg(self, chat_id: int):
        """Fetch and display the last message from each monitored channel."""
        if not self.resolved_channels:
            await self.send_message(chat_id, "No channels resolved.")
            return

        for ch_id, ch_name in self.resolved_channels.items():
            try:
                msgs = await self.client.get_messages(ch_id, limit=1)
                if msgs and msgs[0]:
                    m = msgs[0]
                    preview = (m.text or "(no text)")[:200]
                    await self.send_message(
                        chat_id,
                        f"📡 **{ch_name}**\n"
                        f"ID: {m.id} | {m.date.strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
                        f"{preview}"
                    )
                else:
                    await self.send_message(chat_id, f"📡 **{ch_name}** — no messages found")
            except Exception as e:
                await self.send_message(chat_id, f"📡 **{ch_name}** — error: {e}")

    async def cmd_increase(self, chat_id: int, amount_str: str = None):
        """Show open positions across all wallets and let user pick one to increase.
        Usage: /increase [amount]  — amount is USDC collateral to add (optional)."""
        # Fetch on-chain positions from all wallets
        all_positions = []

        for wid, acct in self._all_wallets():
            try:
                chain_positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                for cp in chain_positions:
                    all_positions.append((wid, acct, cp))
            except Exception as e:
                self.logger.warning(f"Failed to fetch positions for W{wid}: {e}")

        if not all_positions:
            await self.send_message(chat_id, "No open positions found across wallets.")
            return

        msg = "**Select position to increase:**\n\n"
        for i, (wid, acct, cp) in enumerate(all_positions, 1):
            side = "LONG" if cp.is_long else "SHORT"
            pnl_sign = "+" if cp.unrealized_pnl >= 0 else ""
            msg += (
                f"**{i}.** {cp.symbol} {side} [W{wid}]\n"
                f"   Size: ${cp.size_usd:,.2f} @ {cp.leverage:.1f}x\n"
                f"   Entry: ${cp.entry_price:,.2f}  |  Current: ${cp.current_price:,.2f}\n"
                f"   Collateral: ${cp.collateral_amount:,.2f}\n"
                f"   PnL: {pnl_sign}${cp.unrealized_pnl:,.2f} ({pnl_sign}{cp.pnl_percentage:.1f}%)\n\n"
            )

        if amount_str:
            msg += f"Amount: ${amount_str} USDC\nReply with position number (1-{len(all_positions)})"
        else:
            msg += "Reply with: <number> <amount>\nExample: 1 25  (adds $25 USDC to position 1)"

        self.pending_increase[chat_id] = {
            "positions": all_positions,
            "amount": float(amount_str) if amount_str else None,
            "created_at": time.time(),
        }
        await self.send_message(chat_id, msg)

    async def handle_increase_reply(self, chat_id: int, text: str):
        """Handle user's reply to /increase — pick position and optional amount."""
        pending = self.pending_increase.get(chat_id)
        if not pending:
            return

        # Expire after 2 minutes
        if time.time() - pending["created_at"] > 120:
            del self.pending_increase[chat_id]
            await self.send_message(chat_id, "Increase request expired (2min). Use /increase again.")
            return

        text = text.strip().upper()
        if text in ("CANCEL", "NO", "N"):
            del self.pending_increase[chat_id]
            await self.send_message(chat_id, "Increase cancelled.")
            return

        parts = text.split()
        try:
            idx = int(parts[0]) - 1
        except (ValueError, IndexError):
            await self.send_message(chat_id, "Reply with position number (e.g. 1) or 'cancel'")
            return

        positions = pending["positions"]
        if idx < 0 or idx >= len(positions):
            await self.send_message(chat_id, f"Invalid. Pick 1-{len(positions)}")
            return

        # Get amount — from arg, or from reply
        amount = pending.get("amount")
        if amount is None:
            try:
                amount = float(parts[1])
            except (ValueError, IndexError):
                await self.send_message(chat_id, "Include amount: e.g. '1 25' to add $25 to position 1")
                return

        if amount <= 0:
            await self.send_message(chat_id, "Amount must be positive.")
            del self.pending_increase[chat_id]
            return

        wid, acct, cp = positions[idx]
        side = "LONG" if cp.is_long else "SHORT"
        del self.pending_increase[chat_id]

        # Calculate proportional size increase to maintain current leverage
        additional_size = amount * cp.leverage

        await self.send_message(
            chat_id,
            f"Increasing {cp.symbol} {side} [W{wid}]\n"
            f"Adding: ${amount:.2f} collateral → ${additional_size:.2f} size @ {cp.leverage:.1f}x\n"
            "Executing..."
        )

        try:
            # Get current price for acceptable_price calculation
            current_price = await asyncio.to_thread(fetch_current_price, cp.symbol, self.w3)

            exchange = self.w3.eth.contract(
                address=Web3.to_checksum_address(GMX_V2_EXCHANGE_ROUTER),
                abi=EXCHANGE_ROUTER_ABI,
            )
            wallet = Web3.to_checksum_address(acct.address)
            market = Web3.to_checksum_address(cp.market)
            collateral_token = Web3.to_checksum_address(GMX_V2_COLLATERAL_TOKEN)
            order_vault = Web3.to_checksum_address(GMX_V2_ORDER_VAULT)

            txh = await asyncio.to_thread(
                create_market_increase_order,
                self.w3,
                acct,
                exchange,
                wallet,
                market,
                collateral_token,
                order_vault,
                additional_size,
                amount,
                current_price,
                cp.symbol,
                cp.is_long,
                SLIPPAGE_BPS,
                EXECUTION_FEE_WEI,
                DRY_RUN,
            )

            await self.send_message(
                chat_id,
                f"✅ {cp.symbol} {side} increased\n"
                f"Added ${amount:.2f} collateral (${additional_size:.2f} size)\n"
                f"TX: {txh}"
            )

            # Update internal position tracking if we have one
            for pos in self.positions.values():
                if (pos.is_open and pos.market_addr
                        and pos.market_addr.lower() == cp.market.lower()
                        and pos.wallet_id == wid
                        and pos.side == side):
                    pos.size_usd += additional_size
                    break

        except Exception as e:
            self.logger.error(f"Increase failed: {e}\n{traceback.format_exc()}")
            await self.send_message(chat_id, f"Failed to increase position: {e}")

    async def cmd_cancelorder(self, chat_id: int, arg: Optional[str]):
        """Cancel individual on-chain orders (SL, TP, Limit) by number.

        /cancelorder          — list all orders numbered
        /cancelorder 3        — cancel order #3
        /cancelorder 1,3,5    — cancel multiple orders
        /cancelorder all      — cancel every cancellable order
        """
        await self.send_message(chat_id, "Fetching open orders...")
        try:
            _positions, all_orders = await self._fetch_all_positions_and_orders()
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching orders: {e}")
            return

        ORDER_TYPE_NAMES = {
            2: "MarketInc", 3: "LimitInc", 4: "MarketDec",
            5: "TP", 6: "SL",
        }
        CANCELLABLE = {3, 5, 6}  # LimitIncrease, LimitDecrease(TP), StopLossDecrease

        if not all_orders:
            await self.send_message(chat_id, "No open orders on-chain.")
            return

        # Build numbered list
        numbered = []
        for o in all_orders:
            o_type = o["order_type"]
            cancellable = o_type in CANCELLABLE and o.get("key_hex")
            numbered.append({**o, "_cancellable": cancellable})

        if arg is None:
            # Display all orders numbered
            msg = "**Open Orders**\n\n"
            for i, o in enumerate(numbered, 1):
                label = ORDER_TYPE_NAMES.get(o["order_type"], f"Type{o['order_type']}")
                side = "LONG" if o["is_long"] else "SHORT"
                cancel_mark = "" if o["_cancellable"] else " (not cancellable)"

                # Wallet label
                wid = ""
                if o.get("_wallet_id"):
                    wid = f" [W{o['_wallet_id']}]"
                elif o.get("_wallet_acct"):
                    for wid_check, acct_check in self._all_wallets():
                        if o["_wallet_acct"].address == acct_check.address:
                            wid = f" [W{wid_check}]"
                            break

                msg += (
                    f"**#{i}** {o['symbol']} {side} — {label} "
                    f"@ ${o['trigger_price']:,.2f}  "
                    f"(${o['size_usd']:,.2f}){wid}{cancel_mark}\n"
                )

            msg += "\nReply with:\n"
            msg += "  /cancelorder 3    — cancel order #3\n"
            msg += "  /cancelorder 1,3  — cancel multiple\n"
            msg += "  /cancelorder all  — cancel all"
            await self.send_message(chat_id, msg)
            return

        # Parse which orders to cancel
        arg_stripped = arg.strip().upper()
        if arg_stripped == "ALL":
            indices = [i for i, o in enumerate(numbered) if o["_cancellable"]]
        else:
            try:
                indices = []
                for part in arg.replace(" ", ",").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    num = int(part)
                    if num < 1 or num > len(numbered):
                        await self.send_message(chat_id, f"Order #{num} out of range (1-{len(numbered)})")
                        return
                    indices.append(num - 1)  # 0-indexed
            except ValueError:
                await self.send_message(chat_id, "Usage: /cancelorder 3  or  /cancelorder 1,3,5  or  /cancelorder all")
                return

        if not indices:
            await self.send_message(chat_id, "No cancellable orders selected.")
            return

        # Verify all selected are cancellable
        for idx in indices:
            o = numbered[idx]
            if not o["_cancellable"]:
                label = ORDER_TYPE_NAMES.get(o["order_type"], f"Type{o['order_type']}")
                await self.send_message(
                    chat_id,
                    f"Order #{idx+1} ({o['symbol']} {label}) cannot be cancelled "
                    "(market orders execute immediately)."
                )
                return

        # Execute cancellations
        exchange = self.w3.eth.contract(
            address=Web3.to_checksum_address(GMX_V2_EXCHANGE_ROUTER),
            abi=EXCHANGE_ROUTER_ABI,
        )

        cancelled = 0
        failed = 0
        for idx in indices:
            o = numbered[idx]
            label = ORDER_TYPE_NAMES.get(o["order_type"], f"Type{o['order_type']}")
            key_bytes = bytes.fromhex(o["key_hex"])
            acct = o.get("_wallet_acct", self.account)
            wallet = Web3.to_checksum_address(acct.address)

            self.logger.info(
                f"cancelorder: cancelling #{idx+1} {o['symbol']} {label} "
                f"key=0x{o['key_hex'][:16]}..."
            )

            if DRY_RUN:
                self.logger.info(f"  [DRY_RUN] Would cancel order #{idx+1}")
                cancelled += 1
                continue

            try:
                data = exchange.encode_abi("cancelOrder", [key_bytes])
                tx = _open_mod.build_tx(self.w3, wallet, exchange.address, data, value=0)
                txh = _open_mod.sign_send(self.w3, acct, tx, dry_run=False)
                receipt = _open_mod.wait_receipt(self.w3, txh)
                if receipt.get("status") == 1:
                    self.logger.info(f"  Cancelled #{idx+1}: {txh}")
                    cancelled += 1
                else:
                    self.logger.warning(f"  Cancel tx reverted for #{idx+1}: {txh}")
                    failed += 1
            except Exception as e:
                self.logger.warning(f"  Failed to cancel #{idx+1}: {e}")
                failed += 1

        # Summary
        parts = []
        if cancelled:
            parts.append(f"{cancelled} cancelled")
        if failed:
            parts.append(f"{failed} failed")
        summary = ", ".join(parts)

        detail_lines = []
        for idx in indices:
            o = numbered[idx]
            label = ORDER_TYPE_NAMES.get(o["order_type"], f"Type{o['order_type']}")
            detail_lines.append(f"  #{idx+1} {o['symbol']} {label} @ ${o['trigger_price']:,.2f}")

        msg = f"**Cancel Orders: {summary}**\n" + "\n".join(detail_lines)
        await self.send_message(chat_id, msg)

    async def cmd_addorder(self, chat_id: int, arg: Optional[str]):
        """Manually add a SL or TP order to an open position.

        /addorder                      — list positions to pick from
        /addorder sl <#> <price>       — add SL on position # at price
        /addorder tp <#> <price> [%]   — add TP on position # at price
                                         % is optional — auto-calculated from remaining size after existing TPs
        """
        # Fetch positions + existing orders
        await self.send_message(chat_id, "Fetching positions...")
        try:
            positions, orders = await self._fetch_all_positions_and_orders()
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching positions: {e}")
            return

        if not positions:
            await self.send_message(chat_id, "No open positions on-chain.")
            return

        if arg is None:
            # Show positions and usage
            msg = "**Open Positions**\n\n"
            for i, pos in enumerate(positions, 1):
                side = "LONG" if pos.is_long else "SHORT"
                wid = ""
                if hasattr(pos, '_wallet_id'):
                    wid = f" [W{pos._wallet_id}]"
                elif hasattr(pos, '_wallet_acct'):
                    for wid_check, acct_check in self._all_wallets():
                        if pos._wallet_acct.address == acct_check.address:
                            wid = f" [W{wid_check}]"
                            break

                # Show existing SL/TP for this position
                pos_orders = [o for o in orders if o["market"].lower() == pos.market.lower()]
                sl_orders = [o for o in pos_orders if o["order_type"] == 6]
                tp_orders = sorted([o for o in pos_orders if o["order_type"] == 5],
                                   key=lambda o: o["trigger_price"])

                msg += (
                    f"**#{i} {pos.symbol} {side}{wid}**\n"
                    f"  Size: ${pos.size_usd:,.2f} @ {pos.leverage:.1f}x\n"
                    f"  Entry: ${pos.entry_price:,.2f}  |  Current: ${pos.current_price:,.2f}\n"
                )
                if sl_orders:
                    for o in sl_orders:
                        msg += f"  SL @ ${o['trigger_price']:,.2f}\n"
                if tp_orders:
                    for j, o in enumerate(tp_orders, 1):
                        msg += f"  TP{j} @ ${o['trigger_price']:,.2f}  (${o['size_usd']:,.2f})\n"

                # Show remaining size available for new TPs
                existing_tp_size = sum(o["size_usd"] for o in tp_orders)
                remaining = max(0, pos.size_usd - existing_tp_size)
                remaining_pct = (remaining / pos.size_usd * 100) if pos.size_usd > 0 else 0
                if tp_orders:
                    msg += f"  Remaining: ${remaining:,.2f} ({remaining_pct:.0f}%)\n"
                msg += "\n"

            msg += "**Usage:**\n"
            msg += "  /addorder sl 1 95000              — add SL on #1\n"
            msg += "  /addorder tp 1 100000             — single TP (uses all remaining size)\n"
            msg += "  /addorder tp 1 98000 100000 103000 — multiple TPs (split remaining evenly)"
            await self.send_message(chat_id, msg)
            return

        # Parse args: sl/tp <pos#> <price(s)>
        parts = arg.strip().split()
        if len(parts) < 3:
            await self.send_message(
                chat_id,
                "Usage:\n"
                "  /addorder sl <#> <price>\n"
                "  /addorder tp <#> <price>           — single TP (auto-size)\n"
                "  /addorder tp <#> <p1> <p2> <p3>    — multiple TPs (split evenly)"
            )
            return

        order_kind = parts[0].upper()
        if order_kind not in ("SL", "TP"):
            await self.send_message(chat_id, "First argument must be 'sl' or 'tp'.")
            return

        try:
            pos_num = int(parts[1])
        except ValueError:
            await self.send_message(chat_id, "Position number must be an integer.")
            return
        if pos_num < 1 or pos_num > len(positions):
            await self.send_message(chat_id, f"Position #{pos_num} out of range (1-{len(positions)}).")
            return

        pos = positions[pos_num - 1]
        acct = getattr(pos, '_wallet_acct', self.account)
        side = "LONG" if pos.is_long else "SHORT"

        # Build contract references
        exchange = self.w3.eth.contract(
            address=Web3.to_checksum_address(GMX_V2_EXCHANGE_ROUTER),
            abi=EXCHANGE_ROUTER_ABI,
        )
        order_vault = Web3.to_checksum_address(GMX_V2_ORDER_VAULT)
        collateral_token = Web3.to_checksum_address(GMX_V2_COLLATERAL_TOKEN)

        # ── SL ────────────────────────────────────────────────────────────
        if order_kind == "SL":
            try:
                price = float(parts[2].replace(",", ""))
            except ValueError:
                await self.send_message(chat_id, "Price must be a number.")
                return
            if price <= 0:
                await self.send_message(chat_id, "Price must be positive.")
                return

            # Sanity check
            if pos.is_long and price >= pos.entry_price:
                await self.send_message(
                    chat_id,
                    f"Warning: SL ${price:,.2f} is above entry ${pos.entry_price:,.2f} for a LONG. "
                    "Send the command again to confirm, or use a lower price."
                )
                return
            elif not pos.is_long and price <= pos.entry_price:
                await self.send_message(
                    chat_id,
                    f"Warning: SL ${price:,.2f} is below entry ${pos.entry_price:,.2f} for a SHORT. "
                    "Send the command again to confirm, or use a higher price."
                )
                return

            try:
                self.logger.info(
                    f"addorder: placing SL on {pos.symbol} {side} at ${price:,.2f} "
                    f"size=${pos.size_usd:,.2f}"
                )
                txh = await asyncio.to_thread(
                    create_sl_order,
                    self.w3, acct, exchange, acct.address,
                    pos.market, collateral_token, order_vault,
                    price, pos.size_usd, pos.symbol, pos.is_long,
                    SLIPPAGE_BPS, EXECUTION_FEE_WEI, DRY_RUN,
                )
                await self.send_message(
                    chat_id,
                    f"SL placed on {pos.symbol} {side} @ ${price:,.2f}\n"
                    f"Size: ${pos.size_usd:,.2f} (100% close)\nTX: {txh}"
                )
            except Exception as e:
                self.logger.error(f"addorder SL failed: {e}\n{traceback.format_exc()}")
                await self.send_message(chat_id, f"Failed to place SL: {e}")
            return

        # ── TP (single or multiple) ───────────────────────────────────────
        # Parse all price arguments after position number
        price_strs = parts[2:]
        tp_prices = []
        for ps in price_strs:
            try:
                p = float(ps.replace(",", ""))
                if p <= 0:
                    await self.send_message(chat_id, f"Price must be positive, got: {ps}")
                    return
                tp_prices.append(p)
            except ValueError:
                await self.send_message(chat_id, f"Invalid price: '{ps}'")
                return

        if not tp_prices:
            await self.send_message(chat_id, "Provide at least one TP price.")
            return

        # Calculate remaining size after existing TPs
        pos_tp_orders = [
            o for o in orders
            if o["market"].lower() == pos.market.lower() and o["order_type"] == 5
        ]
        existing_tp_size = sum(o["size_usd"] for o in pos_tp_orders)
        remaining_size = max(0, pos.size_usd - existing_tp_size)

        if remaining_size <= 0:
            await self.send_message(
                chat_id,
                "Existing TPs already cover the full position size "
                f"(${existing_tp_size:,.2f} / ${pos.size_usd:,.2f}).\n"
                "Cancel a TP first with /cancelorder."
            )
            return

        # ── Weighted distribution: ascending weights (smaller early, bigger later) ──
        # Weight scheme: TP1=1, TP2=2, TP3=3, etc.
        # Total TPs = existing + new, new TPs get the higher-numbered weights.
        num_existing = len(pos_tp_orders)
        num_new = len(tp_prices)
        total_tps = num_existing + num_new

        # Weights for the new TPs only (they occupy the higher slots)
        new_weights = [i for i in range(num_existing + 1, total_tps + 1)]
        weight_sum = sum(new_weights)

        # Each new TP's share of the remaining size, proportional to its weight
        tp_allocations = []  # (price, size_usd, pct_of_position)
        for i, tp_price in enumerate(sorted(tp_prices)):
            tp_size = remaining_size * (new_weights[i] / weight_sum)
            tp_pct = tp_size / pos.size_usd  # fraction 0-1
            tp_allocations.append((tp_price, tp_size, tp_pct))

        self.logger.info(
            f"addorder: placing {num_new} TP(s) on {pos.symbol} {side}, "
            f"remaining=${remaining_size:,.2f} ({remaining_size/pos.size_usd*100:.0f}%), "
            f"total TPs={total_tps}, weights={new_weights}"
        )
        for tp_price, tp_size, tp_pct in tp_allocations:
            self.logger.info(
                f"  TP @ ${tp_price:,.2f}: ${tp_size:,.2f} ({tp_pct*100:.0f}%)"
            )

        results = []
        for tp_price, tp_size, tp_pct in tp_allocations:
            tp = TakeProfit(price=tp_price, close_pct=tp_pct)
            try:
                txh = await asyncio.to_thread(
                    create_tp_order,
                    self.w3, acct, exchange, acct.address,
                    pos.market, collateral_token, order_vault,
                    tp, pos.size_usd, pos.symbol, pos.is_long,
                    SLIPPAGE_BPS, EXECUTION_FEE_WEI, DRY_RUN,
                )
                results.append((tp_price, tp_size, tp_pct * 100, txh, None))
            except Exception as e:
                self.logger.error(f"addorder TP @ ${tp_price:,.2f} failed: {e}")
                results.append((tp_price, tp_size, tp_pct * 100, None, str(e)))

        # Build summary message
        ok = [r for r in results if r[3]]
        fail = [r for r in results if r[4]]

        msg = f"**Add TP — {pos.symbol} {side}**\n\n"
        for tp_price, size, pct, txh, err in results:
            if txh:
                msg += f"  TP @ ${tp_price:,.2f} — ${size:,.2f} ({pct:.0f}%) — {txh}\n"
            else:
                msg += f"  TP @ ${tp_price:,.2f} — FAILED: {err}\n"

        if ok and not fail:
            msg += f"\n{len(ok)} TP(s) placed successfully."
        elif ok and fail:
            msg += f"\n{len(ok)} placed, {len(fail)} failed."
        else:
            msg += "\nAll TPs failed."

        await self.send_message(chat_id, msg)

    async def cmd_health(self, chat_id: int):
        h = self.get_health_report()
        msg = (
            "**System Health**\n\n"
            f"Status: {h['status']}\n"
            f"Uptime: {h['uptime_hours']:.1f}h\n"
            f"Heartbeat: {time.time() - self.last_heartbeat:.0f}s ago\n"
            f"Positions: {h['open_positions']}\n"
            f"Price updates: {h['price_updates']}\n"
            f"Signals: {h['signals']}\n"
            f"Trades: {h['trades']}\n"
            f"Errors: {h['errors']}"
        )
        await self.send_message(chat_id, msg)

    async def cmd_pnl(self, chat_id: int):
        """Show PnL breakdown (today / 30d / all time) for BTC, SOL, ETH only."""
        PNL_SYMBOLS = {"BTC", "SOL", "ETH"}
        now = time.time()
        today_cutoff = now - 86400          # last 24 hours
        month_cutoff = now - 30 * 86400     # last 30 days

        def pnl_stats(trades):
            if not trades:
                return {"pnl": 0.0, "trades": 0, "wins": 0}
            pnl = sum(t.pnl_usd for t in trades)
            wins = sum(1 for t in trades if t.pnl_usd > 0)
            return {"pnl": pnl, "trades": len(trades), "wins": wins}

        def format_section(label: str, symbol_stats: dict) -> str:
            lines = [f"**{label}**"]
            total = 0.0
            for sym in ("BTC", "ETH", "SOL"):
                s = symbol_stats.get(sym, {"pnl": 0.0, "trades": 0, "wins": 0})
                sign = "+" if s["pnl"] >= 0 else ""
                wr = f"{s['wins']}/{s['trades']}" if s["trades"] else "—"
                lines.append(f"  {sym}: {sign}${s['pnl']:,.2f}  ({wr})")
                total += s["pnl"]
            sign = "+" if total >= 0 else ""
            lines.append(f"  Total: {sign}${total:,.2f}")
            return "\n".join(lines)

        # Build realized stats per period per symbol from trade_history
        relevant = [t for t in self.trade_history if t.symbol in PNL_SYMBOLS]

        today_stats = {
            sym: pnl_stats([t for t in relevant if t.symbol == sym and t.closed_at >= today_cutoff])
            for sym in PNL_SYMBOLS
        }
        month_stats = {
            sym: pnl_stats([t for t in relevant if t.symbol == sym and t.closed_at >= month_cutoff])
            for sym in PNL_SYMBOLS
        }
        alltime_stats = {
            sym: pnl_stats([t for t in relevant if t.symbol == sym])
            for sym in PNL_SYMBOLS
        }

        # Fetch live on-chain positions for unrealized PnL
        open_pnl: dict = {sym: 0.0 for sym in PNL_SYMBOLS}
        try:
            chain_positions = []
            for _, acct in self._all_wallets():
                cps = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                chain_positions.extend(cps)
            for cp in chain_positions:
                sym = cp.symbol.upper().split("/")[0]
                if sym in PNL_SYMBOLS:
                    open_pnl[sym] = open_pnl.get(sym, 0.0) + cp.unrealized_pnl
        except Exception as e:
            self.logger.warning(f"/pnl: could not fetch chain positions for unrealized PnL: {e}")

        open_lines = ["**Open (Unrealized)**"]
        open_total = 0.0
        for sym in ("BTC", "ETH", "SOL"):
            pnl = open_pnl.get(sym, 0.0)
            sign = "+" if pnl >= 0 else ""
            open_lines.append(f"  {sym}: {sign}${pnl:,.2f}")
            open_total += pnl
        sign = "+" if open_total >= 0 else ""
        open_lines.append(f"  Total: {sign}${open_total:,.2f}")

        if not relevant:
            closed_section = "No closed trades recorded yet.\nTrades are saved when you use /close."
        else:
            closed_section = (
                format_section("Today (24h)", today_stats) + "\n\n"
                + format_section("30 Days", month_stats) + "\n\n"
                + format_section("All Time", alltime_stats)
            )

        msg = (
            "**PnL Summary — BTC / ETH / SOL**\n\n"
            + closed_section + "\n\n"
            + "\n".join(open_lines)
        )
        await self.send_message(chat_id, msg)

    # ──────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────
    def _record_trade(self, pos_obj, exit_reason: str = "manual"):
        """Record a closed trade into trade_history for /winrate and /pnl stats.

        Accepts either a GMXPosition (from close.py) or an internal Position.
        """
        closed_at = time.time()

        # ── Normalise fields across GMXPosition vs internal Position ──
        if hasattr(pos_obj, "is_long"):
            # GMXPosition (from close.py)
            symbol = pos_obj.symbol
            side = "LONG" if pos_obj.is_long else "SHORT"
            entry_price = pos_obj.entry_price
            exit_price = pos_obj.current_price
            size_usd = pos_obj.size_usd
            leverage = pos_obj.leverage
            pnl_usd = pos_obj.unrealized_pnl
            collateral = getattr(pos_obj, "collateral_amount", 0.0)
            opened_at = closed_at - 3600  # GMXPosition has no open timestamp
        else:
            # Internal Position dataclass
            symbol = pos_obj.symbol
            side = pos_obj.side
            entry_price = pos_obj.entry_price
            exit_price = pos_obj.current_price
            size_usd = pos_obj.size_usd
            leverage = pos_obj.leverage
            pnl_usd = pos_obj.unrealized_pnl
            collateral = size_usd / leverage if leverage else 0.0
            opened_at = getattr(pos_obj, "opened_at", closed_at - 3600)

        pnl_pct = (pnl_usd / collateral * 100) if collateral else 0.0

        record = TradeRecord(
            id=str(uuid.uuid4()),
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            size_usd=size_usd,
            leverage=leverage,
            duration_hours=(closed_at - opened_at) / 3600,
            pnl_usd=pnl_usd,
            pnl_percentage=pnl_pct,
            exit_reason=exit_reason,
            opened_at=opened_at,
            closed_at=closed_at,
        )
        self.trade_history.append(record)
        self.logger.info(
            f"Recorded trade: {symbol} {side} "
            f"PnL=${pnl_usd:+.2f} ({pnl_pct:+.1f}%)"
        )

    def get_health_report(self) -> Dict[str, Any]:
        uptime = time.time() - self.health_stats["uptime_start"]
        open_pos = sum(1 for p in self.positions.values() if p.is_open)
        exposure = sum(p.size_usd for p in self.positions.values() if p.is_open)
        return {
            "status": "HALTED" if self.is_halted else "ACTIVE",
            "uptime_hours": uptime / 3600,
            "open_positions": open_pos,
            "total_exposure": exposure,
            "price_updates": self.health_stats["price_updates"],
            "signals": self.health_stats["signals_processed"],
            "trades": self.health_stats["trades_executed"],
            "errors": self.health_stats["errors"],
        }

    async def notify(self, message: str):
        if NOTIFY_CHAT and self.client:
            try:
                await self.client.send_message(NOTIFY_CHAT, message)
            except Exception as e:
                self.logger.error(f"Notify failed: {e}")

    async def send_message(self, chat_id: int, message: str):
        try:
            await self.client.send_message(chat_id, message)
        except Exception as e:
            self.logger.error(f"Send message failed: {e}")

    async def notify_position_opened(self, position: Position, order_type: str = "market"):
        tp_lines = ""
        for i, tp in enumerate(position.take_profits):
            tp_lines += f"  TP{i+1}: ${tp.price:,.0f} ({tp.percentage:.0%})\n"
        order_label = "LIMIT ORDER" if order_type == "limit" else "MARKET ORDER"

        msg = (
            f"**Position Opened ({order_label})**\n\n"
            f"{position.symbol} {position.side}\n"
            f"Size: ${position.size_usd:.0f} @ {position.leverage:.0f}x\n"
            f"Entry: ${position.entry_price:,.0f}\n"
            f"SL: ${position.stop_loss:,.0f}\n"
            f"{tp_lines}"
            f"TX: {position.tx_hash}"
        )
        if order_type == "limit":
            msg += "\n\nLimit order placed — waiting for price to reach entry."
        await self.notify(msg)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def main():
    print("=" * 60)
    print("  GMX V2 Telegram Trading Bot")
    print("=" * 60)
    print(f"  Network:  {NETWORK.upper()}")
    print(f"  Mode:     {'DRY RUN' if DRY_RUN else 'LIVE TRADING'}")
    print(f"  Market:   {GMX_V2_MARKET[:10]}..." if GMX_V2_MARKET else "  Market:   NOT SET")
    print(f"  Channels: {TELEGRAM_CHANNELS}")
    print(f"  Admin:    {ADMIN_CHAT}")
    print("=" * 60)

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        print("Missing Telegram credentials. Set TELEGRAM_API_ID and TELEGRAM_API_HASH.")
        return

    if not TELEGRAM_CHANNELS:
        print("No channels configured. Set TELEGRAM_CHANNELS.")
        return

    if not GMX_V2_EXCHANGE_ROUTER or not GMX_V2_ORDER_VAULT:
        print("Missing GMX V2 addresses. Set GMX_V2_EXCHANGE_ROUTER and GMX_V2_ORDER_VAULT.")
        return

    bot = GMXBot()
    try:
        await bot.start()
    except Exception as e:
        # Attempt a last-resort offline notification if start() itself raises
        # before shutdown() was called inside start()
        try:
            if bot.client and bot.client.is_connected():
                await bot.notify("🔴 Bot Offline")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
