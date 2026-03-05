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
import json
import time
import uuid
import asyncio
import hashlib
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
# IMPORT CONFIG MODULE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from config import load_config, Config, ALLOWED_SYMBOLS, CHAINLINK_FEEDS, CHAINLINK_ABI
from state_io import atomic_json_write, safe_json_read
from signal_store import SignalStore

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IMPORT RISK FUNCTIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from risk import (
    is_update_message,
    cap_leverage,
    calculate_position_size,
    check_min_collateral,
    validate_sl_required,
    validate_tp_required,
    check_price_deviation,
    validate_sl_tp_direction,
    classify_exit_reason,
    calculate_unrealized_pnl,
    determine_new_sl_target,
    verify_tp_hit_by_price,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IMPORT MIXIN ARCHITECTURE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from notifications import NotificationsMixin
from sl_tp import SLTPMixin
from wallet_mgmt import WalletMixin
from price_feeds import PriceFeedsMixin, PriceData
from analytics import AnalyticsMixin, TradeRecord
from withdraw_mixin import WithdrawMixin
from telegram import CoreTelegramMixin

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
# DATA STRUCTURES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class TakeProfitLevel:
    price: float
    percentage: float


@dataclass
class FailedOrder:
    """Represents a TP or SL order that failed to place on-chain and needs retry."""
    position_id: str
    symbol: str
    side: str
    market_addr: str
    wallet_id: int
    order_kind: str          # "tp" or "sl"
    price: float             # trigger price
    size_usd: float          # total position size (for TP percentage calc)
    close_pct: float         # fraction of position to close (0.0–1.0)
    is_long: bool
    attempts: int = 0
    max_attempts: int = 5
    last_attempt: float = 0.0
    error: str = ""
    created_at: float = field(default_factory=time.time)


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
    signal_id: Optional[str] = None  # links back to SignalStore entry
    order_history: list = field(default_factory=list)  # chronological order events

    # Which wallet this position belongs to (1=swing, 2-4=scalp)
    wallet_id: int = 1

    # On-chain tracking for TP-hit → move SL
    market_addr: Optional[str] = None
    collateral_token: Optional[str] = None  # on-chain collateral token address (for Reader PnL queries)
    sl_moved_to_entry: bool = False
    sl_move_label: Optional[str] = None  # e.g. "Entry", "TP1", "TP2" — where the SL was moved to
    sl_move_failed: bool = False  # True if a move_sl attempt failed — SL may be at wrong price
    pending_fill: bool = False  # True if placed as limit order and not yet filled on-chain
    pending_fill_since: Optional[float] = None  # timestamp when limit order was placed
    current_sl_key: Optional[str] = None  # hex key of the SL order we placed (to avoid cancelling it accidentally)
    original_size_usd: float = 0.0  # position size at open (before any partial TP closes)
    processed_tx_hashes: set = field(default_factory=set)  # dedup PositionDecrease events by tx_hash:log_index
    # Verified PositionDecrease events — single source of truth for TP hits
    # Each dict: {execution_price, net_pnl_usd, timestamp, tx_hash, log_index, size_delta_usd, matched_tp_price}
    verified_decreases: list = field(default_factory=list)

    @property
    def tp_hits_count(self) -> int:
        """Derived from verified on-chain PositionDecrease events."""
        return len(self.verified_decreases)

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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BOT ENGINE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GMXBot(NotificationsMixin, SLTPMixin, WalletMixin, PriceFeedsMixin, AnalyticsMixin, WithdrawMixin, CoreTelegramMixin):
    """Production GMX V2 Trading Bot with real on-chain execution."""

    def __init__(self):
        # Load configuration
        self.cfg = load_config()

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
        self.pending_collateral: Dict[int, Dict[str, Any]] = {}

        # Withdraw state: chat_id -> pending withdraw info
        self.pending_withdraw: Dict[int, Dict[str, Any]] = {}

        # Signal selection state: chat_id -> pending signal pick
        self.pending_signals: Dict[int, Dict[str, Any]] = {}

        # Last signal text for replay
        self.last_signal_text: Optional[str] = None

        # Retry queue for failed TP/SL order placements
        self.failed_order_queue: List[FailedOrder] = []

        # Signal store — persistent archive of all parsed signals
        self.signal_store = SignalStore()

        # Concurrency: prevent duplicate signal execution
        self._signal_lock = asyncio.Lock()
        self._recent_signal_hashes: Dict[str, float] = {}  # hash -> timestamp
        self._signal_dedup_window: float = 300.0  # 5 minutes

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
        self.order_retry_task: Optional[asyncio.Task] = None
        self.resolved_channels: Dict[int, str] = {}  # channel_id -> channel_name

        # Bot API polling state
        self._bot_api_chats: set = set()       # chat IDs from Bot API DMs
        self._bot_update_offset: int = 0       # getUpdates offset
        self.bot_polling_task: Optional[asyncio.Task] = None

        # Cooldown: after order placement, skip TP monitoring & reconciliation
        # to prevent false TP-hit detection from manual order changes.
        self._orders_cooldown_until: float = 0.0

        # 2-check guard: track positions missing from on-chain across check cycles.
        # Key = pos_id, value = consecutive miss count. Only close after 2 misses.
        self._position_missing_count: dict = {}

        self.setup_logging()

    def setup_logging(self):
        logging.basicConfig(
            level=getattr(logging, self.cfg.log_level, logging.INFO),
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler("gmx_bot.log"),
            ],
        )
        self.logger = logging.getLogger("GMXBot")

    def _set_orders_cooldown(self, seconds: float = 30.0):
        """Set a cooldown period after order placement.

        During cooldown, check_tp_hits skips all positions to avoid
        interpreting manual order changes as TP hits.
        """
        self._orders_cooldown_until = time.time() + seconds
        self.logger.info(f"Orders cooldown set for {seconds:.0f}s")

    def _in_orders_cooldown(self) -> bool:
        return time.time() < self._orders_cooldown_until

    def _find_internal_position(self, market_addr: str, is_long: bool, wallet_id: int = 0):
        """Find the internal Position matching a market+side+wallet."""
        market_lower = market_addr.lower()
        side = "LONG" if is_long else "SHORT"
        for pos in self.positions.values():
            if (pos.is_open and pos.market_addr
                    and pos.market_addr.lower() == market_lower
                    and pos.side == side
                    and (wallet_id == 0 or pos.wallet_id == wallet_id)):
                return pos
        return None

    # ──────────────────────────────────────────────────────────────────────
    # Position state persistence (realized PnL survives restarts)
    # ──────────────────────────────────────────────────────────────────────
    POSITION_STATE_FILE = "position_state.json"

    def _save_position_state(self):
        """Persist realized PnL and TP hit state for all open positions."""
        # Load existing state to preserve original_take_profits from first save
        existing_state = self._load_position_state()

        state = {}
        for pos in self.positions.values():
            if not pos.is_open or not pos.market_addr:
                continue
            key = f"{pos.wallet_id}:{pos.market_addr.lower()}:{pos.side}"

            # Preserve original TPs from first save — never overwrite
            existing_entry = existing_state.get(key, {})
            preserved_original_tps = existing_entry.get("original_take_profits")

            # Sanitize original_take_profits to heal corrupted state.
            # If preserved list is corrupted (has price<=0 entries or more entries
            # than expected), discard it and use current pos.take_profits instead.
            current_tps = [
                {"price": tp.price, "close_pct": tp.percentage}
                for tp in pos.take_profits if tp.price > 0
            ]
            if preserved_original_tps:
                cleaned = [tp for tp in preserved_original_tps if tp.get("price", 0) > 0]
                if len(pos.take_profits) > 0 and len(cleaned) > len(pos.take_profits):
                    # Corrupted — too many entries, discard preserved and use current
                    sanitized_tps = current_tps
                else:
                    sanitized_tps = cleaned if cleaned else current_tps
            else:
                sanitized_tps = current_tps

            state[key] = {
                "realized_pnl": pos.realized_pnl,
                "original_size_usd": pos.original_size_usd or pos.size_usd,
                "expected_tp_count": len(pos.take_profits),
                "sl_move_label": pos.sl_move_label,
                "sl_moved_to_entry": pos.sl_moved_to_entry,
                "opened_at": pos.opened_at,
                "original_take_profits": sanitized_tps,
                "entry_price": pos.entry_price,
                "stop_loss": pos.stop_loss,
                "leverage": pos.leverage,
                "processed_tx_hashes": list(pos.processed_tx_hashes),
                "verified_decreases": pos.verified_decreases,
                "signal_id": pos.signal_id,
                "tp_tx_hashes": list(pos.tp_tx_hashes),
                "sl_tx_hash": pos.sl_tx_hash,
                "order_history": pos.order_history,
            }
        try:
            atomic_json_write(self.POSITION_STATE_FILE, state)
        except Exception as e:
            self.logger.warning(f"Failed to save position state: {e}")

    def _load_position_state(self) -> dict:
        """Load persisted position state from disk. Returns dict keyed by composite key."""
        return safe_json_read(self.POSITION_STATE_FILE, default={})

    def _clear_position_state(self, pos):
        """Remove a closed position's entry from the state file."""
        if not pos.market_addr:
            return
        key = f"{pos.wallet_id}:{pos.market_addr.lower()}:{pos.side}"
        try:
            state = safe_json_read(self.POSITION_STATE_FILE, default={})
            if key in state:
                del state[key]
                atomic_json_write(self.POSITION_STATE_FILE, state)
        except Exception as e:
            self.logger.warning(f"Failed to clear position state: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # Failed order queue persistence
    # ──────────────────────────────────────────────────────────────────────
    FAILED_ORDERS_FILE = "failed_orders.json"

    def _save_failed_orders(self):
        """Persist the failed order queue to disk (atomic write)."""
        from dataclasses import asdict
        data = []
        for order in self.failed_order_queue:
            d = {
                "position_id": order.position_id,
                "symbol": order.symbol,
                "side": order.side,
                "market_addr": order.market_addr,
                "wallet_id": order.wallet_id,
                "order_kind": order.order_kind,
                "price": order.price,
                "size_usd": order.size_usd,
                "close_pct": order.close_pct,
                "is_long": order.is_long,
                "attempts": order.attempts,
                "max_attempts": order.max_attempts,
                "last_attempt": order.last_attempt,
                "error": order.error,
                "created_at": order.created_at,
            }
            data.append(d)
        try:
            atomic_json_write(self.FAILED_ORDERS_FILE, data)
        except Exception as e:
            self.logger.warning(f"Failed to save failed orders: {e}")

    def _load_failed_orders(self) -> List[FailedOrder]:
        """Load failed orders from disk on startup."""
        data = safe_json_read(self.FAILED_ORDERS_FILE, default=[])
        orders = []
        for d in data:
            try:
                orders.append(FailedOrder(**d))
            except Exception as e:
                self.logger.warning(f"Skipping corrupt failed order entry: {e}")
        return orders

    # ──────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────
    async def start(self):
        self.logger.info("Starting GMX V2 Trading Bot")
        await self.init_telegram()
        self.init_web3()

        # Load persisted trade history (PnL / win rate data)
        self._load_trade_history()

        # Sync on-chain positions into internal tracking (survives reboots)
        # skip_sl_check=True: SL is already correct on-chain, don't infer & move
        await self._sync_on_chain_positions(skip_sl_check=True)

        # Load persisted failed orders for retry
        self.failed_order_queue = self._load_failed_orders()
        if self.failed_order_queue:
            self.logger.info(f"Loaded {len(self.failed_order_queue)} failed orders from disk for retry")

        # Startup TP catch-up: extended lookback to catch events missed while offline
        try:
            await self.check_tp_hits(lookback_override=3600)  # 1 hour
            self.logger.info("Startup TP catch-up check completed")
        except Exception as e:
            self.logger.warning(f"Startup TP catch-up failed: {e}")

        # Verify SL orders exist for all synced positions
        for pos in list(self.positions.values()):
            if not pos.is_open or not pos.market_addr or not pos.stop_loss:
                continue
            try:
                acct = self._get_account(pos.wallet_id)
                orders = await asyncio.to_thread(fetch_open_orders, self.w3, acct.address)
                market_lower = pos.market_addr.lower()
                sl_orders = [
                    o for o in orders
                    if o["market"].lower() == market_lower
                    and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
                ]
                if not sl_orders:
                    self.logger.warning(
                        f"STARTUP: {pos.symbol} {pos.side} [W{pos.wallet_id}] has no on-chain SL "
                        f"(expected @ ${pos.stop_loss:,.0f})"
                    )
                    await self.notify(
                        f"⚠️ WARNING: {pos.symbol} {pos.side} [W{pos.wallet_id}] has no SL on-chain! "
                        f"Expected SL @ ${pos.stop_loss:,.0f}. Use /sl to place one."
                    )
            except Exception as e:
                self.logger.warning(f"SL verification failed for {pos.symbol}: {e}")

        self.price_update_task = asyncio.create_task(self.price_update_loop())
        self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())
        self.tp_monitor_task = asyncio.create_task(self.tp_monitor_loop())
        self.daily_summary_task = asyncio.create_task(self.daily_summary_loop())
        self.rebalance_task = asyncio.create_task(self.rebalance_loop())
        self.order_retry_task = asyncio.create_task(self.order_retry_loop())
        self.gas_check_task = asyncio.create_task(self.gas_check_loop())
        self.hourly_pnl_task = asyncio.create_task(self.hourly_pnl_loop())

        # Bot API polling for DM commands
        if self.cfg.telegram_bot_token:
            self.bot_polling_task = asyncio.create_task(self.bot_api_polling_loop())

        # Record initial balance snapshot for 24h tracking
        try:
            initial_portfolio = await self._get_total_portfolio_value()
            self._save_balance_snapshot(initial_portfolio)
        except Exception as e:
            self.logger.debug(f"Initial balance snapshot failed: {e}")

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
        if self.order_retry_task:
            self.order_retry_task.cancel()
        if getattr(self, 'gas_check_task', None):
            self.gas_check_task.cancel()
        if getattr(self, 'hourly_pnl_task', None):
            self.hourly_pnl_task.cancel()
        if self.bot_polling_task:
            self.bot_polling_task.cancel()

        # Reconnect if needed so the offline message can be sent
        try:
            if self.client and self.cfg.notify_chat:
                if not self.client.is_connected():
                    await self.client.connect()
                await self.client.send_message(self.cfg.notify_chat, "🔴 Bot Offline")
                await self.client.disconnect()
        except Exception as e:
            self.logger.error(f"Failed to send offline notification: {e}")

        # Send shutdown notice via Bot API (works even if Telethon is down)
        try:
            await self.notify_admin(f"🔴 Bot Offline — {reason}")
        except Exception:
            pass

        self.logger.info("Bot shutdown complete")

    def init_web3(self):
        self.w3 = Web3(Web3.HTTPProvider(self.cfg.rpc_url))
        if self.cfg.private_key:
            self.account = Account.from_key(self.cfg.private_key)
            self.logger.info(f"Web3 on {self.cfg.network}, wallet 1 (swing): {self.account.address[:10]}...")
        else:
            self.logger.warning("No private key — read-only mode")
        if self.cfg.private_key_2:
            self.account2 = Account.from_key(self.cfg.private_key_2)
            self.logger.info(f"Wallet 2 (scalp): {self.account2.address[:10]}...")
        else:
            self.logger.info("No PRIVATE_KEY_2 — single wallet mode")
        if self.cfg.private_key_3:
            self.account3 = Account.from_key(self.cfg.private_key_3)
            self.logger.info(f"Wallet 3 (scalp): {self.account3.address[:10]}...")
        if self.cfg.private_key_4:
            self.account4 = Account.from_key(self.cfg.private_key_4)
            self.logger.info(f"Wallet 4 (scalp): {self.account4.address[:10]}...")

    async def _sync_on_chain_positions(self, *, skip_sl_check: bool = False):
        """Scan on-chain positions for all wallets, sync state, and clean stale entries.

        On restart this:
          1. Adds any on-chain positions not already tracked internally
          2. Infers executed TPs by comparing SL price vs entry/TP prices
          3. Triggers SL move if TPs were hit but SL is stale
          4. Removes internal positions that are no longer on-chain
        """
        MARKET_TO_SYMBOL = {v.lower(): k for k, v in self.cfg.markets.items()}
        saved_state = self._load_position_state()

        wallets = self._all_wallets()

        # Collect all on-chain positions keyed by (wallet_id, market_lower, side)
        on_chain_set = set()
        wallet_chain_data = {}  # wid -> (chain_positions, chain_orders)

        synced = 0
        for wid, acct in wallets:
            try:
                chain_positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                chain_orders = await asyncio.to_thread(
                    fetch_open_orders, self.w3, acct.address
                )
                wallet_chain_data[wid] = (chain_positions, chain_orders)
            except Exception as e:
                self.logger.warning(f"Sync: failed to fetch wallet {wid}: {e}")
                continue

            for cp in chain_positions:
                market_lower = cp.market.lower()
                symbol = MARKET_TO_SYMBOL.get(market_lower, cp.symbol)
                side = "LONG" if cp.is_long else "SHORT"
                on_chain_set.add((wid, market_lower, side))

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

                # Reconstruct TP levels from on-chain TP orders (LimitDecrease)
                tp_orders = [
                    o for o in chain_orders
                    if o["market"].lower() == market_lower
                    and o["order_type"] == ORDER_TYPE_LIMIT_DECREASE
                ]
                tp_count = len(tp_orders)

                take_profits = []
                if tp_orders and cp.size_usd > 0:
                    tp_orders_sorted = sorted(
                        tp_orders,
                        key=lambda o: o["trigger_price"],
                        reverse=(side == "SHORT"),
                    )
                    for o in tp_orders_sorted:
                        tp_size = o.get("size_usd", 0)
                        pct = tp_size / cp.size_usd if cp.size_usd > 0 else 0
                        take_profits.append(TakeProfitLevel(
                            price=o["trigger_price"],
                            percentage=pct,
                        ))

                # Reconstruct SL from on-chain StopLossDecrease orders
                sl_orders = [
                    o for o in chain_orders
                    if o["market"].lower() == market_lower
                    and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
                ]
                reconstructed_sl = sl_orders[-1]["trigger_price"] if sl_orders else None

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
                    stop_loss=reconstructed_sl,
                    market_addr=cp.market,
                    collateral_token=cp.collateral_token,
                    wallet_id=wid,
                    take_profits=take_profits,
                )

                # ── Reconstruct TP state: signal TPs → on-chain cross-check ──
                state_key = f"{wid}:{market_lower}:{side}"
                saved = saved_state.get(state_key)

                # Restore basic fields from hint
                if saved:
                    pos.original_size_usd = saved.get("original_size_usd", pos.size_usd)
                    pos.processed_tx_hashes = set(saved.get("processed_tx_hashes", []))
                    pos.verified_decreases = saved.get("verified_decreases", [])
                    if saved.get("opened_at"):
                        pos.opened_at = saved["opened_at"]
                    else:
                        # opened_at missing from old state format — assume at least 30 days
                        pos.opened_at = time.time() - 30 * 86400
                    if saved.get("entry_price"):
                        pos.entry_price = saved["entry_price"]
                    if saved.get("leverage"):
                        pos.leverage = saved["leverage"]
                    # Restore signal & order linkage fields
                    pos.signal_id = saved.get("signal_id")
                    pos.tp_tx_hashes = saved.get("tp_tx_hashes", [])
                    pos.sl_tx_hash = saved.get("sl_tx_hash")
                    pos.order_history = saved.get("order_history", [])

                # ── Path A: Original signal TPs available → ground truth ──
                original_tps = saved.get("original_take_profits") if saved else None
                reconstructed = False

                if original_tps:
                    # Filter out corrupted entries (price=0 or negative)
                    valid_tps = [tp for tp in original_tps if tp.get("price", 0) > 0]
                    saved_expected = saved.get("expected_tp_count", 0) if saved else 0

                    # If saved list has more entries than expected, state is corrupted
                    # (orphaned TPs were prepended before being saved) — discard and
                    # fall through to Path B which rebuilds from on-chain data.
                    if saved_expected > 0 and len(valid_tps) > saved_expected:
                        self.logger.warning(
                            f"Sync: {symbol} {side} [W{wid}] original_take_profits has "
                            f"{len(valid_tps)} entries but expected_tp_count={saved_expected} "
                            f"— discarding corrupted state, falling through to Path B"
                        )
                        original_tps = None

                if original_tps:
                    valid_tps = [tp for tp in original_tps if tp.get("price", 0) > 0]
                    # Rebuild full TP list from signal data
                    signal_tps = [
                        TakeProfitLevel(price=tp["price"], percentage=tp["close_pct"])
                        for tp in valid_tps
                    ]
                    pos.take_profits = signal_tps
                    # expected_tp_count removed — derive from len(take_profits)

                    # Build set of on-chain TP prices (still pending)
                    on_chain_tp_prices = set()
                    for otp in take_profits:
                        on_chain_tp_prices.add(round(otp.price, 2))

                    # Mark signal TPs that are still on-chain as NOT executed
                    # Mark signal TPs that are NOT on-chain as candidates for hit verification
                    missing_from_chain = []
                    for i, stp in enumerate(signal_tps):
                        # Check if this signal TP matches an on-chain TP order
                        matched_on_chain = any(
                            abs(stp.price - otp) / stp.price < 0.01
                            for otp in on_chain_tp_prices
                        ) if stp.price > 0 else False
                        if not matched_on_chain:
                            missing_from_chain.append((i, stp))

                    # Query PositionDecrease events to verify which missing TPs were actually hit
                    verified_hits = 0
                    total_realized = 0.0
                    try:
                        from history import fetch_recent_position_decreases
                        lookback = int(time.time() - pos.opened_at) + 300
                        lookback = max(lookback, 600)
                        lookback = min(lookback, 30 * 86400)
                        decreases = await asyncio.to_thread(
                            fetch_recent_position_decreases,
                            self.w3, acct.address, market_lower,
                            side == "LONG", lookback,
                        )
                    except Exception as e:
                        self.logger.warning(f"Sync: {symbol} event query failed: {e}")
                        decreases = []

                    # Match each missing TP to a PositionDecrease event
                    used_events = set()
                    verified_decrease_list = []
                    for idx, stp in missing_from_chain:
                        event_match = None
                        for j, evt in enumerate(decreases):
                            if j in used_events:
                                continue
                            exec_price = evt.get("execution_price", 0)
                            if exec_price and stp.price > 0:
                                if abs(exec_price - stp.price) / stp.price < 0.01:
                                    event_match = evt
                                    used_events.add(j)
                                    break

                        if event_match:
                            verified_decrease_list.append({
                                "execution_price": event_match.get("execution_price", 0),
                                "net_pnl_usd": event_match.get("net_pnl_usd", 0),
                                "timestamp": event_match.get("timestamp", pos.opened_at),
                                "tx_hash": event_match.get("tx_hash", ""),
                                "log_index": event_match.get("log_index", 0),
                                "size_delta_usd": event_match.get("size_delta_usd", 0),
                                "matched_tp_price": stp.price,
                            })
                            total_realized += event_match.get("net_pnl_usd", 0)
                        else:
                            self.logger.debug(
                                f"Sync: {symbol} TP ${stp.price:,.2f} missing from chain "
                                f"but no PositionDecrease event found — not marking as hit"
                            )

                    # Preserve saved verified_decreases if event query failed
                    # or returned fewer hits than what was already on disk
                    saved_vd = saved.get("verified_decreases", []) if saved else []
                    if len(verified_decrease_list) >= len(saved_vd) or decreases:
                        pos.verified_decreases = verified_decrease_list
                        pos.realized_pnl = total_realized
                    else:
                        pos.verified_decreases = saved_vd
                        pos.realized_pnl = saved.get("realized_pnl", 0.0)
                        self.logger.warning(
                            f"Sync: {symbol} {side} [W{wid}] event query returned "
                            f"{len(verified_decrease_list)} hits but saved state has "
                            f"{len(saved_vd)} — keeping saved state"
                        )
                    pos.original_size_usd = (
                        cp.size_usd + sum(
                            d["size_delta_usd"] for d in pos.verified_decreases
                        )
                    )

                    # Derive SL state from on-chain SL price
                    verified_tp_prices = {d["matched_tp_price"] for d in verified_decrease_list}
                    if reconstructed_sl and cp.entry_price:
                        entry = cp.entry_price
                        if entry > 0 and abs(reconstructed_sl - entry) / entry < 0.005:
                            pos.sl_moved_to_entry = True
                            pos.sl_move_label = "Entry"
                        else:
                            for i, stp in enumerate(signal_tps):
                                if stp.price in verified_tp_prices and stp.price > 0:
                                    if abs(reconstructed_sl - stp.price) / stp.price < 0.005:
                                        pos.sl_move_label = f"TP{i + 1}"
                                        pos.sl_moved_to_entry = True
                                        break

                    reconstructed = True
                    verified_hits = len(verified_decrease_list)
                    self.logger.info(
                        f"Sync: {symbol} {side} [W{wid}] cross-checked with signal: "
                        f"{len(signal_tps)} TPs, {verified_hits} verified hit(s), "
                        f"{len(missing_from_chain) - verified_hits} unverified, "
                        f"realized=${total_realized:,.2f}"
                    )

                # ── Path B: No original signal → verify via on-chain events ──
                if not reconstructed and saved:
                    on_chain_tp_prices_b = set()
                    for otp in take_profits:
                        on_chain_tp_prices_b.add(round(otp.price, 2))

                    # Always query PositionDecrease events — needed for both
                    # missing-TP verification AND orphaned-event detection
                    try:
                        from history import fetch_recent_position_decreases
                        lookback_b = int(time.time() - pos.opened_at) + 300
                        lookback_b = max(lookback_b, 600)
                        lookback_b = min(lookback_b, 30 * 86400)
                        decreases_b = await asyncio.to_thread(
                            fetch_recent_position_decreases,
                            self.w3, acct.address, market_lower,
                            side == "LONG", lookback_b,
                        )
                    except Exception as e:
                        self.logger.warning(f"Sync Path B: {symbol} event query failed: {e}")
                        decreases_b = []

                    # Step 1: Check if any internal TPs are missing from chain
                    missing_tps_b = []
                    for i, tp_lvl in enumerate(pos.take_profits):
                        matched = any(
                            abs(tp_lvl.price - otp) / tp_lvl.price < 0.01
                            for otp in on_chain_tp_prices_b
                        ) if tp_lvl.price > 0 else False
                        if not matched:
                            missing_tps_b.append((i, tp_lvl))

                    total_realized_b = 0.0
                    used_events_b = set()
                    verified_decrease_list_b = []

                    # Step 2: Verify missing TPs against decrease events
                    for idx, tp_lvl in missing_tps_b:
                        for j, evt in enumerate(decreases_b):
                            if j in used_events_b:
                                continue
                            exec_price = evt.get("execution_price", 0)
                            if exec_price and tp_lvl.price > 0:
                                if abs(exec_price - tp_lvl.price) / tp_lvl.price < 0.01:
                                    verified_decrease_list_b.append({
                                        "execution_price": exec_price,
                                        "net_pnl_usd": evt.get("net_pnl_usd", 0),
                                        "timestamp": evt.get("timestamp", pos.opened_at),
                                        "tx_hash": evt.get("tx_hash", ""),
                                        "log_index": evt.get("log_index", 0),
                                        "size_delta_usd": evt.get("size_delta_usd", 0),
                                        "matched_tp_price": tp_lvl.price,
                                    })
                                    total_realized_b += evt.get("net_pnl_usd", 0)
                                    used_events_b.add(j)
                                    break

                    # Sort TPs for consistent SL target resolution
                    if side == "LONG":
                        pos.take_profits.sort(key=lambda t: t.price)
                    else:
                        pos.take_profits.sort(key=lambda t: t.price, reverse=True)

                    # Preserve saved verified_decreases if event query failed
                    saved_vd_b = saved.get("verified_decreases", []) if saved else []
                    if len(verified_decrease_list_b) >= len(saved_vd_b) or decreases_b:
                        pos.verified_decreases = verified_decrease_list_b
                        pos.realized_pnl = total_realized_b
                    else:
                        pos.verified_decreases = saved_vd_b
                        pos.realized_pnl = saved.get("realized_pnl", 0.0)
                        self.logger.warning(
                            f"Sync Path B: {symbol} {side} [W{wid}] event query returned "
                            f"{len(verified_decrease_list_b)} hits but saved state has "
                            f"{len(saved_vd_b)} — keeping saved state"
                        )

                    # Derive SL state from on-chain SL price + verified hits
                    verified_tp_prices_b = {d["matched_tp_price"] for d in verified_decrease_list_b}
                    verified_hits_b = len(verified_decrease_list_b)
                    if reconstructed_sl and cp.entry_price and verified_hits_b > 0:
                        entry = cp.entry_price
                        if entry > 0 and abs(reconstructed_sl - entry) / entry < 0.005:
                            pos.sl_moved_to_entry = True
                            pos.sl_move_label = "Entry"
                        else:
                            for i, tp_lvl in enumerate(pos.take_profits):
                                if tp_lvl.price in verified_tp_prices_b and tp_lvl.price > 0:
                                    if abs(reconstructed_sl - tp_lvl.price) / tp_lvl.price < 0.005:
                                        pos.sl_move_label = f"TP{i + 1}"
                                        pos.sl_moved_to_entry = True
                                        break
                    elif saved.get("sl_move_label") and verified_hits_b > 0:
                        pos.sl_move_label = saved["sl_move_label"]
                        pos.sl_moved_to_entry = saved.get("sl_moved_to_entry", False)

                    self.logger.info(
                        f"Sync: {symbol} {side} [W{wid}] Path B event-verified: "
                        f"tp_hits={pos.tp_hits_count}, realized=${pos.realized_pnl:,.2f}"
                    )

                # Sanity: cap verified_decreases to total TPs
                if pos.tp_hits_count > len(pos.take_profits) and pos.take_profits:
                    self.logger.warning(
                        f"Sync: {symbol} {side} [W{wid}] verified_decreases={pos.tp_hits_count} "
                        f"> total TPs={len(pos.take_profits)} — trimming"
                    )
                    pos.verified_decreases = pos.verified_decreases[:len(pos.take_profits)]

                # Always use on-chain entry price (signal entry may differ from fill)
                if cp.entry_price and cp.entry_price > 0:
                    pos.entry_price = cp.entry_price

                self.positions[pos.id] = pos
                synced += 1
                tp_str = f", {tp_count} TPs" if take_profits else ", no TPs"
                sl_str = f", SL ${reconstructed_sl:,.2f}" if reconstructed_sl else ", no SL"
                hits_str = f", {pos.tp_hits_count} TP hit(s)" if pos.tp_hits_count else ""
                rlz_str = f", realized=${pos.realized_pnl:,.2f}" if pos.realized_pnl else ""
                self.logger.info(
                    f"Synced on-chain position: {symbol} {side} "
                    f"${cp.size_usd:,.2f} @ {cp.leverage:.1f}x [W{wid}]{tp_str}{sl_str}{hits_str}{rlz_str}"
                )

        # ── Clean up stale internal positions no longer on-chain ──
        stale_count = 0
        for pos_id, pos in list(self.positions.items()):
            if not pos.is_open or not pos.market_addr:
                continue
            key = (pos.wallet_id, pos.market_addr.lower(), pos.side)
            # If we successfully fetched this wallet's data but the position isn't there
            if pos.wallet_id in wallet_chain_data and key not in on_chain_set:
                self.logger.info(
                    f"Sync: {pos.symbol} {pos.side} [W{pos.wallet_id}] no longer on-chain — marking closed"
                )
                pos.is_open = False
                pos.closed_at = time.time()
                pos.exit_reason = "closed_while_offline"
                await self._record_trade(pos, exit_reason="closed_while_offline")
                self._clear_position_state(pos)
                stale_count += 1

                # Cancel any orphaned orders for this market
                try:
                    _, chain_orders = wallet_chain_data[pos.wallet_id]
                    orphaned = [
                        o for o in chain_orders
                        if o["market"].lower() == pos.market_addr.lower()
                        and o["is_long"] == (pos.side == "LONG")
                    ]
                    if orphaned:
                        acct = self._get_account(pos.wallet_id)
                        exchange = self.w3.eth.contract(
                            address=Web3.to_checksum_address(self.cfg.exchange_router),
                            abi=EXCHANGE_ROUTER_ABI,
                        )
                        for o in orphaned:
                            if o.get("key_hex"):
                                try:
                                    key_bytes = bytes.fromhex(o["key_hex"])
                                    data = exchange.encode_abi("cancelOrder", [key_bytes])
                                    txh = await asyncio.to_thread(
                                        self._send_tx, exchange.address, data, 0, acct
                                    )
                                    self.logger.info(
                                        f"Sync: cancelled orphaned order for closed {pos.symbol} "
                                        f"{pos.side}: {o.get('order_type_name', 'order')}"
                                    )
                                except Exception as e:
                                    self.logger.warning(f"Sync: failed to cancel orphaned order: {e}")
                except Exception as e:
                    self.logger.warning(f"Sync: orphan cleanup error: {e}")

        if stale_count:
            await self.notify(
                f"🧹 Startup cleanup: {stale_count} position(s) were closed while bot was offline"
            )

        if synced:
            self.logger.info(f"Synced {synced} on-chain position(s) into tracking")
        else:
            self.logger.info("No untracked on-chain positions found")

        # ── Post-sync: verify SL is at the correct level for inferred TP hits ──
        # Only on startup — skip when user runs /sync (SL is already on-chain)
        if skip_sl_check:
            return

        for pos in self.positions.values():
            if not pos.is_open or pos.tp_hits_count == 0 or not pos.take_profits:
                continue
            if pos.wallet_id not in wallet_chain_data:
                continue

            sorted_tps = sorted(
                pos.take_profits,
                key=lambda t: t.price,
                reverse=(pos.side == "SHORT"),
            )
            target_sl, target_label = determine_new_sl_target(
                pos.tp_hits_count, pos.entry_price, sorted_tps,
                leverage=pos.leverage,
            )

            # None means no SL move for this TP hit count (trailing strategy: TP2 stays at Entry)
            if target_sl is None:
                continue

            # Check if current SL is already correct
            tolerance = pos.entry_price * 0.003
            sl_correct = (
                pos.stop_loss is not None
                and abs(pos.stop_loss - target_sl) < tolerance
            )
            if sl_correct:
                continue

            self.logger.info(
                f"Sync: {pos.symbol} {pos.side} [W{pos.wallet_id}] SL stale after "
                f"{pos.tp_hits_count} TP hit(s) — should be at {target_label} "
                f"(${target_sl:,.2f}), currently ${pos.stop_loss or 0:,.2f}"
            )
            try:
                acct = self._get_account(pos.wallet_id)
                fresh_orders = await asyncio.to_thread(
                    fetch_open_orders, self.w3, acct.address
                )
                await self.move_sl(pos, fresh_orders, target_sl, target_label)
                await self.notify(
                    f"🔧 Startup SL fix: {pos.symbol} {pos.side} [W{pos.wallet_id}] "
                    f"SL moved to {target_label} (${target_sl:,.2f}) after "
                    f"{pos.tp_hits_count} TP hit(s) detected"
                )
            except Exception as e:
                self.logger.error(f"Sync: failed to move SL for {pos.symbol}: {e}")
                await self.notify(
                    f"⚠️ Startup SL fix FAILED: {pos.symbol} {pos.side} [W{pos.wallet_id}] "
                    f"— manual /sl needed. Error: {e}"
                )

    # ──────────────────────────────────────────────────────────────────────
    # Failed Order Retry Queue
    # ──────────────────────────────────────────────────────────────────────

    async def order_retry_loop(self):
        """Background loop that retries failed TP/SL placements every 30 seconds."""
        while True:
            try:
                await asyncio.sleep(30)
                await self.process_retry_queue()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Order retry loop error: {e}")

    async def process_retry_queue(self):
        """Process the failed order queue — retry each pending order once per cycle."""
        if not self.failed_order_queue:
            return

        still_pending: List[FailedOrder] = []

        for order in self.failed_order_queue:
            # Skip if the parent position is no longer open
            pos = self.positions.get(order.position_id)
            if not pos or not pos.is_open:
                self.logger.info(
                    f"Retry queue: dropping {order.order_kind.upper()} "
                    f"@ ${order.price:,.2f} for {order.symbol} — position closed"
                )
                continue

            # Skip if max attempts reached
            if order.attempts >= order.max_attempts:
                self.logger.warning(
                    f"Retry queue: {order.order_kind.upper()} @ ${order.price:,.2f} "
                    f"for {order.symbol} FAILED after {order.attempts} attempts: {order.error}"
                )
                await self.notify(
                    f"❌ {order.symbol} {order.side} [W{order.wallet_id}]: "
                    f"{order.order_kind.upper()} @ ${order.price:,.2f} permanently failed "
                    f"after {order.attempts} attempts.\n"
                    f"Last error: {order.error}\n"
                    f"Use /addorder to manually place this order."
                )
                continue

            # Attempt retry
            order.attempts += 1
            order.last_attempt = time.time()

            try:
                acct = self._get_account(order.wallet_id)
                cfg = self.cfg
                exchange = self.w3.eth.contract(
                    address=Web3.to_checksum_address(cfg.exchange_router),
                    abi=EXCHANGE_ROUTER_ABI,
                )
                collateral_token = Web3.to_checksum_address(cfg.collateral_token)
                order_vault = Web3.to_checksum_address(cfg.order_vault)

                if order.order_kind == "tp":
                    tp = TakeProfit(price=order.price, close_pct=order.close_pct)
                    retry_collateral = pos.size_usd / pos.leverage if pos.leverage else pos.size_usd
                    txh = await asyncio.to_thread(
                        create_tp_order,
                        self.w3, acct, exchange, acct.address,
                        order.market_addr, collateral_token, order_vault,
                        tp, order.size_usd, retry_collateral,
                        order.symbol, order.is_long,
                        cfg.slippage_bps, cfg.execution_fee_wei, cfg.dry_run,
                    )
                    self.logger.info(
                        f"Retry SUCCESS: TP @ ${order.price:,.2f} for "
                        f"{order.symbol} {order.side} [W{order.wallet_id}] "
                        f"(attempt {order.attempts}): {txh}"
                    )
                    # Add back to position tracking
                    pos.take_profits.append(
                        TakeProfitLevel(price=order.price, percentage=order.close_pct)
                    )
                    pos.tp_tx_hashes.append(txh)
                    pos.order_history.append({
                        "order_type": "tp_retry",
                        "tx_hash": txh,
                        "price": order.price,
                        "status": "placed",
                        "timestamp": time.time(),
                        "attempt": order.attempts,
                    })
                    self._set_orders_cooldown(30)
                    await self.notify(
                        f"✅ Retry succeeded: {order.symbol} {order.side} [W{order.wallet_id}] "
                        f"TP @ ${order.price:,.2f} placed (attempt {order.attempts})\n"
                        f"TX: {txh}"
                    )

                elif order.order_kind == "sl":
                    txh = await asyncio.to_thread(
                        create_sl_order,
                        self.w3, acct, exchange, acct.address,
                        order.market_addr, collateral_token, order_vault,
                        order.price, order.size_usd, order.symbol, order.is_long,
                        cfg.slippage_bps, cfg.execution_fee_wei, cfg.dry_run,
                    )
                    self.logger.info(
                        f"Retry SUCCESS: SL @ ${order.price:,.2f} for "
                        f"{order.symbol} {order.side} [W{order.wallet_id}] "
                        f"(attempt {order.attempts}): {txh}"
                    )
                    pos.stop_loss = order.price
                    pos.sl_tx_hash = txh
                    pos.order_history.append({
                        "order_type": "sl_retry",
                        "tx_hash": txh,
                        "price": order.price,
                        "status": "placed",
                        "timestamp": time.time(),
                        "attempt": order.attempts,
                    })
                    await self.notify(
                        f"✅ Retry succeeded: {order.symbol} {order.side} [W{order.wallet_id}] "
                        f"SL @ ${order.price:,.2f} placed (attempt {order.attempts})\n"
                        f"TX: {txh}"
                    )

                # Success — don't re-add to queue
                self._save_position_state()
                continue

            except RuntimeError as e:
                # Transaction reverted on-chain — don't retry, it will keep reverting
                order.error = f"REVERT: {e}"
                order.attempts = order.max_attempts  # force permanent failure
                self.logger.error(
                    f"Retry PERMANENT FAIL (revert): {order.order_kind.upper()} "
                    f"@ ${order.price:,.2f} ({order.symbol}): {e}"
                )
                still_pending.append(order)
            except (ConnectionError, TimeoutError, OSError) as e:
                # Retryable network error
                order.error = f"RPC: {e}"
                self.logger.warning(
                    f"Retry attempt {order.attempts}/{order.max_attempts} "
                    f"(network error) for {order.order_kind.upper()} "
                    f"@ ${order.price:,.2f} ({order.symbol}): {e}"
                )
                still_pending.append(order)
            except Exception as e:
                err_str = str(e).lower()
                if any(kw in err_str for kw in ["nonce too low", "replacement", "already known"]):
                    order.error = f"NONCE: {e}"
                    self.logger.warning(f"Nonce error on retry, will retry with fresh nonce: {e}")
                else:
                    order.error = str(e)
                    self.logger.error(
                        f"Retry attempt {order.attempts}/{order.max_attempts} "
                        f"for {order.order_kind.upper()} @ ${order.price:,.2f} "
                        f"({order.symbol}): {e}"
                    )
                still_pending.append(order)

        self.failed_order_queue = still_pending
        self._save_failed_orders()

    async def _resync_tp_orders(self, chat_id: int):
        """Cancel and re-place all TP orders with proportional collateral withdrawal.

        Old TP orders used collateral_delta=0 which causes leverage to drop
        when TPs execute. New orders withdraw proportional collateral so
        leverage stays constant on the remaining position.
        """
        cfg = self.cfg
        exchange = self.w3.eth.contract(
            address=Web3.to_checksum_address(cfg.exchange_router),
            abi=EXCHANGE_ROUTER_ABI,
        )
        collateral_token = Web3.to_checksum_address(cfg.collateral_token)
        order_vault = Web3.to_checksum_address(cfg.order_vault)

        total_cancelled = 0
        total_placed = 0

        for pos in list(self.positions.values()):
            if not pos.is_open or not pos.market_addr:
                continue

            acct = self._get_account(pos.wallet_id)
            market_lower = pos.market_addr.lower()
            is_long = pos.side == "LONG"

            try:
                orders = await asyncio.to_thread(
                    fetch_open_orders, self.w3, acct.address
                )
            except Exception as e:
                self.logger.warning(f"Resync TPs: failed to fetch orders for W{pos.wallet_id}: {e}")
                continue

            # Find existing TP orders for this position
            tp_orders = [
                o for o in orders
                if o["market"].lower() == market_lower
                and o["order_type"] == ORDER_TYPE_LIMIT_DECREASE
                and o.get("is_long") == is_long
            ]

            if not tp_orders:
                continue

            # Cancel old TP orders
            cancelled = 0
            for o in tp_orders:
                key_hex = o.get("key_hex")
                if not key_hex:
                    continue
                try:
                    key_bytes = bytes.fromhex(key_hex)
                    data = exchange.encode_abi("cancelOrder", [key_bytes])
                    txh = await asyncio.to_thread(
                        self._send_tx, exchange.address, data, 0, acct
                    )
                    cancelled += 1
                    self.logger.info(f"Resync TPs: cancelled old TP for {pos.symbol}: {txh}")
                except Exception as e:
                    self.logger.warning(f"Resync TPs: cancel failed for {pos.symbol}: {e}")
            total_cancelled += cancelled

            if cancelled == 0:
                continue

            # Small delay for chain state to settle
            await asyncio.sleep(2)

            # Re-place TPs using current on-chain size and collateral
            collateral_usd = pos.size_usd / pos.leverage if pos.leverage else pos.size_usd
            placed = 0
            # Safety: if a TP has a verified decrease but its order was still
            # on-chain (just cancelled), remove the stale verified decrease
            on_chain_tp_prices_resync = {
                round(o["trigger_price"], 2) for o in tp_orders
            }
            pos.verified_decreases = [
                d for d in pos.verified_decreases
                if not any(
                    abs(d.get("matched_tp_price", 0) - ocp) / ocp < 0.01
                    for ocp in on_chain_tp_prices_resync
                    if ocp > 0
                )
            ]

            # Re-place TPs that are NOT verified as hit
            verified_tp_prices_resync = {
                d.get("matched_tp_price") for d in pos.verified_decreases
                if d.get("matched_tp_price") is not None
            }
            remaining_tps = [
                tp for tp in pos.take_profits
                if not any(
                    abs(tp.price - vtp) / tp.price < 0.01
                    for vtp in verified_tp_prices_resync
                    if tp.price > 0
                )
            ]
            for tp_level in remaining_tps:
                tp_obj = TakeProfit(price=tp_level.price, close_pct=tp_level.percentage)
                try:
                    txh = await asyncio.to_thread(
                        create_tp_order,
                        self.w3, acct, exchange, acct.address,
                        pos.market_addr, collateral_token, order_vault,
                        tp_obj, pos.size_usd, collateral_usd,
                        pos.symbol, is_long,
                        cfg.slippage_bps, cfg.execution_fee_wei, cfg.dry_run,
                    )
                    placed += 1
                    self.logger.info(f"Resync TPs: placed TP @ ${tp_level.price:,.2f} for {pos.symbol}: {txh}")
                    if not cfg.dry_run:
                        await asyncio.sleep(2)
                except Exception as e:
                    self.logger.warning(f"Resync TPs: place failed for {pos.symbol} TP @ ${tp_level.price:,.2f}: {e}")
            total_placed += placed

        if total_cancelled > 0 or total_placed > 0:
            await self.send_message(
                chat_id,
                f"TP orders updated: cancelled {total_cancelled}, re-placed {total_placed}\n"
                f"New TPs will withdraw proportional collateral to maintain leverage."
            )

    # ──────────────────────────────────────────────────────────────────────
    # Signal Processing
    # ──────────────────────────────────────────────────────────────────────
    async def process_signal(self, text: str):
        """Process a trading signal from a Telegram channel.

        Protected by _signal_lock to prevent concurrent duplicate execution,
        and a dedup check to skip identical signals within 30 seconds.
        """
        async with self._signal_lock:
            await self._process_signal_inner(text)

    async def _process_signal_inner(self, text: str):
        """Inner signal processing (called under _signal_lock)."""
        try:
            self.health_stats["signals_processed"] += 1

            if self.is_halted:
                self.logger.info("Signal ignored — bot is halted")
                return

            if not text or len(text) < 10:
                return

            # Skip update / status messages (e.g. TP hit, SL triggered, etc.)
            # These are channel updates about existing positions, NOT new signals.
            # Route through channel TP confirmation as a safety-net fallback.
            if is_update_message(text):
                self.logger.debug(f"Update message detected: {text[:80]}")
                try:
                    await self.check_channel_tp_confirmation(text)
                except Exception as e:
                    self.logger.debug(f"Channel TP confirmation check failed: {e}")
                return

            # Try to parse with open.py's robust parser
            try:
                signal = parse_signal(text)
            except (ValueError, Exception) as e:
                self.logger.debug(f"Could not parse signal: {e}")
                return

            # Record signal in persistent store (before any filtering)
            signal_id = self.signal_store.record_signal(signal, text)

            # Only trade BTC, ETH, SOL
            if signal.symbol not in ALLOWED_SYMBOLS:
                self.logger.debug(f"Ignored signal for {signal.symbol} — not in allowed pairs (BTC/ETH/SOL)")
                self.signal_store.mark_rejected(signal_id, f"{signal.symbol} not in allowed pairs")
                return

            # Dedup: skip only if an identical signal (same symbol, side, leverage, TP levels)
            # was processed within the dedup window.  Different leverage or TP levels = new trade.
            tp_prices = tuple(sorted(tp.price for tp in signal.take_profits))
            sig_fingerprint = f"{signal.symbol}|{signal.side}|{signal.leverage}|{tp_prices}"
            sig_hash = hashlib.md5(sig_fingerprint.encode()).hexdigest()
            now = time.time()

            # Purge expired entries
            self._recent_signal_hashes = {
                h: t for h, t in self._recent_signal_hashes.items()
                if now - t < self._signal_dedup_window
            }

            if sig_hash in self._recent_signal_hashes:
                elapsed = now - self._recent_signal_hashes[sig_hash]
                self.logger.info(f"Exact duplicate signal ignored (same leverage+TPs {elapsed:.0f}s ago, window={self._signal_dedup_window:.0f}s)")
                self.signal_store.mark_rejected(signal_id, "exact duplicate signal")
                return
            self._recent_signal_hashes[sig_hash] = now

            # Store for /lastsignal replay
            self.last_signal_text = text

            self.logger.info(f"Signal parsed: {signal.symbol} {signal.side} [{signal.trade_type}] (sig={signal_id[:8]})")

            # If signal has no explicit entry, use current market price
            if signal.entry_mid == 0:
                try:
                    mkt_price = await asyncio.to_thread(fetch_current_price, signal.symbol, self.w3)
                    signal.entry_low = signal.entry_high = mkt_price
                    self.logger.info(f"No entry in signal — using market price ${mkt_price:,.2f}")
                except Exception as e:
                    self.logger.warning(f"Could not fetch market price for missing entry: {e}")
                    self.signal_store.mark_rejected(signal_id, "no entry price and market price unavailable")
                    return

            # Validation
            if not await self.validate_signal(signal):
                self.signal_store.mark_rejected(signal_id, "validation failed")
                return

            # Pick wallet based on trade type:
            #   swing → W1 only
            #   scalp → W2, W3, W4 (first available)
            wallet_id, acct = await self._pick_wallet(signal.symbol, signal.trade_type, is_long=signal.is_long)
            if not acct:
                reason = getattr(self, '_last_wallet_reject_reason', 'unknown')
                self.signal_store.mark_rejected(signal_id, f"no available wallets: {reason}")
                await self.notify(
                    f"Rejected {signal.symbol} {signal.side} [{signal.trade_type}]: "
                    f"no available wallets\n{reason}"
                )
                return

            wallet_label = f" [W{wallet_id}]" if len(self._all_wallets()) > 1 else ""
            type_label = signal.trade_type.upper()

            # Determine collateral based on number of open trades:
            #   0-1 open: portfolio_pct of TOTAL portfolio (free USDC + deployed + PnL)
            #   2+  open: portfolio_pct of FREE USDC only (avoid over-deploying)
            total_portfolio = await self._get_total_portfolio_value()
            free_usdc = await self._get_combined_usdc()
            open_count = sum(1 for p in self.positions.values() if p.is_open)

            if total_portfolio <= 0:
                self.signal_store.mark_rejected(signal_id, "portfolio value is $0")
                await self.notify(f"Rejected {signal.symbol}: total portfolio value is $0")
                return

            # Cap leverage at max_leverage first so collateral calculation is correct
            signal.leverage = cap_leverage(signal.leverage, self.cfg.max_leverage)

            if open_count >= self.cfg.free_balance_after:
                # Size from free USDC only — don't count deployed collateral
                sizing_base = free_usdc
                sizing_label = f"free USDC (${free_usdc:,.0f}, {open_count} open trades)"
            else:
                sizing_base = total_portfolio
                sizing_label = f"total portfolio (${total_portfolio:,.0f})"

            collateral_usd = sizing_base * self.cfg.portfolio_pct
            size_usd = collateral_usd * signal.leverage

            min_collateral_err = check_min_collateral(
                collateral_usd, self.cfg.min_position_usd, self.cfg.portfolio_pct, total_portfolio
            )
            if min_collateral_err:
                self.signal_store.mark_rejected(signal_id, min_collateral_err)
                await self.notify(f"Rejected {signal.symbol}: {min_collateral_err}")
                return

            # Notify that we're executing
            tp_list = ", ".join(f"${tp.price:,.0f} ({tp.close_pct:.0%})" for tp in signal.take_profits)
            await self.notify(
                f"Executing {signal.symbol} {signal.side}{wallet_label} [{type_label}]\n"
                f"Entry: ${signal.entry_low:,.0f}-${signal.entry_high:,.0f}\n"
                f"TP: {tp_list}\n"
                f"SL: ${signal.stop_loss:,.0f}\n"
                f"Portfolio: ${total_portfolio:.0f} (free: ${free_usdc:.0f} + deployed)\n"
                f"Sizing: {self.cfg.portfolio_pct:.0%} of {sizing_label}\n"
                f"Collateral: ${collateral_usd:.0f}\n"
                f"Size: ${size_usd:.0f} @ {signal.leverage:.0f}x\n"
                f"Mode: {'DRY RUN' if self.cfg.dry_run else 'LIVE'}"
            )

            # Check that the selected wallet has enough USDC for the collateral.
            # If not, pull USDC from other wallets directly into this one.
            wallet_usdc = await asyncio.to_thread(self._get_portfolio_value_for, acct)
            required_collateral = size_usd / signal.leverage if signal.leverage else size_usd
            if wallet_usdc < required_collateral:
                shortfall = required_collateral - wallet_usdc
                self.logger.warning(
                    f"W{wallet_id} has ${wallet_usdc:.2f} USDC but needs "
                    f"${required_collateral:.2f} collateral — auto-funding ${shortfall:.2f}"
                )
                await self.notify(
                    f"⚠️ W{wallet_id} low: ${wallet_usdc:.2f} USDC "
                    f"(need ${required_collateral:.2f}) — pulling from other wallets..."
                )
                await self._fund_wallet(wallet_id, shortfall)

                # Re-check after funding
                wallet_usdc = await asyncio.to_thread(self._get_portfolio_value_for, acct)
                if wallet_usdc < required_collateral:
                    await self.notify(
                        f"Rejected {signal.symbol} {signal.side}: W{wallet_id} still only "
                        f"${wallet_usdc:.2f} USDC after auto-fund (need ${required_collateral:.2f})"
                    )
                    return

            # Final safety check: verify the SELECTED wallet doesn't already
            # have an on-chain position for this market+side (guards against
            # race conditions where _pick_wallet passed but the position
            # appeared on-chain before we execute).
            market_addr = self.cfg.markets.get(signal.symbol, "").lower()
            if market_addr:
                try:
                    chain_positions = await asyncio.to_thread(
                        chain_fetch_positions, self.w3, acct.address
                    )
                    for cp in chain_positions:
                        if cp.market.lower() == market_addr and cp.is_long == signal.is_long:
                            self.logger.warning(
                                f"Duplicate blocked: {signal.symbol} {signal.side} already open "
                                f"on W{wallet_id} ({acct.address[:10]}...)"
                            )
                            self.signal_store.mark_rejected(signal_id, f"duplicate on W{wallet_id}")
                            await self.notify(
                                f"Blocked duplicate {signal.symbol} {signal.side}: "
                                f"already open on W{wallet_id}"
                            )
                            return
                except Exception as e:
                    self.logger.warning(f"Could not check W{wallet_id} for duplicates: {e}")

            # Execute on-chain with the selected wallet
            position, order_type = await self.execute_open(signal, size_usd, acct, collateral_usd=collateral_usd, wallet_id=wallet_id)
            if position:
                position.signal_id = signal_id
                self.signal_store.mark_executed(signal_id, position.id, wallet_id)
                self.positions[position.id] = position
                self._save_position_state()
                self.health_stats["trades_executed"] += 1
                await self.notify_position_opened(position, order_type)
                # Top up ETH for gas if balance is low
                await self.topup_eth_if_needed()
                # Rebalance USDC between wallets after opening
                await self._rebalance_wallets()

        except Exception as e:
            self.logger.error(f"Error processing signal: {e}\n{traceback.format_exc()}")
            self.health_stats["errors"] += 1
            await self.notify(f"⚠️ Error processing signal: {e}")

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
            market_addr = self.cfg.markets.get(pos.symbol)
            if not market_addr:
                self.logger.error(f"No market address configured for {pos.symbol} — cannot close override")
                return False
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
                        address=Web3.to_checksum_address(self.cfg.exchange_router),
                        abi=EXCHANGE_ROUTER_ABI,
                    )
                    await asyncio.to_thread(
                        cancel_orders_for_market, self.w3, pos_acct, exchange, market_addr, self.cfg.dry_run
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
                    address=Web3.to_checksum_address(self.cfg.exchange_router),
                    abi=EXCHANGE_ROUTER_ABI,
                )
                n_cancelled = await asyncio.to_thread(
                    cancel_orders_for_market, self.w3, pos_acct, exchange, gmx_pos.market, self.cfg.dry_run
                )
                if n_cancelled:
                    self.logger.info(f"Cancelled {n_cancelled} orders for {pos.symbol} override")
            except Exception as e:
                self.logger.error(f"Failed to cancel orders during override: {e}")

            # Mark internal position as closed
            await self._record_trade(pos, exit_reason="override")
            self._clear_position_state(pos)
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
        sl_err = validate_sl_required(signal.stop_loss, self.cfg.require_sl)
        if sl_err:
            self.logger.warning(f"Rejected: {sl_err}")
            await self.notify(f"Rejected {signal.symbol} {signal.side}: {sl_err}")
            return False

        # Require TP
        tp_err = validate_tp_required(signal.take_profits, self.cfg.require_tp)
        if tp_err:
            self.logger.warning(f"Rejected: {tp_err}")
            await self.notify(f"Rejected {signal.symbol} {signal.side}: {tp_err}")
            return False

        # NOTE: We no longer reject based on existing positions for the same
        # symbol+side here.  _pick_wallet() handles routing to a free wallet
        # and rejects only when ALL eligible wallets are occupied.

        # Price deviation check — reject if too far from signal entry
        try:
            current_price = await asyncio.to_thread(fetch_current_price, signal.symbol, self.w3)
            entry_avg = signal.entry_mid
            should_reject, deviation = check_price_deviation(
                current_price, entry_avg, self.cfg.max_price_deviation
            )
            if should_reject:
                self.logger.warning(
                    f"Price deviation {deviation:.1%} > {self.cfg.max_price_deviation:.0%} "
                    f"(current: ${current_price:,.0f}, entry: ${entry_avg:,.0f})"
                )
                await self.notify(
                    f"Rejected {signal.symbol}: price deviation {deviation:.1%} (>{self.cfg.max_price_deviation:.0%})\n"
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
        sl_tp_err = validate_sl_tp_direction(
            signal.is_long,
            signal.stop_loss,
            signal.entry_low,
            signal.entry_high,
            signal.take_profits,
        )
        if sl_tp_err:
            self.logger.warning(sl_tp_err)
            return False

        return True

    # ──────────────────────────────────────────────────────────────────────
    # OPEN — Real on-chain execution via open.py
    # ──────────────────────────────────────────────────────────────────────
    async def execute_open(self, signal: Signal, size_usd: float, acct: Account = None, collateral_usd: float = None, wallet_id: int = 1) -> tuple:
        """Execute a full open signal on-chain: MarketIncrease/LimitIncrease + TPs + SL.

        Returns (Position, order_type) where order_type is "market" or "limit", or (None, None) on failure."""
        if acct is None:
            acct = self.account

        try:
            market_addr = self.cfg.markets.get(signal.symbol)
            if not market_addr:
                self.logger.error(f"Unknown symbol {signal.symbol}")
                return None, None

            self.logger.info(
                f"Opening {signal.symbol} {signal.side} "
                f"size=${size_usd:.2f} @ {signal.leverage:.1f}x, "
                f"entry=${signal.entry_mid:,.0f}, sl=${signal.stop_loss:,.0f}"
            )

            # Create position object immediately (before on-chain tx, for ID tracking)
            position = Position(
                id=str(uuid.uuid4()),
                symbol=signal.symbol,
                side=signal.side,
                size_usd=size_usd,
                leverage=signal.leverage,
                entry_price=signal.entry_mid,
                stop_loss=signal.stop_loss,
                take_profits=[
                    TakeProfitLevel(price=tp.price, percentage=tp.close_pct)
                    for tp in signal.take_profits
                ],
                market_addr=market_addr,
                collateral_token=self.cfg.collateral_token or None,
                opened_at=time.time(),
                wallet_id=wallet_id,
                original_size_usd=size_usd,
            )

            # Try to execute on-chain
            try:
                actual_collateral = collateral_usd if collateral_usd else (size_usd / signal.leverage if signal.leverage else size_usd)
                results = await asyncio.to_thread(
                    execute_signal,
                    w3=self.w3,
                    acct=acct,
                    signal=signal,
                    exchange_router=self.cfg.exchange_router,
                    order_vault=self.cfg.order_vault,
                    market=market_addr,
                    collateral_token=self.cfg.collateral_token,
                    size_usd=size_usd,
                    collateral_usd=actual_collateral,
                    execution_fee=self.cfg.execution_fee_wei,
                    slippage_bps=self.cfg.slippage_bps,
                    dry_run=self.cfg.dry_run,
                )
                order_type = results.get("order_type", "market")
                tx_hash = results.get("open", "")
                position.tx_hash = tx_hash

                # Record open order in history
                position.order_history.append({
                    "order_type": "open",
                    "tx_hash": tx_hash,
                    "price": signal.entry_mid,
                    "status": "placed",
                    "timestamp": time.time(),
                })

                # Filter take_profits to only include TPs that were actually placed on-chain.
                # This prevents the notification from showing TPs that failed to place,
                # and keeps check_tp_hits from tracking phantom TP orders.
                tp_results = results.get("tp", [])
                successfully_placed_prices = {
                    tp_r["price"] for tp_r in tp_results if tp_r.get("tx")
                }
                failed_tp_results = [
                    tp_r for tp_r in tp_results if not tp_r.get("tx")
                ]
                if tp_results:  # only filter if we have results to check against
                    position.take_profits = [
                        tp for tp in position.take_profits
                        if tp.price in successfully_placed_prices
                    ]
                # Populate tp_tx_hashes and record order history for all TP results
                position.tp_tx_hashes = [
                    tp_r["tx"] for tp_r in tp_results if tp_r.get("tx")
                ]
                for tp_r in tp_results:
                    position.order_history.append({
                        "order_type": "tp",
                        "tx_hash": tp_r.get("tx"),
                        "price": tp_r["price"],
                        "close_pct": tp_r.get("pct", 0),
                        "status": "placed" if tp_r.get("tx") else "failed",
                        "error": tp_r.get("error"),
                        "timestamp": time.time(),
                    })

                # TP count verification: expected vs actually placed
                placed_count = len(position.take_profits)
                expected_count = len(signal.take_profits)
                if placed_count == expected_count:
                    self.logger.info(
                        f"{signal.symbol} {signal.side}: All {expected_count}/{expected_count} TPs placed successfully"
                    )
                else:
                    self.logger.warning(
                        f"{signal.symbol} {signal.side}: Placed {placed_count}/{expected_count} TPs "
                        f"— {expected_count - placed_count} failed, queued for retry"
                    )

                # Queue failed TP orders for retry
                for tp_r in failed_tp_results:
                    failed = FailedOrder(
                        position_id=position.id,
                        symbol=signal.symbol,
                        side=signal.side,
                        market_addr=market_addr,
                        wallet_id=position.wallet_id,
                        order_kind="tp",
                        price=tp_r["price"],
                        size_usd=size_usd,
                        close_pct=tp_r.get("pct", 0),
                        is_long=signal.is_long,
                        error=tp_r.get("error", "unknown"),
                    )
                    self.failed_order_queue.append(failed)
                    self.logger.warning(
                        f"Queued failed TP @ ${tp_r['price']:,.2f} for retry "
                        f"({signal.symbol} {signal.side}): {tp_r.get('error', '?')}"
                    )

                # Record SL order result
                sl_result = results.get("sl")
                if sl_result:
                    position.sl_tx_hash = sl_result
                position.order_history.append({
                    "order_type": "sl",
                    "tx_hash": sl_result,
                    "price": signal.stop_loss,
                    "status": "placed" if sl_result else "failed",
                    "timestamp": time.time(),
                })

                # Queue failed SL order for retry
                if sl_result is None and signal.stop_loss:
                    failed_sl = FailedOrder(
                        position_id=position.id,
                        symbol=signal.symbol,
                        side=signal.side,
                        market_addr=market_addr,
                        wallet_id=position.wallet_id,
                        order_kind="sl",
                        price=signal.stop_loss,
                        size_usd=size_usd,
                        close_pct=1.0,
                        is_long=signal.is_long,
                        error="SL placement failed",
                    )
                    self.failed_order_queue.append(failed_sl)
                    self.logger.warning(
                        f"Queued failed SL @ ${signal.stop_loss:,.2f} for retry "
                        f"({signal.symbol} {signal.side})"
                    )

                # Notify if any orders were queued for retry
                n_failed_tp = len(failed_tp_results)
                n_failed_sl = 1 if (sl_result is None and signal.stop_loss) else 0
                if n_failed_tp > 0 or n_failed_sl > 0:
                    self._save_failed_orders()
                    parts = []
                    if n_failed_tp > 0:
                        parts.append(f"{n_failed_tp} TP(s)")
                    if n_failed_sl > 0:
                        parts.append("SL")
                    await self.notify(
                        f"⚠️ {signal.symbol} {signal.side}: {' + '.join(parts)} failed to place.\n"
                        f"Added to retry queue (max 5 attempts, 30s intervals)."
                    )

                self.logger.info(f"Position opened: {position.symbol} {position.side} TX={tx_hash} ({order_type})")
                self._set_orders_cooldown(30)
                return position, order_type

            except RuntimeError as e:
                self.logger.error(f"Open order REVERTED for {signal.symbol}: {e}")
                await self.notify(
                    f"❌ REVERTED {signal.symbol} {signal.side}: transaction reverted on-chain.\n"
                    f"Error: {e}\nThis order will NOT be retried."
                )
                return None, None
            except (ConnectionError, TimeoutError, OSError) as e:
                self.logger.error(f"RPC error opening {signal.symbol}: {e}")
                await self.notify(
                    f"❌ RPC ERROR opening {signal.symbol} {signal.side}: {e}\n"
                    f"Network issue — signal was NOT executed. Try /lastsignal to retry."
                )
                return None, None
            except Exception as e:
                self.logger.error(f"Failed to execute open: {e}\n{traceback.format_exc()}")
                await self.notify(f"❌ Failed to open {signal.symbol} {signal.side}: {e}")
                return None, None

        except Exception as e:
            self.logger.error(f"Error in execute_open: {e}\n{traceback.format_exc()}")
            return None, None

    async def wait_for_position_closed(self, market: str, is_long: bool, timeout: int = 120, acct: Account = None) -> bool:
        """Poll on-chain positions until the specified position disappears.
        Returns True if position closed within timeout, False otherwise."""
        if acct is None:
            acct = self.account

        start = time.time()
        while time.time() - start < timeout:
            try:
                positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                found = any(
                    p.market.lower() == market.lower() and p.is_long == is_long
                    for p in positions
                )
                if not found:
                    self.logger.info(f"Position closed: {market} {'LONG' if is_long else 'SHORT'}")
                    return True
            except Exception as e:
                self.logger.warning(f"Could not check position status: {e}")

            await asyncio.sleep(2)

        self.logger.warning(f"Position did not close within {timeout}s")
        return False

    async def execute_close(self, gmx_pos: GMXPosition, percentage: float = 1.0, acct: Account = None) -> Optional[str]:
        """Close a GMX position via MarketDecrease order.
        Returns tx_hash on success, None on failure."""
        if acct is None:
            acct = self.account

        try:
            is_long = gmx_pos.is_long
            side_label = "LONG" if is_long else "SHORT"
            pct_label = f"{percentage:.0%}" if percentage < 1.0 else "100%"

            self.logger.info(f"Closing {gmx_pos.symbol} {side_label} ({pct_label})")

            if self.cfg.dry_run:
                tx_hash = f"dry_run_{uuid.uuid4().hex[:16]}"
                self.logger.info(f"[DRY_RUN] Would close {gmx_pos.symbol} {side_label} TX={tx_hash}")
                return tx_hash

            tx_hash = await asyncio.to_thread(
                create_close_order,
                w3=self.w3,
                acct=acct,
                position=gmx_pos,
                percentage=percentage,
                dry_run=self.cfg.dry_run,
            )
            self.logger.info(f"Close TX submitted: {tx_hash}")
            return tx_hash

        except Exception as e:
            self.logger.error(f"Failed to close position: {e}")
            return None

    def _send_tx(self, to_addr: str, data: bytes, value: int, acct: Account = None) -> str:
        """Send a raw transaction and return tx_hash."""
        if acct is None:
            acct = self.account

        if self.cfg.dry_run:
            return f"dry_run_{uuid.uuid4().hex[:16]}"

        try:
            nonce = self.w3.eth.get_transaction_count(acct.address, "pending")
            base_fee = self.w3.eth.get_block("latest").get("baseFeePerGas", 0)
            priority_fee = self.w3.to_wei(0.1, "gwei")
            max_fee = base_fee * 2 + priority_fee
            tx = {
                'to': Web3.to_checksum_address(to_addr),
                'from': acct.address,
                'value': value,
                'data': data,
                'nonce': nonce,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'gas': 1000000,
                'chainId': self.w3.eth.chain_id,
                'type': 2,
            }

            # Estimate gas
            try:
                gas_estimate = self.w3.eth.estimate_gas(tx)
                tx['gas'] = int(gas_estimate * 1.1)  # 10% buffer
            except Exception as e:
                self.logger.warning(f"Gas estimation failed: {e}, using default")
                tx['gas'] = 1000000

            signed = acct.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            return tx_hash.hex()

        except Exception as e:
            self.logger.error(f"Failed to send TX: {e}")
            raise

    async def heartbeat_loop(self):
        """Periodic health check & status logging."""
        while True:
            try:
                await asyncio.sleep(self.cfg.heartbeat_interval)
                await self.perform_health_check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Heartbeat loop error: {e}")

    async def perform_health_check(self):
        """Log system health and clean up stale closed positions."""
        self.last_heartbeat = time.time()

        # Purge closed positions older than 24h to prevent unbounded dict growth
        cutoff = time.time() - 86400
        stale_ids = [
            pid for pid, p in self.positions.items()
            if not p.is_open and p.closed_at and p.closed_at < cutoff
        ]
        for pid in stale_ids:
            del self.positions[pid]
        if stale_ids:
            self.logger.debug(f"Purged {len(stale_ids)} stale closed position(s)")

        # Log stats
        self.logger.info(
            f"HEARTBEAT: {len(self.positions)} positions, "
            f"{self.health_stats['signals_processed']} signals, "
            f"{self.health_stats['trades_executed']} trades, "
            f"{self.health_stats['errors']} errors"
        )

    async def tp_monitor_loop(self):
        """Monitor open positions for TP hits and SL adjustments."""
        while True:
            try:
                await asyncio.sleep(5)
                await self.check_pending_fills()
                await self.check_position_closed()
                await self.check_tp_hits()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"TP monitor loop error: {e}")

    async def check_pending_fills(self):
        """Check if any pending limit orders have been filled on-chain."""
        for pos_id, pos in list(self.positions.items()):
            if not pos.pending_fill or not pos.is_open:
                continue
            if not pos.market_addr:
                continue

            try:
                acct = self._get_account(pos.wallet_id)
                chain_pos = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                found = any(p.market.lower() == pos.market_addr.lower() and p.is_long == (pos.side == "LONG") for p in chain_pos)

                if found:
                    self.logger.info(f"{pos.symbol} {pos.side} limit order FILLED on-chain")
                    pos.pending_fill = False
                else:
                    # Still pending — guard against None pending_fill_since
                    if pos.pending_fill_since is None:
                        pos.pending_fill_since = time.time()
                    elapsed = time.time() - pos.pending_fill_since
                    if elapsed > 300:  # 5 min timeout
                        self.logger.warning(f"{pos.symbol} {pos.side} limit order still pending after 5m — cancelling on-chain")
                        # Cancel the stale limit order on-chain
                        try:
                            exchange = self.w3.eth.contract(
                                address=Web3.to_checksum_address(self.cfg.exchange_router),
                                abi=EXCHANGE_ROUTER_ABI,
                            )
                            await asyncio.to_thread(
                                cancel_orders_for_market,
                                self.w3, acct, exchange, pos.market_addr, self.cfg.dry_run,
                            )
                            self.logger.info(f"Cancelled stale limit orders for {pos.symbol}")
                        except Exception as ce:
                            self.logger.warning(f"Failed to cancel stale orders for {pos.symbol}: {ce}")
                        pos.pending_fill = False
                        pos.is_open = False
                        del self.positions[pos_id]
                        await self.notify(
                            f"⚠️ {pos.symbol} {pos.side} limit order expired after 5m — cancelled"
                        )
            except Exception as e:
                self.logger.debug(f"Failed to check pending fill for {pos.symbol}: {e}")

    async def check_position_closed(self):
        """Check if any open positions have been closed on-chain (SL/TP hit, liquidation, etc.)."""
        for pos_id, pos in list(self.positions.items()):
            if not pos.is_open or pos.pending_fill:
                continue
            if not pos.market_addr:
                continue
            # Guard: if closed_at is already set, another coroutine beat us
            if pos.closed_at is not None:
                continue

            try:
                acct = self._get_account(pos.wallet_id)
                chain_pos = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )

                # Re-check is_open after the await — another coroutine (e.g.
                # handle_close_confirmation) may have closed this position
                # while we were fetching chain state.
                if not pos.is_open:
                    continue

                found = any(
                    p.market.lower() == pos.market_addr.lower()
                    and p.is_long == (pos.side == "LONG")
                    for p in chain_pos
                )

                if found:
                    # Position still on-chain — reset miss counter
                    self._position_missing_count.pop(pos_id, None)
                    continue

                # Position not found on-chain — require 2 consecutive misses
                # to guard against stale RPC returning empty results.
                miss_count = self._position_missing_count.get(pos_id, 0) + 1
                self._position_missing_count[pos_id] = miss_count

                if miss_count < 2:
                    self.logger.warning(
                        f"{pos.symbol} {pos.side} [W{pos.wallet_id}] not found on-chain "
                        f"(miss {miss_count}/2) — waiting for confirmation before closing"
                    )
                    continue

                # 2nd consecutive miss — position is genuinely closed
                self._position_missing_count.pop(pos_id, None)
                self.logger.info(f"{pos.symbol} {pos.side} [W{pos.wallet_id}] position closed on-chain (confirmed)")

                # Fetch remaining on-chain orders to distinguish liquidation
                # from SL/TP fills. If both SL and TP orders are still present,
                # neither was triggered → liquidation.
                sl_orders_remaining = -1
                tp_orders_remaining = -1
                try:
                    chain_orders = await asyncio.to_thread(
                        fetch_open_orders, self.w3, acct.address
                    )
                    market_lower = pos.market_addr.lower()
                    is_long = pos.side == "LONG"
                    market_orders = [
                        o for o in chain_orders
                        if o["market"].lower() == market_lower
                        and o["is_long"] == is_long
                    ]
                    sl_orders_remaining = sum(
                        1 for o in market_orders
                        if o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
                    )
                    tp_orders_remaining = sum(
                        1 for o in market_orders
                        if o["order_type"] == ORDER_TYPE_LIMIT_DECREASE
                    )
                    self.logger.info(
                        f"{pos.symbol} {pos.side} [W{pos.wallet_id}] "
                        f"remaining orders: {sl_orders_remaining} SL, {tp_orders_remaining} TP"
                    )
                except Exception as e:
                    self.logger.warning(f"Could not fetch orders for exit classification: {e}")

                # Refresh current price for exit classification
                try:
                    current_price = await self.get_current_price(pos.symbol)
                    if current_price and current_price > 0:
                        pos.current_price = current_price
                except Exception as e:
                    self.logger.warning(f"Failed to fetch price for {pos.symbol} during close detection: {e}")

                # Classify exit reason (SL hit, TP filled, liquidation, etc.)
                exit_reason = classify_exit_reason(
                    is_long=(pos.side == "LONG"),
                    current_price=pos.current_price,
                    stop_loss=pos.stop_loss,
                    tp_hits_count=pos.tp_hits_count,
                    sl_moved_to_entry=pos.sl_moved_to_entry,
                    sl_move_label=pos.sl_move_label,
                    sl_orders_remaining=sl_orders_remaining,
                    tp_orders_remaining=tp_orders_remaining,
                )

                is_liquidation = exit_reason == "Liquidation"

                # Determine exit price: SL exits use the SL trigger price
                # (more accurate than market price at detection time)
                exit_price = pos.current_price or pos.entry_price
                if "SL" in exit_reason and pos.stop_loss:
                    exit_price = pos.stop_loss

                # Calculate total PnL: realized (from verified decreases) + remaining
                realized_pnl = pos.realized_pnl or 0.0
                total_decreased = sum(d.get("size_delta_usd", 0) for d in pos.verified_decreases)
                base_size = pos.original_size_usd if pos.original_size_usd > 0 else pos.size_usd
                remaining_size = max(base_size - total_decreased, 0.0)

                remaining_pnl = 0.0
                if pos.entry_price and pos.entry_price > 0 and exit_price > 0:
                    remaining_pnl = calculate_unrealized_pnl(
                        pos.side, pos.entry_price, exit_price, remaining_size
                    )
                total_pnl = realized_pnl + remaining_pnl

                # Update pos so _record_trade and pnl_percentage use correct values
                pos.current_price = exit_price
                pos.unrealized_pnl = total_pnl

                # Final guard: re-check that no other coroutine closed this
                # position while we were classifying the exit.
                if pos.closed_at is not None:
                    continue

                pos.is_open = False
                pos.closed_at = time.time()
                pos.exit_reason = exit_reason
                await self._record_trade(pos, exit_reason=exit_reason)
                self._clear_position_state(pos)

                # Cancel orphaned SL/TP orders left behind after close/liquidation
                if sl_orders_remaining > 0 or tp_orders_remaining > 0:
                    try:
                        exchange = self.w3.eth.contract(
                            address=Web3.to_checksum_address(self.cfg.exchange_router),
                            abi=EXCHANGE_ROUTER_ABI,
                        )
                        n_cancelled = await asyncio.to_thread(
                            cancel_orders_for_market,
                            self.w3, acct, exchange, pos.market_addr, self.cfg.dry_run,
                        )
                        if n_cancelled:
                            self.logger.info(
                                f"Cancelled {n_cancelled} orphaned order(s) for "
                                f"{pos.symbol} {pos.side} [W{pos.wallet_id}]"
                            )
                    except Exception as e:
                        self.logger.warning(f"Failed to cancel orphaned orders: {e}")

                # Notify admin
                pnl_sign = "+" if total_pnl >= 0 else ""
                pnl_pct = pos.pnl_percentage
                duration = pos.duration_hours

                if is_liquidation:
                    msg = (
                        f"**LIQUIDATION DETECTED**\n\n"
                        f"{pos.symbol} {pos.side} [W{pos.wallet_id}]\n"
                        f"Entry: ${pos.entry_price:,.2f}\n"
                        f"Exit: ${exit_price:,.2f}\n"
                        f"Size: ${pos.size_usd:,.2f} @ {pos.leverage:.1f}x\n"
                        f"Collateral: ${pos.collateral_usd:,.2f}\n"
                    )
                    if realized_pnl != 0:
                        r_sign = "+" if realized_pnl >= 0 else ""
                        msg += f"Realized (TPs): {r_sign}${realized_pnl:,.2f}\n"
                    msg += (
                        f"PnL: {pnl_sign}${total_pnl:,.2f} ({pnl_sign}{pnl_pct:.1f}%)\n"
                        f"Duration: {duration:.1f}h\n"
                    )
                    if pos.stop_loss:
                        msg += f"SL was: ${pos.stop_loss:,.2f}\n"
                    msg += f"Orphaned orders cancelled: {sl_orders_remaining} SL + {tp_orders_remaining} TP"
                else:
                    msg = (
                        f"**Position Closed**\n\n"
                        f"{pos.symbol} {pos.side} [W{pos.wallet_id}]\n"
                        f"Entry: ${pos.entry_price:,.2f}\n"
                        f"Exit: ${exit_price:,.2f}\n"
                    )
                    if realized_pnl != 0:
                        r_sign = "+" if realized_pnl >= 0 else ""
                        rm_sign = "+" if remaining_pnl >= 0 else ""
                        msg += (
                            f"Realized (TPs): {r_sign}${realized_pnl:,.2f}\n"
                            f"Remaining:      {rm_sign}${remaining_pnl:,.2f}\n"
                        )
                    msg += (
                        f"PnL: {pnl_sign}${total_pnl:,.2f} ({pnl_sign}{pnl_pct:.1f}%)\n"
                        f"Duration: {duration:.1f}h"
                    )
                await self.notify(msg)

                # Track liquidation in health stats
                if is_liquidation:
                    self.health_stats["liquidations"] = self.health_stats.get("liquidations", 0) + 1

            except Exception as e:
                self.logger.debug(f"Failed to check position close for {pos.symbol}: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def main():
    bot = GMXBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        print("\nBot interrupted by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
