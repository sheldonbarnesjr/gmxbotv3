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
import json
import time
import uuid
import asyncio
import logging
import importlib
import statistics
import traceback
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from collections import defaultdict

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
fetch_current_price = _open_mod.fetch_current_price
COINGECKO_IDS = _open_mod.COINGECKO_IDS
INDEX_TOKEN_DECIMALS = _open_mod.INDEX_TOKEN_DECIMALS
ERC20_ABI = _open_mod.ERC20_ABI

# From close.py — position fetching & closing
from close import (
    fetch_positions as chain_fetch_positions,
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
RPC_URL = os.getenv("ARBITRUM_RPC_URL") or os.getenv("RPC_URL", "https://arb1.arbitrum.io/rpc")

# GMX V2 addresses
GMX_V2_EXCHANGE_ROUTER = os.getenv("GMX_V2_EXCHANGE_ROUTER", "").strip()
GMX_V2_ORDER_VAULT = os.getenv("GMX_V2_ORDER_VAULT", "").strip()
GMX_V2_MARKET = os.getenv("GMX_V2_MARKET", "").strip()
GMX_V2_COLLATERAL_TOKEN = os.getenv("GMX_V2_COLLATERAL_TOKEN", "").strip()

# Trading
MAX_LEVERAGE = float(os.getenv("MAX_LEVERAGE", "10"))
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "100"))
MIN_POSITION_USD = float(os.getenv("MIN_POSITION_USD", "20"))
PORTFOLIO_PCT = float(os.getenv("PORTFOLIO_PCT", "0.25"))  # 25% of portfolio per trade
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "30"))
EXECUTION_FEE_WEI = int(os.getenv("GMX_V2_EXECUTION_FEE_WEI", str(Web3.to_wei(0.0002, "ether"))))

