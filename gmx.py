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
import time
import uuid
import asyncio
import logging
import importlib
import statistics
import traceback
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
# "open" is a Python builtin, so we use importlib to import the module
_open_mod = importlib.import_module("open")

# From open.py — signal parsing & execution
parse_signal = _open_mod.parse_signal
Signal = _open_mod.Signal
TakeProfit = _open_mod.TakeProfit
execute_signal = _open_mod.execute_signal
create_limit_increase_order = _open_mod.create_limit_increase_order
create_sl_order = _open_mod.create_sl_order
fetch_current_price = _open_mod.fetch_current_price
cancel_orders_for_market = _open_mod.cancel_orders_for_market
cancel_all_orders = _open_mod.cancel_all_orders
fetch_open_orders = _open_mod.fetch_open_orders
scale_price = _open_mod.scale_price
COINGECKO_IDS = _open_mod.COINGECKO_IDS
INDEX_TOKEN_DECIMALS = _open_mod.INDEX_TOKEN_DECIMALS
ERC20_ABI = _open_mod.ERC20_ABI
EXCHANGE_ROUTER_ABI = _open_mod.EXCHANGE_ROUTER_ABI
ORDER_TYPE_STOP_LOSS_DECREASE = _open_mod.ORDER_TYPE_STOP_LOSS_DECREASE
ORDER_TYPE_LIMIT_DECREASE = _open_mod.ORDER_TYPE_LIMIT_DECREASE

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

# Chainlink price feeds on Arbitrum (reliable on-chain fallback for CoinGecko)
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
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "100"))
MIN_POSITION_USD = float(os.getenv("MIN_POSITION_USD", "20"))
PORTFOLIO_PCT = float(os.getenv("PORTFOLIO_PCT", "0.25"))  # 25% of portfolio per trade
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "30"))
EXECUTION_FEE_WEI = int(os.getenv("GMX_V2_EXECUTION_FEE_WEI", str(Web3.to_wei(0.0002, "ether"))))

MAX_PRICE_DEVIATION = float(os.getenv("MAX_PRICE_DEVIATION", "0.05"))

