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
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IMPORT MIXIN ARCHITECTURE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from notifications import NotificationsMixin
from sl_tp import SLTPMixin
from wallet_mgmt import WalletMixin
from price_feeds import PriceFeedsMixin, PriceData
from analytics import AnalyticsMixin, TradeRecord
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BOT ENGINE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GMXBot(NotificationsMixin, SLTPMixin, WalletMixin, PriceFeedsMixin, AnalyticsMixin, CoreTelegramMixin):
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
            level=getattr(logging, self.cfg.log_level, logging.INFO),
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

        # Load persisted trade history (PnL / win rate data)
        self._load_trade_history()

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
            if self.client and self.cfg.notify_chat:
                if not self.client.is_connected():
                    await self.client.connect()
                await self.client.send_message(self.cfg.notify_chat, "🔴 Bot Offline")
                await self.client.disconnect()
        except Exception as e:
            self.logger.error(f"Failed to send offline notification: {e}")

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

    async def _sync_on_chain_positions(self):
        """Scan on-chain positions for all wallets and add any that aren't
        already tracked internally. This makes the bot aware of positions
        opened manually or before a reboot."""
        MARKET_TO_SYMBOL = {v.lower(): k for k, v in self.cfg.markets.items()}

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

                # Reconstruct TP levels from on-chain TP orders (LimitDecrease)
                tp_orders = [
                    o for o in chain_orders
                    if o["market"].lower() == market_lower
                    and o["order_type"] == ORDER_TYPE_LIMIT_DECREASE
                ]
                tp_count = len(tp_orders)

                take_profits = []
                if tp_orders and cp.size_usd > 0:
                    # Sort TPs by price (ascending for LONG, descending for SHORT)
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
                    last_known_tp_count=tp_count,
                    take_profits=take_profits,
                )
                self.positions[pos.id] = pos
                synced += 1
                tp_str = f", {tp_count} TPs reconstructed" if take_profits else ", no TPs"
                self.logger.info(
                    f"Synced on-chain position: {symbol} {side} "
                    f"${cp.size_usd:,.2f} @ {cp.leverage:.1f}x [W{wid}]{tp_str}"
                )

        if synced:
            self.logger.info(f"Synced {synced} on-chain position(s) into tracking")
        else:
            self.logger.info("No untracked on-chain positions found")

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
            if is_update_message(text):
                self.logger.debug(f"Ignored update message: {text[:80]}")
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

            # Determine collateral = portfolio_pct of TOTAL portfolio (free USDC + deployed collateral + PnL)
            total_portfolio = await self._get_total_portfolio_value()
            free_usdc = await self._get_combined_usdc()

            if total_portfolio <= 0:
                await self.notify(f"Rejected {signal.symbol}: total portfolio value is $0")
                return

            # Cap leverage at max_leverage first so collateral calculation is correct
            signal.leverage = cap_leverage(signal.leverage, self.cfg.max_leverage)

            collateral_usd = total_portfolio * self.cfg.portfolio_pct
            size_usd = collateral_usd * signal.leverage

            min_collateral_err = check_min_collateral(
                collateral_usd, self.cfg.min_position_usd, self.cfg.portfolio_pct, total_portfolio
            )
            if min_collateral_err:
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
                f"Collateral: ${collateral_usd:.0f} ({self.cfg.portfolio_pct:.0%} of ${total_portfolio:.0f})\n"
                f"Size: ${size_usd:.0f} @ {signal.leverage:.0f}x\n"
                f"Mode: {'DRY RUN' if self.cfg.dry_run else 'LIVE'}"
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

        # Note: wallet availability (on-chain position check) is done in
        # process_signal via _pick_wallet() after validation passes.

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
    async def execute_open(self, signal: Signal, size_usd: float, acct: Account = None, collateral_usd: float = None) -> tuple:
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
                opened_at=time.time(),
            )

            # Try to execute on-chain
            try:
                actual_collateral = collateral_usd if collateral_usd else size_usd / signal.leverage
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

                # Set last_known_tp_count from successfully placed TP orders
                successful_tps = sum(1 for tp_r in results.get("tp", []) if tp_r.get("tx"))
                position.last_known_tp_count = successful_tps

                self.logger.info(f"Position opened: {position.symbol} {position.side} TX={tx_hash} ({order_type})")
                return position, order_type

            except Exception as e:
                self.logger.error(f"Failed to execute open: {e}")
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
            max_fee = base_fee + priority_fee * 2
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

                if not found:
                    # Position no longer on-chain — closed by SL/TP/liquidation
                    self.logger.info(f"{pos.symbol} {pos.side} [W{pos.wallet_id}] position closed on-chain")

                    # Refresh current price for exit classification
                    try:
                        current_price = await self.get_current_price(pos.symbol)
                        if current_price and current_price > 0:
                            pos.current_price = current_price
                    except Exception:
                        pass

                    # Classify exit reason (SL hit, TP filled, liquidation, etc.)
                    exit_reason = classify_exit_reason(
                        is_long=(pos.side == "LONG"),
                        current_price=pos.current_price,
                        stop_loss=pos.stop_loss,
                        tp_hits_count=pos.tp_hits_count,
                        last_known_tp_count=pos.last_known_tp_count,
                        sl_moved_to_entry=pos.sl_moved_to_entry,
                        sl_move_label=pos.sl_move_label,
                    )

                    # Determine exit price: SL exits use the SL trigger price
                    # (more accurate than market price at detection time)
                    exit_price = pos.current_price or pos.entry_price
                    if "SL" in exit_reason and pos.stop_loss:
                        exit_price = pos.stop_loss

                    # Calculate total PnL: realized (from executed TPs) + remaining
                    realized_pnl = pos.realized_pnl or 0.0
                    remaining_pct = 1.0
                    if pos.take_profits:
                        executed_tps = [tp for tp in pos.take_profits if tp.executed]
                        remaining_pct = 1.0 - sum(tp.percentage for tp in executed_tps)
                    remaining_size = pos.size_usd * max(remaining_pct, 0.0)

                    remaining_pnl = 0.0
                    if pos.entry_price and pos.entry_price > 0 and exit_price > 0:
                        remaining_pnl = calculate_unrealized_pnl(
                            pos.side, pos.entry_price, exit_price, remaining_size
                        )
                    total_pnl = realized_pnl + remaining_pnl

                    # Update pos so _record_trade and pnl_percentage use correct values
                    pos.current_price = exit_price
                    pos.unrealized_pnl = total_pnl

                    pos.is_open = False
                    pos.closed_at = time.time()
                    pos.exit_reason = exit_reason
                    self._record_trade(pos, exit_reason=exit_reason)

                    # Notify admin
                    pnl_sign = "+" if total_pnl >= 0 else ""
                    pnl_pct = pos.pnl_percentage
                    duration = pos.duration_hours
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
                        f"Duration: {duration:.1f}h\n"
                        f"Reason: {exit_reason}"
                    )
                    await self.notify(msg)

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