# Risk
SKIP_HIGH_RISK = os.getenv("SKIP_HIGH_RISK", "true").lower() == "true"
HIGH_RISK_KEYWORDS = ["yolo", "degen", "moonshot", "lambo", "ape", "fomo"]
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
class RiskProfile:
    level: str = "normal"
    leverage_multiplier: float = 1.0
    allocation_multiplier: float = 1.0
    expires_at: Optional[float] = None
    reason: str = ""

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

        # State
        self.positions: Dict[str, Position] = {}
        self.risk_profiles: Dict[str, RiskProfile] = {}
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

        self.price_update_task = asyncio.create_task(self.price_update_loop())
        self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())

        self.logger.info("GMX Bot started successfully")

        # Send startup notification to admin
        await self.send_startup_notification()

        try:
            await self.client.run_until_disconnected()
        except KeyboardInterrupt:
            self.logger.info("Bot stopped by user")
        finally:
            await self.shutdown()

    async def shutdown(self):
        self.logger.info("Shutting down GMX Bot...")
        if self.price_update_task:
            self.price_update_task.cancel()
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
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
            self.logger.info(f"Web3 on {NETWORK}, wallet: {self.account.address[:10]}...")
        else:
            self.logger.warning("No private key — read-only mode")

    def get_portfolio_value(self) -> float:
        """Get USDC collateral token balance in USD (portfolio value)."""
        try:
            token = self.w3.eth.contract(
                address=Web3.to_checksum_address(GMX_V2_COLLATERAL_TOKEN),
                abi=ERC20_ABI,
            )
            decimals = token.functions.decimals().call()
            balance_raw = token.functions.balanceOf(self.account.address).call()
            balance = balance_raw / (10 ** decimals)
            return balance
        except Exception as e:
            self.logger.error(f"Error getting portfolio value: {e}")
            return 0.0

    async def send_startup_notification(self):
        """Send status update to admin when bot comes online."""
        try:
            # Get balances
            eth_bal = await asyncio.to_thread(
                self.w3.eth.get_balance, self.account.address
            )
            eth_formatted = eth_bal / 10**18

            portfolio = await asyncio.to_thread(self.get_portfolio_value)
            trade_size = portfolio * PORTFOLIO_PCT

            # Count on-chain positions
            try:
                positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, self.account.address
                )
                pos_count = len(positions) if positions else 0
            except Exception:
                pos_count = 0

            wallet = f"{self.account.address[:8]}...{self.account.address[-6:]}"

            msg = (
                f"**Bot Online**\n\n"
                f"Wallet: {wallet}\n"
                f"Network: {NETWORK.upper()}\n"
                f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}\n\n"
                f"Portfolio: ${portfolio:,.2f} USDC\n"
                f"Trade size: ${trade_size:,.2f} ({PORTFOLIO_PCT:.0%})\n"
                f"ETH: {eth_formatted:.6f}\n"
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

            # Try to parse with open.py's robust parser
            try:
                signal = parse_signal(text)
            except (ValueError, Exception) as e:
                self.logger.debug(f"Could not parse signal: {e}")
                return

            # Check symbol is supported
            if signal.symbol not in COINGECKO_IDS:
                self.logger.debug(f"Unsupported symbol: {signal.symbol}")
                return

            self.logger.info(f"Signal parsed: {signal.symbol} {signal.side}")

            # Validation
            if not await self.validate_signal(signal):
                return

            # Risk assessment
            risk = self.assess_risk(signal, text)
            if risk == "risky" and SKIP_HIGH_RISK:
                self.logger.warning(f"Skipping high-risk signal for {signal.symbol}")
                await self.notify(f"Skipped high-risk {signal.symbol} {signal.side} signal")
                return

            # Determine position size = 25% of portfolio (USDC balance)
            portfolio = await asyncio.to_thread(self.get_portfolio_value)
            size_usd = portfolio * PORTFOLIO_PCT
            risk_profile = self.get_risk_profile(signal.symbol)
            size_usd *= risk_profile.allocation_multiplier
            size_usd = max(MIN_POSITION_USD, min(size_usd, MAX_POSITION_USD))

            if portfolio <= 0:
                await self.notify(f"Rejected {signal.symbol}: portfolio balance is $0")
                return
            if size_usd < MIN_POSITION_USD:
                await self.notify(
                    f"Rejected {signal.symbol}: trade size ${size_usd:.2f} "
                    f"(25% of ${portfolio:.2f}) below minimum ${MIN_POSITION_USD:.0f}"
                )
                return

            # Cap leverage
            leverage = min(signal.leverage, MAX_LEVERAGE)
            leverage *= risk_profile.leverage_multiplier
            leverage = min(leverage, MAX_LEVERAGE)
            signal.leverage = leverage

            # Notify that we're executing
            tp_list = ", ".join(f"${tp.price:,.0f} ({tp.close_pct:.0%})" for tp in signal.take_profits)
            await self.notify(
                f"Executing {signal.symbol} {signal.side}\n"
                f"Entry: ${signal.entry_low:,.0f}-${signal.entry_high:,.0f}\n"
                f"TP: {tp_list}\n"
                f"SL: ${signal.stop_loss:,.0f}\n"
                f"Size: ${size_usd:.0f} ({PORTFOLIO_PCT:.0%} of ${portfolio:.0f}) @ {leverage:.0f}x\n"
                f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}"
            )

            # Execute on-chain
            position, order_type = await self.execute_open(signal, size_usd)
            if position:
                self.positions[position.id] = position
                self.health_stats["trades_executed"] += 1
                await self.notify_position_opened(position, order_type)

        except Exception as e:
            self.logger.error(f"Error processing signal: {e}\n{traceback.format_exc()}")
            self.health_stats["errors"] += 1
            await self.notify(f"Error processing signal: {e}")

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

        # Check for existing position in same symbol
        for pos in self.positions.values():
            if pos.symbol == signal.symbol and pos.is_open:
                self.logger.info(f"Already have open {signal.symbol} position")
                await self.notify(f"Rejected {signal.symbol} {signal.side}: already have open position")
                return False

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

    def assess_risk(self, signal: Signal, raw_text: str) -> str:
        """Assess signal risk level."""
        score = 0
        text_lower = raw_text.lower()
        score += sum(2 for w in HIGH_RISK_KEYWORDS if w in text_lower)
        if signal.leverage > 20:
            score += 2
        elif signal.leverage > 10:
            score += 1
        if signal.stop_loss:
            sl_dist = abs(signal.stop_loss - signal.entry_mid) / signal.entry_mid
            if sl_dist < 0.02:
                score += 2
            elif sl_dist < 0.05:
                score += 1
        return "risky" if score >= 4 else "normal" if score >= 2 else "safe"

    # ──────────────────────────────────────────────────────────────────────
    # OPEN — Real on-chain execution via open.py
    # ──────────────────────────────────────────────────────────────────────
    async def execute_open(self, signal: Signal, size_usd: float) -> tuple:
        """Execute a full open signal on-chain: MarketIncrease/LimitIncrease + TPs + SL.

        Returns (Position, order_type_str) or (None, None) on failure.
        """
        try:
            self.logger.info(
                f"Opening {signal.symbol} {signal.side} "
                f"${size_usd:.0f} @ {signal.leverage:.0f}x"
            )

            # Run synchronous web3 calls in a thread
            results = await asyncio.to_thread(
                execute_signal,
                self.w3,
                self.account,
                signal,
                GMX_V2_EXCHANGE_ROUTER,
                GMX_V2_ORDER_VAULT,
                GMX_V2_MARKET,
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
    async def execute_close(self, gmx_pos: GMXPosition, percentage: float = 1.0) -> Optional[str]:
        """Execute a close order on-chain via close.py's create_close_order."""
        try:
            tx_hash = await asyncio.to_thread(
                create_close_order,
                self.w3,
                self.account,
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
        """Fetch real price from CoinGecko via open.py's fetch_current_price."""
        try:
            price = await asyncio.to_thread(fetch_current_price, symbol)
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
        issues = []
        for symbol, pd in self.price_cache.items():
            if not pd.is_fresh and pd.age_seconds > HALT_ON_PRICE_STALE:
                issues.append(f"{symbol}({pd.age_seconds:.0f}s stale)")
        if issues and not self.is_halted:
            await self.halt_trading(f"Stale prices: {', '.join(issues)}")
        if not issues and self.is_halted and self.halt_time:
            if time.time() - self.halt_time > AUTO_RESUME_AFTER:
                await self.resume_trading("Auto-resume: prices recovered")

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
    # Risk
    # ──────────────────────────────────────────────────────────────────────
    def get_risk_profile(self, symbol: str) -> RiskProfile:
        if symbol in self.risk_profiles:
            p = self.risk_profiles[symbol]
            if p.expires_at and time.time() > p.expires_at:
                del self.risk_profiles[symbol]
                return RiskProfile()
            return p
        return RiskProfile()

    def set_risk_profile(self, symbol: str, level: str, hours: Optional[int] = None, reason: str = ""):
        multipliers = {
            "safe": (0.5, 0.5),
            "normal": (1.0, 1.0),
            "risky": (1.5, 1.5),
        }
        lm, am = multipliers.get(level, (1.0, 1.0))
        self.risk_profiles[symbol] = RiskProfile(
            level=level,
            leverage_multiplier=lm,
            allocation_multiplier=am,
            expires_at=(time.time() + hours * 3600) if hours else None,
            reason=reason,
        )

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
            elif cmd == "/cancel":
                if chat_id in self.pending_closes:
                    del self.pending_closes[chat_id]
                    await self.send_message(chat_id, "Close cancelled.")
                else:
                    await self.send_message(chat_id, "Nothing to cancel.")
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
            elif cmd == "/adjrisk":
                await self.cmd_adjust_risk(chat_id, parts[1:])
            else:
                await self.send_message(chat_id, "Unknown command. Type /help")

        except Exception as e:
            self.logger.error(f"Admin command error: {e}\n{traceback.format_exc()}")
            await self.send_message(chat_id, f"Command error: {e}")

    async def cmd_help(self, chat_id: int):
        msg = """**GMX V2 Bot Commands**

/status — Bot status & mode
/positions — Show on-chain positions
/close — Interactive close (fetches from chain)
/close all — Close all positions
/close 1 — Close position #1
/confirm — Confirm pending close
/cancel — Cancel pending close
/balance — Wallet ETH & token balance
/halt [reason] — Halt trading
/resume [reason] — Resume trading
/winrate [SYMBOL] [N] — Win rate stats
/health — System health
/adjrisk SYMBOL safe|normal|risky [hours]
/help — This message"""
        await self.send_message(chat_id, msg)

    async def cmd_status(self, chat_id: int):
        health = self.get_health_report()
        status = "HALTED" if health["status"] == "HALTED" else "ACTIVE"
        wallet = self.account.address[:8] + "..." if self.account else "N/A"
        msg = (
            f"**GMX V2 Bot Status**\n\n"
            f"Status: {status}\n"
            f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}\n"
            f"Wallet: {wallet}\n"
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

    async def cmd_positions(self, chat_id: int):
        """Show on-chain positions fetched from GMX Reader contract."""
        await self.send_message(chat_id, "Fetching on-chain positions...")

        try:
            positions = await asyncio.to_thread(
                chain_fetch_positions, self.w3, self.account.address
            )
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching positions: {e}")
            return

        if not positions:
            await self.send_message(chat_id, "No open positions on-chain.")
            return

        msg = f"**On-Chain Positions ({len(positions)})**\n\n"
        for i, pos in enumerate(positions, 1):
            side = "LONG" if pos.is_long else "SHORT"
            pnl_icon = "+" if pos.unrealized_pnl >= 0 else ""
            msg += (
                f"**#{i} {pos.symbol} {side}**\n"
                f"  Size: ${pos.size_usd:,.2f} @ {pos.leverage:.1f}x\n"
                f"  Entry: ${pos.entry_price:,.2f}\n"
                f"  Current: ${pos.current_price:,.2f}\n"
                f"  PnL: {pnl_icon}${pos.unrealized_pnl:.2f} ({pnl_icon}{pos.pnl_percentage:.1f}%)\n\n"
            )

        total_pnl = sum(p.unrealized_pnl for p in positions)
        total_size = sum(p.size_usd for p in positions)
        msg += f"Total Size: ${total_size:,.2f}\nTotal PnL: ${total_pnl:+.2f}"
        await self.send_message(chat_id, msg)

    async def cmd_close(self, chat_id: int, arg: Optional[str]):
        """Interactive close flow via Telegram.

        /close       — list positions, ask which to close
        /close all   — close all positions (with confirm)
        /close 1     — close position #1 (with confirm)
        /close BTC   — close BTC position (with confirm)
        """
        # Fetch on-chain positions
        await self.send_message(chat_id, "Fetching positions...")
        try:
            positions = await asyncio.to_thread(
                chain_fetch_positions, self.w3, self.account.address
            )
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching positions: {e}")
            return

        if not positions:
            await self.send_message(chat_id, "No open positions to close.")
            return

        if arg is None:
            # Show positions and ask which to close
            msg = "**Select position to close:**\n\n"
            for i, pos in enumerate(positions, 1):
                side = "LONG" if pos.is_long else "SHORT"
                msg += (
                    f"**#{i}** {pos.symbol} {side} — "
                    f"${pos.size_usd:,.2f} @ {pos.leverage:.1f}x — "
                    f"PnL: ${pos.unrealized_pnl:+.2f}\n"
                )
            msg += (
                f"\nReply with:\n"
                f"  /close 1 — close position #1\n"
                f"  /close all — close all positions\n"
                f"  /close BTC — close by symbol"
            )
            await self.send_message(chat_id, msg)
            return

        # Determine which positions to close
        arg_upper = arg.upper()
        to_close: List[GMXPosition] = []

        if arg_upper == "ALL":
            to_close = positions
        elif arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(positions):
                to_close = [positions[idx]]
            else:
                await self.send_message(chat_id, f"Invalid position #{arg}. Range: 1-{len(positions)}")
                return
        else:
            # Match by symbol
            for pos in positions:
                if arg_upper in pos.symbol.upper():
                    to_close.append(pos)
            if not to_close:
                await self.send_message(chat_id, f"No position found matching '{arg}'")
                return

        # Show confirmation
        msg = "**Confirm close:**\n\n"
        for pos in to_close:
            side = "LONG" if pos.is_long else "SHORT"
            msg += f"  {pos.symbol} {side} — ${pos.size_usd:,.2f} — PnL: ${pos.unrealized_pnl:+.2f}\n"
        msg += f"\nSend /confirm to execute or /cancel to abort."

        self.pending_closes[chat_id] = {
            "positions": to_close,
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

            await self.send_message(chat_id, f"Closing {len(positions_to_close)} position(s)...")

            for pos in positions_to_close:
                side = "LONG" if pos.is_long else "SHORT"
                await self.send_message(chat_id, f"Closing {pos.symbol} {side}...")

                tx_hash = await self.execute_close(pos, 1.0)
                if tx_hash:
                    await self.send_message(
                        chat_id,
                        f"Closed {pos.symbol} {side}\n"
                        f"TX: {tx_hash}\n"
                        f"{'https://arbiscan.io/tx/' + tx_hash if not tx_hash.startswith('dry_run') else 'DRY RUN'}"
                    )
                else:
                    await self.send_message(chat_id, f"FAILED to close {pos.symbol} {side}")

            await self.send_message(chat_id, "Close flow complete.")

        elif text_upper in ("NO", "N", "CANCEL"):
            del self.pending_closes[chat_id]
            await self.send_message(chat_id, "Close cancelled.")

    async def cmd_balance(self, chat_id: int):
        """Show wallet balances and trade sizing."""
        try:
            eth_bal = await asyncio.to_thread(
                self.w3.eth.get_balance, self.account.address
            )
            eth_formatted = eth_bal / 10**18

            portfolio = await asyncio.to_thread(self.get_portfolio_value)
            trade_size = portfolio * PORTFOLIO_PCT

            msg = (
                f"**Wallet Balance**\n\n"
                f"Address: {self.account.address[:10]}...{self.account.address[-6:]}\n"
                f"USDC: ${portfolio:,.2f}\n"
                f"ETH: {eth_formatted:.6f}\n\n"
                f"**Trade Sizing**\n"
                f"Per trade: ${trade_size:,.2f} ({PORTFOLIO_PCT:.0%} of portfolio)\n"
                f"Min: ${MIN_POSITION_USD:.0f} | Max: ${MAX_POSITION_USD:.0f}\n"
                f"Execution fee: {EXECUTION_FEE_WEI / 10**18:.6f} ETH/order"
            )
            await self.send_message(chat_id, msg)
        except Exception as e:
            await self.send_message(chat_id, f"Error: {e}")

    async def cmd_winrate(self, chat_id: int, symbol: Optional[str], n: Optional[int]):
        stats = self.calculate_win_rate(symbol, n)
        if "error" in stats:
            await self.send_message(chat_id, stats["error"])
            return
        title = "Win Rate"
        if symbol:
            title += f" — {symbol}"
        if n:
            title += f" (last {n})"
        msg = (
            f"**{title}**\n\n"
            f"Win Rate: {stats['win_rate']:.1f}% ({stats['wins']}/{stats['total']})\n"
            f"Profit Factor: {stats['profit_factor']:.2f}\n"
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

    async def cmd_adjust_risk(self, chat_id: int, args: List[str]):
        if len(args) < 2:
            if self.risk_profiles:
                msg = "**Risk Profiles:**\n"
                for sym, p in self.risk_profiles.items():
                    msg += f"  {sym}: {p.level}\n"
                await self.send_message(chat_id, msg)
            else:
                await self.send_message(chat_id, "No custom risk profiles set.\nUsage: /adjrisk BTC safe [hours]")
            return

        symbol = args[0].upper()
        level = args[1].lower()
        if level not in ("safe", "normal", "risky", "clear"):
            await self.send_message(chat_id, "Level must be: safe, normal, risky, or clear")
            return
        if level == "clear":
            self.risk_profiles.pop(symbol, None)
            await self.send_message(chat_id, f"Cleared risk for {symbol}")
            return
        hours = int(args[2]) if len(args) > 2 else None
        self.set_risk_profile(symbol, level, hours)
        dur = f" for {hours}h" if hours else ""
        await self.send_message(chat_id, f"Set {symbol} risk to {level}{dur}")

    # ──────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────
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
            msg += "\n\n⏳ Limit order placed — waiting for price to reach entry."
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
    await bot.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