# Safety
REQUIRE_SL = os.getenv("REQUIRE_SL", "true").lower() == "true"
REQUIRE_TP = os.getenv("REQUIRE_TP", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Price monitoring
PRICE_MAX_AGE_S = int(os.getenv("PRICE_MAX_AGE_S", "30"))
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

    # Which wallet this position belongs to (1 or 2)
    wallet_id: int = 1

    # On-chain tracking for TP-hit → move SL
    market_addr: Optional[str] = None
    sl_moved_to_entry: bool = False
    tp_hits_count: int = 0  # how many TPs have been hit so far
    last_known_tp_count: int = 0  # track how many TP orders remain on-chain

    @property
    def short_id(self) -> str:
        return self.id[-6:]

    @property
    def duration_hours(self) -> float:
        end_time = self.closed_at or time.time()
        return (end_time - self.opened_at) / 3600

    @property
    def pnl_percentage(self) -> float:
        if self.size_usd == 0:
            return 0.0
        return (self.unrealized_pnl / self.size_usd) * 100

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
        self.account2: Optional[Account] = None  # Second wallet for duplicate signals

        # State
        self.positions: Dict[str, Position] = {}
        self.price_cache: Dict[str, PriceData] = {}
        self.trade_history: List[TradeRecord] = []

        # Close confirmation state: chat_id -> pending close info
        self.pending_closes: Dict[int, Dict[str, Any]] = {}

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

        # Signal handler — messages from monitored channels
        @self.client.on(events.NewMessage(chats=TELEGRAM_CHANNELS))
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
            await self.handle_close_confirmation(event.chat_id, text)

        self.logger.info(f"Telegram initialized, monitoring {len(TELEGRAM_CHANNELS)} channel(s)")

    def init_web3(self):
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        if PRIVATE_KEY:
            self.account = Account.from_key(PRIVATE_KEY)
            self.logger.info(f"Web3 on {NETWORK}, wallet 1: {self.account.address[:10]}...")
        else:
            self.logger.warning("No private key — read-only mode")
        if PRIVATE_KEY_2:
            self.account2 = Account.from_key(PRIVATE_KEY_2)
            self.logger.info(f"Wallet 2: {self.account2.address[:10]}...")
        else:
            self.logger.info("No PRIVATE_KEY_2 — single wallet mode")

    async def _sync_on_chain_positions(self):
        """Scan on-chain positions for all wallets and add any that aren't
        already tracked internally. This makes the bot aware of positions
        opened manually or before a reboot."""
        MARKET_TO_SYMBOL = {v.lower(): k for k, v in GMX_V2_MARKETS.items()}

        wallets = [(1, self.account)]
        if self.account2:
            wallets.append((2, self.account2))

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
        """Get combined USDC balance across all wallets (not deployed).
        Both wallets act as one pool for sizing trades."""
        total_usdc = 0.0
        all_wallets = [self.account] + ([self.account2] if self.account2 else [])
        for acct in all_wallets:
            try:
                usdc = await asyncio.to_thread(self._get_portfolio_value_for, acct)
                total_usdc += usdc
            except Exception:
                pass
        return total_usdc

    async def _rebalance_wallets(self):
        """Send USDC from the richer wallet to the poorer one to equalize balances.
        Called after positions open/close to keep wallets balanced."""
        if not self.account2:
            return  # Single wallet mode — nothing to balance

        if DRY_RUN:
            self.logger.info("[DRY_RUN] Would rebalance wallets (skipped)")
            return

        try:
            w1_usdc = await asyncio.to_thread(self._get_portfolio_value_for, self.account)
            w2_usdc = await asyncio.to_thread(self._get_portfolio_value_for, self.account2)

            diff = w1_usdc - w2_usdc
            # Only rebalance if difference > $1 (avoid dust transfers)
            if abs(diff) < 1.0:
                self.logger.debug(
                    f"Wallets balanced (W1: ${w1_usdc:.2f}, W2: ${w2_usdc:.2f}, diff: ${abs(diff):.2f})"
                )
                return

            transfer_amount = abs(diff) / 2.0  # send half the difference
            if diff > 0:
                # W1 has more — send from W1 to W2
                sender_acct = self.account
                receiver_addr = self.account2.address
                sender_label, receiver_label = "W1", "W2"
            else:
                # W2 has more — send from W2 to W1
                sender_acct = self.account2
                receiver_addr = self.account.address
                sender_label, receiver_label = "W2", "W1"

            self.logger.info(
                f"Rebalancing: sending ${transfer_amount:.2f} USDC from {sender_label} to {receiver_label} "
                f"(before: W1=${w1_usdc:.2f}, W2=${w2_usdc:.2f})"
            )

            # Build ERC20 transfer
            usdc_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(GMX_V2_COLLATERAL_TOKEN),
                abi=ERC20_ABI,
            )
            decimals = await asyncio.to_thread(lambda: usdc_contract.functions.decimals().call())
            raw_amount = int(transfer_amount * (10 ** decimals))

            # Encode transfer(address, uint256)
            transfer_data = usdc_contract.encode_abi(
                "transfer",
                [Web3.to_checksum_address(receiver_addr), raw_amount],
            )

            tx_hash = await asyncio.to_thread(
                self._send_tx,
                GMX_V2_COLLATERAL_TOKEN,
                transfer_data,
                0,
                sender_acct,
            )

            receipt = await asyncio.to_thread(_open_mod.wait_receipt, self.w3, tx_hash)

            if receipt.get("status") == 1:
                # Verify new balances
                new_w1 = await asyncio.to_thread(self._get_portfolio_value_for, self.account)
                new_w2 = await asyncio.to_thread(self._get_portfolio_value_for, self.account2)
                self.logger.info(
                    f"Rebalance complete: W1=${new_w1:.2f}, W2=${new_w2:.2f} (TX: {tx_hash})"
                )
                await self.notify(
                    f"🔄 Wallets rebalanced\n"
                    f"Sent ${transfer_amount:.2f} USDC: {sender_label} → {receiver_label}\n"
                    f"W1: ${new_w1:.2f} | W2: ${new_w2:.2f}"
                )
            else:
                self.logger.error(f"Rebalance tx reverted: {tx_hash}")
                await self.notify(f"⚠️ Wallet rebalance failed (tx reverted): {tx_hash}")

        except Exception as e:
            self.logger.error(f"Wallet rebalance error: {e}")
            # Don't notify on rebalance failure — it's not critical
            # The bot will try again after the next trade

    async def send_startup_notification(self):
        """Send status update to admin when bot comes online."""
        try:
            # Get balances for all wallets (combined pool)
            total_usdc = 0.0
            total_deployed = 0.0
            pos_count = 0
            wallet_lines = []

            all_wallets = [self.account] + ([self.account2] if self.account2 else [])
            for i, acct in enumerate(all_wallets, 1):
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
                wallet_lines.append(f"W{i}: {addr} — ${usdc:,.2f} USDC")

            collateral_per_trade = total_usdc * PORTFOLIO_PCT

            msg = (
                f"🟢 **Bot Online**\n\n"
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

            # Skip alert messages (e.g. "target reached" notifications)
            if "target reached" in text.lower():
                self.logger.debug("Ignored 'target reached' alert")
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

            self.logger.info(f"Signal parsed: {signal.symbol} {signal.side}")

            # Validation
            if not await self.validate_signal(signal):
                return

            # Pick wallet (prefer wallet 1, fallback to wallet 2 if same symbol is open)
            # Queries on-chain positions to determine which wallet is available
            wallet_id, acct = await self._pick_wallet(signal.symbol)
            if not acct:
                await self.notify(f"Rejected {signal.symbol} {signal.side}: all wallets have open positions")
                return

            wallet_label = f" [W{wallet_id}]" if self.account2 else ""

            # Determine collateral = 25% of COMBINED pool (both wallets USDC + deployed)
            # Both wallets act as one pool
            combined_usdc = await self._get_combined_usdc()

            if combined_usdc <= 0:
                await self.notify(f"Rejected {signal.symbol}: combined USDC balance is $0")
                return

            # Cap leverage at MAX_LEVERAGE first so collateral calculation is correct
            signal.leverage = min(signal.leverage, MAX_LEVERAGE)

            collateral_usd = combined_usdc * PORTFOLIO_PCT
            size_usd = collateral_usd * signal.leverage
            size_usd = max(MIN_POSITION_USD, min(size_usd, MAX_POSITION_USD))

            if collateral_usd < MIN_POSITION_USD / signal.leverage:
                await self.notify(
                    f"Rejected {signal.symbol}: collateral ${collateral_usd:.2f} "
                    f"({PORTFOLIO_PCT:.0%} of ${combined_usdc:.2f} USDC) too small for min position ${MIN_POSITION_USD:.0f}"
                )
                return

            # Notify that we're executing
            tp_list = ", ".join(f"${tp.price:,.0f} ({tp.close_pct:.0%})" for tp in signal.take_profits)
            await self.notify(
                f"Executing {signal.symbol} {signal.side}{wallet_label}\n"
                f"Entry: ${signal.entry_low:,.0f}-${signal.entry_high:,.0f}\n"
                f"TP: {tp_list}\n"
                f"SL: ${signal.stop_loss:,.0f}\n"
                f"Collateral: ${collateral_usd:.0f} ({PORTFOLIO_PCT:.0%} of ${combined_usdc:.0f} USDC)\n"
                f"Size: ${size_usd:.0f} @ {signal.leverage:.0f}x\n"
                f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}"
            )

            # Execute on-chain with the selected wallet
            position, order_type = await self.execute_open(signal, size_usd, acct)
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
        """Get the Account object for a given wallet_id (1 or 2)."""
        if wallet_id == 2 and self.account2:
            return self.account2
        return self.account

    async def _pick_wallet(self, symbol: str) -> tuple:
        """Pick which wallet to use for a new position.
        Queries ON-CHAIN positions (not just internal tracking) so the bot
        is aware of positions opened manually or before a reboot.
        Returns (wallet_id, account) or (None, None) if all wallets busy."""
        market_addr = GMX_V2_MARKETS.get(symbol, "").lower()
        if not market_addr:
            return 1, self.account  # unknown symbol, default to W1

        wallets = [(1, self.account)]
        if self.account2:
            wallets.append((2, self.account2))

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
                        f"Wallet {wid} ({acct.address[:10]}...) has no {symbol} position — selected"
                    )
                    return wid, acct
                else:
                    self.logger.info(
                        f"Wallet {wid} ({acct.address[:10]}...) already has {symbol} open on-chain"
                    )
            except Exception as e:
                self.logger.warning(f"Failed to check wallet {wid} on-chain: {e}")
                # If we can't check, skip this wallet to be safe
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
                    self.logger.warning(f"{pos.symbol} did not close within timeout")

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

        # Price deviation check
        # Within 10%: allowed (limit order if outside entry range, market if inside)
        # Beyond 10%: rejected
        try:
            current_price = await asyncio.to_thread(fetch_current_price, signal.symbol)
            entry_avg = signal.entry_mid
            deviation = abs(current_price - entry_avg) / entry_avg
            max_deviation = 0.10  # 10% — limit orders cover up to this
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
                # Between 2-10%: will use limit order, inform admin
                self.logger.info(
                    f"Price deviation {deviation:.1%} — will place as LIMIT order"
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
    async def execute_open(self, signal: Signal, size_usd: float, acct: Account = None) -> tuple:
        """Execute a full open signal on-chain: MarketIncrease/LimitIncrease + TPs + SL.

        Returns (Position, order_type_str) or (None, None) on failure.
        """
        if acct is None:
            acct = self.account
        try:
            self.logger.info(
                f"Opening {signal.symbol} {signal.side} "
                f"${size_usd:.0f} @ {signal.leverage:.0f}x (wallet: {acct.address[:10]}...)"
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
        all_wallets = [(1, self.account)] + ([(2, self.account2)] if self.account2 else [])
        for wid, acct in all_wallets:
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
                    eth_price = await asyncio.to_thread(close_fetch_current_price, "ETH")
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

    # ──────────────────────────────────────────────────────────────────────
    # Price feeds — real CoinGecko
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
        for symbol in active_symbols:
            price = await self.fetch_price(symbol)
            if price:
                self.price_cache[symbol] = PriceData(symbol=symbol, price=price)
                self.health_stats["price_updates"] += 1

        # Update P&L for all open positions
        for pos in self.positions.values():
            if pos.is_open and pos.symbol in self.price_cache:
                pos.current_price = self.price_cache[pos.symbol].price
                if pos.side == "LONG":
                    change = pos.current_price - pos.entry_price
                else:
                    change = pos.entry_price - pos.current_price
                pos.unrealized_pnl = (change / pos.entry_price) * pos.size_usd * pos.leverage

    async def get_current_price(self, symbol: str) -> Optional[float]:
        if symbol in self.price_cache and self.price_cache[symbol].is_fresh:
            return self.price_cache[symbol].price
        price = await self.fetch_price(symbol)
        if price:
            self.price_cache[symbol] = PriceData(symbol=symbol, price=price)
            return price
        return None

    async def fetch_price(self, symbol: str) -> Optional[float]:
        """Fetch price from CoinGecko, fallback to Chainlink on-chain feed."""
        # Try CoinGecko first
        try:
            price = await asyncio.to_thread(fetch_current_price, symbol)
            if price and price > 0:
                return price
        except Exception as e:
            self.logger.debug(f"CoinGecko price fetch failed for {symbol}: {e}")

        # Fallback: Chainlink on-chain price feed (no rate limits)
        feed_addr = CHAINLINK_FEEDS.get(symbol.upper())
        if feed_addr:
            try:
                feed = self.w3.eth.contract(
                    address=Web3.to_checksum_address(feed_addr),
                    abi=CHAINLINK_ABI,
                )
                result = await asyncio.to_thread(
                    feed.functions.latestRoundData().call
                )
                decimals = await asyncio.to_thread(
                    feed.functions.decimals().call
                )
                price = result[1] / (10 ** decimals)
                if price > 0:
                    self.logger.debug(f"Chainlink price for {symbol}: ${price:,.2f}")
                    return price
            except Exception as e:
                self.logger.debug(f"Chainlink price fetch failed for {symbol}: {e}")

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
                await self.check_position_closed()
                await self.check_tp_hits()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"TP monitor error: {e}")
                await asyncio.sleep(30)

    async def check_position_closed(self):
        """Detect when a tracked open position disappears from chain
        (SL hit, liquidation, or all TPs filled) and report PnL."""
        open_positions = [p for p in self.positions.values()
                          if p.is_open and p.market_addr]
        if not open_positions:
            return

        # Fetch chain positions per wallet
        chain_keys_by_wallet: Dict[int, set] = {}
        for wid in {p.wallet_id for p in open_positions}:
            acct = self._get_account(wid)
            try:
                cps = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                chain_keys_by_wallet[wid] = {
                    (cp.market.lower(), cp.is_long) for cp in cps
                }
            except Exception as e:
                self.logger.debug(f"check_position_closed: fetch failed for wallet {wid}: {e}")
                chain_keys_by_wallet[wid] = None  # skip this wallet

        for pos in open_positions:
            chain_keys = chain_keys_by_wallet.get(pos.wallet_id)
            if chain_keys is None:
                continue  # fetch failed for this wallet, skip
            key = (pos.market_addr.lower(), pos.side == "LONG")
            if key not in chain_keys:
                # Position is gone from chain — it was closed by keepers
                # (SL hit, liquidation, or final TP filled the entire position)

                # Determine exit reason
                if pos.sl_moved_to_entry:
                    exit_reason = "SL (breakeven)"
                elif pos.last_known_tp_count > 0:
                    exit_reason = "SL/TP/liquidation"
                else:
                    exit_reason = "SL/liquidation"

                # Get last known price for PnL estimate
                current_price = await self.get_current_price(pos.symbol)
                if current_price:
                    pos.current_price = current_price
                    if pos.side == "LONG":
                        change = current_price - pos.entry_price
                    else:
                        change = pos.entry_price - current_price
                    pos.unrealized_pnl = (change / pos.entry_price) * pos.size_usd * pos.leverage

                pnl_sign = "+" if pos.unrealized_pnl >= 0 else ""
                price_str = f"${current_price:,.2f}" if current_price else "N/A"

                self.logger.info(
                    f"Position closed on-chain: {pos.symbol} {pos.side} "
                    f"PnL={pnl_sign}${pos.unrealized_pnl:,.2f} reason={exit_reason}"
                )

                await self.notify(
                    f"Position Closed: {pos.symbol} {pos.side}\n"
                    f"Entry: ${pos.entry_price:,.2f}  |  Exit: {price_str}\n"
                    f"PnL: {pnl_sign}${pos.unrealized_pnl:,.2f} ({pnl_sign}{pos.pnl_percentage:.1f}%)"
                )

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
                    self.logger.warning(f"Failed to cancel orphaned orders for {pos.symbol}: {e}")

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
                          if p.is_open and p.market_addr]
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

            if current_tp_count < pos.last_known_tp_count:
                hit_count = pos.last_known_tp_count - current_tp_count

                # Safety: if ALL TPs vanished at once, verify position exists.
                # Keepers auto-cancel all orders when a position is rejected or
                # doesn't exist. A real TP execution removes 1 order at a time.
                if current_tp_count == 0 and hit_count > 1:
                    self.logger.info(
                        f"All {hit_count} TPs vanished for {pos.symbol} {pos.side} — "
                        f"verifying position still exists on-chain..."
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
                        self.logger.warning(f"check_tp_hits: position check failed: {e}")
                        chain_pos = None

                    if not chain_pos:
                        # Position doesn't exist — orders were auto-cancelled by keepers
                        self.logger.warning(
                            f"Position {pos.symbol} {pos.side} not found on-chain. "
                            f"TPs were auto-cancelled (position rejected or already closed). "
                            f"NOT treating as TP hit."
                        )
                        pos.last_known_tp_count = 0
                        continue

                # Verify price actually reached TP level before declaring a hit.
                # If TP orders were manually cancelled (via /close), the price
                # won't be near the TP level → skip, just update baseline.
                current_price = await self.get_current_price(pos.symbol)
                if not current_price:
                    try:
                        cps = await asyncio.to_thread(
                            chain_fetch_positions, self.w3, pos_acct.address
                        )
                        cp = next((c for c in cps if c.market.lower() == market_lower), None)
                        current_price = cp.current_price if cp else None
                    except Exception:
                        pass

                if current_price:
                    # Determine which TP should have been hit based on sorted TPs
                    sorted_tps = sorted(pos.take_profits, key=lambda t: t.price,
                                        reverse=(pos.side == "SHORT"))
                    # The TP that was supposedly hit is the next one in sequence
                    next_tp_idx = pos.tp_hits_count  # 0-based index of next TP to be hit
                    if next_tp_idx < len(sorted_tps):
                        expected_tp_price = sorted_tps[next_tp_idx].price
                        # For LONG: price must be >= TP price (within 2% tolerance)
                        # For SHORT: price must be <= TP price (within 2% tolerance)
                        tolerance = expected_tp_price * 0.02
                        if is_long and current_price < expected_tp_price - tolerance:
                            self.logger.warning(
                                f"TP count dropped for {pos.symbol} {pos.side} but price "
                                f"${current_price:,.2f} hasn't reached TP ${expected_tp_price:,.2f}. "
                                f"Orders likely cancelled manually. Updating baseline only."
                            )
                            pos.last_known_tp_count = current_tp_count
                            continue
                        elif not is_long and current_price > expected_tp_price + tolerance:
                            self.logger.warning(
                                f"TP count dropped for {pos.symbol} {pos.side} but price "
                                f"${current_price:,.2f} hasn't reached TP ${expected_tp_price:,.2f}. "
                                f"Orders likely cancelled manually. Updating baseline only."
                            )
                            pos.last_known_tp_count = current_tp_count
                            continue

                # Genuine TP hit — position exists + price confirmed
                pos.tp_hits_count += hit_count
                self.logger.info(
                    f"TP HIT detected: {pos.symbol} {pos.side} — "
                    f"{hit_count} TP(s) executed (was {pos.last_known_tp_count}, now {current_tp_count}), "
                    f"total hits: {pos.tp_hits_count}"
                )

                # Fetch current PnL from chain for the notification
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
                        pnl_sign = "+" if chain_pos.unrealized_pnl >= 0 else ""
                        pnl_line = (
                            f"Current PnL: {pnl_sign}${chain_pos.unrealized_pnl:,.2f} "
                            f"({pnl_sign}{chain_pos.pnl_percentage:.1f}%)\n"
                            f"Remaining size: ${chain_pos.size_usd:,.2f}\n"
                        )
                except Exception:
                    pass

                # Determine new SL target based on how many TPs have been hit:
                #   TP1 hit → SL moves to entry
                #   TP(N) hit (N>=2) → SL moves to TP(N-1) price
                # Works for any number of TPs (2-5+)
                sorted_tps = sorted(pos.take_profits, key=lambda t: t.price,
                                    reverse=(pos.side == "SHORT"))
                hits = pos.tp_hits_count
                if hits <= 1:
                    new_sl_target = pos.entry_price
                    sl_label = "Entry"
                elif hits - 2 < len(sorted_tps):
                    # TP2 hit → SL to TP1 (index 0), TP3 → TP2 (index 1), etc.
                    new_sl_target = sorted_tps[hits - 2].price
                    sl_label = f"TP{hits - 1}"
                else:
                    # More hits than TPs we know about — use last TP
                    new_sl_target = sorted_tps[-1].price if sorted_tps else pos.entry_price
                    sl_label = f"TP{len(sorted_tps)}" if sorted_tps else "Entry"

                await self.notify(
                    f"TP{pos.tp_hits_count} Hit: {pos.symbol} {pos.side}\n"
                    f"{pnl_line}"
                    f"SL → {sl_label} ${new_sl_target:,.2f}"
                )
                await self.move_sl(pos, orders, new_sl_target, sl_label)
                pos.last_known_tp_count = current_tp_count

    async def move_sl(self, pos: "Position", orders: list,
                      new_sl_price: float, sl_label: str = "entry"):
        """Cancel ALL existing SL orders for this market, then place a new one.

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
                self.logger.warning(f"move_sl: could not re-fetch orders, using passed list: {e}")
                fresh_orders = orders

            # Find ALL SL orders for this market (cancel every one)
            sl_orders = [
                o for o in fresh_orders
                if o["market"].lower() == market_lower
                and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
            ]

            self.logger.info(
                f"move_sl: found {len(sl_orders)} SL order(s) to cancel for {pos.symbol}"
            )

            # Cancel old SL order(s)
            cancelled_count = 0
            for sl in sl_orders:
                if not sl.get("key_hex"):
                    self.logger.warning(f"  SL order has no key_hex, skipping")
                    continue
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
                            _open_mod.wait_receipt(self.w3, txh)
                        return txh
                    txh = await asyncio.to_thread(_cancel_sl)
                    self.logger.info(f"Old SL cancelled: {txh}")
                    cancelled_count += 1
                except Exception as e:
                    self.logger.warning(f"Failed to cancel old SL: {e}")

            if sl_orders and cancelled_count == 0:
                self.logger.error(
                    f"Could not cancel any of {len(sl_orders)} old SL orders! "
                    f"Aborting new SL placement to avoid duplicates."
                )
                await self.notify(
                    f"Failed to cancel old SL for {pos.symbol}. "
                    f"New SL NOT placed to avoid duplicates."
                )
                return

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

            pos.stop_loss = new_sl_price
            pos.sl_moved_to_entry = True

            self.logger.info(
                f"SL moved to {sl_label} ${new_sl_price:,.2f} for {pos.symbol} {pos.side} "
                f"size=${remaining_size_usd:,.2f} tx={txh}"
            )

        except Exception as e:
            self.logger.error(f"move_sl failed: {e}\n{traceback.format_exc()}")
            await self.notify(f"Failed to move SL for {pos.symbol}: {e}")

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
/balance — Wallet ETH & token balance
/halt [reason] — Halt trading
/resume [reason] — Resume trading
/winrate [SYMBOL] [N] — Win rate stats
/pnl — PnL summary (today / 30d / all time) for BTC, SOL, ETH
/health — System health
/help — This message"""
        await self.send_message(chat_id, msg)

    async def cmd_status(self, chat_id: int):
        health = self.get_health_report()
        status = "HALTED" if health["status"] == "HALTED" else "ACTIVE"
        wallet_lines = []
        if self.account:
            wallet_lines.append(f"W1: {self.account.address[:8]}...{self.account.address[-6:]}")
        if self.account2:
            wallet_lines.append(f"W2: {self.account2.address[:8]}...{self.account2.address[-6:]}")
        wallet_str = "\n".join(wallet_lines) if wallet_lines else "N/A"
        msg = (
            f"**GMX V2 Bot Status**\n\n"
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
        Each GMXPosition gets a _wallet_acct attribute for close routing."""
        all_positions = []
        all_orders = []
        for acct in [self.account] + ([self.account2] if self.account2 else []):
            try:
                pos, ords = await asyncio.gather(
                    asyncio.to_thread(chain_fetch_positions, self.w3, acct.address),
                    asyncio.to_thread(fetch_open_orders, self.w3, acct.address),
                )
                for p in pos:
                    p._wallet_acct = acct  # tag with owning wallet
                all_positions.extend(pos)
                all_orders.extend(ords)
            except Exception as e:
                self.logger.warning(f"Failed to fetch from {acct.address[:10]}: {e}")
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
                pnl_icon = "+" if pos.unrealized_pnl >= 0 else ""

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
                if self.account2 and hasattr(pos, '_wallet_acct'):
                    if pos._wallet_acct.address == self.account.address:
                        wid_label = " [W1]"
                    elif pos._wallet_acct.address == self.account2.address:
                        wid_label = " [W2]"

                msg += (
                    f"\n**#{i} {pos.symbol} {side}{wid_label}**\n"
                    f"  Size:    ${pos.size_usd:,.2f} @ {pos.leverage:.1f}x\n"
                    f"  Entry:   ${pos.entry_price:,.2f}\n"
                    f"  Current: ${pos.current_price:,.2f}\n"
                    f"  PnL:     {pnl_icon}${pos.unrealized_pnl:.2f} ({pnl_icon}{pos.pnl_percentage:.1f}%)\n"
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
                                    f"within 2 minutes. Order cancellation may fail."
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
                all_wallets = [self.account] + ([self.account2] if self.account2 else [])
                if also_cancel_orders:
                    # /close all — cancel every open order on all wallets
                    for acct in all_wallets:
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
                        for acct in all_wallets:
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

    async def cmd_balance(self, chat_id: int):
        """Show wallet balances and trade sizing (combined pool)."""
        try:
            total_usdc = 0.0
            total_deployed = 0.0
            wallet_lines = []

            all_wallets = [self.account] + ([self.account2] if self.account2 else [])
            for i, acct in enumerate(all_wallets, 1):
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

                label = f"W{i}"
                addr = f"{acct.address[:10]}...{acct.address[-6:]}"
                dep_str = f"${deployed:,.2f}" if deployed > 0 else "$0.00"

                wallet_lines.append(
                    f"**{label}** {addr}\n"
                    f"  USDC: ${usdc:,.2f} | Deployed: {dep_str} | Positions: {n_pos}"
                )

            collateral_per_trade = total_usdc * PORTFOLIO_PCT

            msg = (
                f"**Wallet Balance**\n\n"
                + "\n".join(wallet_lines)
                + f"\n\n**Combined**\n"
                f"USDC: ${total_usdc:,.2f}\n"
                f"Deployed: ${total_deployed:,.2f}\n"
                f"Collateral/trade: ${collateral_per_trade:,.2f} ({PORTFOLIO_PCT:.0%} of ${total_usdc:,.2f} USDC)"
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

    async def cmd_health(self, chat_id: int):
        h = self.get_health_report()
        msg = (
            f"**System Health**\n\n"
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
            for acct in [self.account] + ([self.account2] if self.account2 else []):
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
    def _record_trade(self, gmx_pos: "GMXPosition", exit_reason: str = "manual"):
        """Record a closed trade into trade_history for /winrate and /pnl stats."""
        closed_at = time.time()
        pnl_usd = gmx_pos.unrealized_pnl          # best estimate at close time
        pnl_pct = (pnl_usd / gmx_pos.collateral_amount * 100) if gmx_pos.collateral_amount else 0.0
        # opened_at: use entry_price timestamp proxy — we don't store open time for
        # GMXPosition, so use closed_at - 1h as a fallback (display only, not critical)
        opened_at = closed_at - 3600
        record = TradeRecord(
            id=str(uuid.uuid4()),
            symbol=gmx_pos.symbol,
            side="LONG" if gmx_pos.is_long else "SHORT",
            entry_price=gmx_pos.entry_price,
            exit_price=gmx_pos.current_price,
            size_usd=gmx_pos.size_usd,
            leverage=gmx_pos.leverage,
            duration_hours=(closed_at - opened_at) / 3600,
            pnl_usd=pnl_usd,
            pnl_percentage=pnl_pct,
            exit_reason=exit_reason,
            opened_at=opened_at,
            closed_at=closed_at,
        )
        self.trade_history.append(record)
        self.logger.info(
            f"Recorded trade: {gmx_pos.symbol} {'LONG' if gmx_pos.is_long else 'SHORT'} "
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
