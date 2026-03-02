"""
Telegram integration for GMX V2 Trading Bot — Core Commands.

Contains:
  - CoreTelegramMixin: Telegram initialization, command routing,
    and core commands (status, positions, close, increase, etc.)
    designed to be mixed into GMXBot.
  - HELP_TEXT constant for the /help command.

Other Telegram features have been moved to specialized mixins:
  - NotificationsMixin (notifications.py): notify(), send_message(), etc.
  - SLTPMixin (sl_tp.py): cmd_sl(), cmd_addorder(), cmd_cancelorder()
  - WalletMixin (wallet_mgmt.py): cmd_balance(), cmd_gas(), cmd_topup(), cmd_balance_wallets()
  - PriceFeedsMixin (price_feeds.py): cmd_prices()
  - AnalyticsMixin (analytics.py): cmd_winrate(), cmd_pnl(), cmd_reset(), cmd_health()

GMXBot inherits from all mixins to get full command coverage.
"""

import time
import asyncio
import logging
import traceback
from typing import Optional, List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from web3 import Web3

import bot_api

# ── Imports from sibling modules ──
from config import ALLOWED_SYMBOLS
from risk import is_update_message
from open import (  # type: ignore[assignment]  # noqa: A004
    fetch_current_price,
    fetch_open_orders,
    cancel_orders_for_market,
    cancel_all_orders,
    create_market_increase_order,
    scale_price,
    EXCHANGE_ROUTER_ABI,
    ERC20_ABI,
)
import open as _open_mod  # type: ignore[assignment]  # noqa: A004
from close import (
    fetch_positions as chain_fetch_positions,
    fetch_current_price as close_fetch_current_price,
    GMXPosition,
    create_close_order,
)

logger = logging.getLogger("GMXBot.telegram")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Help text
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HELP_TEXT = """**GMX V2 Bot Commands**

/addorder — Manually add a SL or TP to an open position
/balance — Wallet ETH & token balance
/balance-wallets — Manually rebalance USDC between wallets (W1-W4)
/cancel — Cancel a pending withdraw or close
/cancelorder — List & cancel individual SL/TP orders by number
/close — Show positions + open orders
/close all — Close all positions + cancel all orders
/close BTC — Close by symbol
/confirm — Confirm pending close
/consolidate — Move ALL free USDC from W2-W4 into W1 (for withdrawals)
/gas — ETH gas balances for all wallets
/halt [reason] — Halt trading
/health — System health
/help — This message
/increase — Add collateral to an open position
/lastmsg — Print last message from monitored channel(s)
/lastsignal — Re-run the last parsed signal
/pdf — Download trade history as PDF
/pnl — PnL summary (today / 30d / all time) for BTC, SOL, ETH
/positions — Show on-chain positions
/prices — Live GMX & Chainlink prices for all tracked assets
/reset — Clear all trade history & PnL stats
/resume [reason] — Resume trading
/retryqueue — Show pending failed order retries
/sl — Move SL to entry or TP level
/sl 1 entry — Move #1 SL to entry (breakeven)
/sl 1 tp2 — Move #1 SL to TP2 price
/status — Bot status & mode
/sync — Force re-sync positions from on-chain
/summary — Send daily summary now
/topup — Manual ETH top-up (swap USDC → ETH for gas)
/tradesize — Show/change trade size (e.g. /tradesize 20 for 20%)
/deposit — Show W1 deposit address (auto-rebalances when USDC arrives)
/withdraw <amount> — Withdraw USDC to any Arbitrum address
/winrate [SYMBOL] [N] — Win rate stats

**Wallets:** W1=swing, W2-W4=scalps"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CoreTelegramMixin — core Telegram methods for GMXBot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CoreTelegramMixin:
    """Mixin providing Telegram init, command routing, and core commands.

    Expected attributes on the host class (GMXBot):
        cfg, client, w3, account, account2, account3, account4,
        positions, trade_history, pending_closes, pending_increase,
        is_halted, halt_reason, halt_time, health_stats,
        resolved_channels, price_cache, logger,
        and methods: _all_wallets, _scalp_wallets, _get_account,
        _get_portfolio_value_for, _get_combined_usdc,
        _get_total_portfolio_value, _rebalance_wallets,
        execute_close, wait_for_position_closed,
        topup_eth_if_needed, get_current_price, fetch_price,
        move_sl, get_health_report, _record_trade, process_signal.

    Also expects these methods from mixins:
        notify(), send_message() — from NotificationsMixin
        cmd_sl(), cmd_addorder(), cmd_cancelorder() — from SLTPMixin
        cmd_balance(), cmd_gas(), cmd_topup(), cmd_balance_wallets() — from WalletMixin
        cmd_prices() — from PriceFeedsMixin
        cmd_winrate(), cmd_pnl(), cmd_reset(), cmd_health() — from AnalyticsMixin
    """

    # ──────────────────────────────────────────────────────────────────────
    # Init & event handlers
    # ──────────────────────────────────────────────────────────────────────

    async def init_telegram(self):
        cfg = self.cfg
        if not cfg.telegram_api_id or not cfg.telegram_api_hash:
            raise ValueError("Missing Telegram API credentials")

        self.client = TelegramClient(
            cfg.telegram_session, cfg.telegram_api_id, cfg.telegram_api_hash
        )
        await self.client.start()

        # Pre-resolve channel entities
        resolved_channels = []
        for ch in cfg.telegram_channels:
            try:
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
            text = event.message.text
            if not text:
                return
            # Check if this is an update message (TP hit, SL triggered, etc.)
            # Route through channel TP confirmation before discarding
            if is_update_message(text):
                try:
                    await self.check_channel_tp_confirmation(text)
                except Exception as e:
                    self.logger.debug(f"Channel TP confirmation check failed: {e}")
                return
            await self.process_signal(text)

        # Resolve admin_chat — must be a valid int for Telethon event matching
        admin_chat_id = cfg.admin_chat
        if not admin_chat_id:
            self.logger.warning(
                "ADMIN_CHAT not configured — admin commands disabled. "
                "Set ADMIN_CHAT in .env to a Telegram chat/group ID."
            )
        else:
            # Admin commands — messages starting with /
            @self.client.on(events.NewMessage(chats=[admin_chat_id], pattern=r"^/"))
            async def handle_admin(event):
                sender = await event.get_sender()
                username = (getattr(sender, "username", "") or "").lower()
                if cfg.admin_usernames and username not in cfg.admin_usernames:
                    return
                await self.process_admin_command(event.message.text, event.chat_id)

            # Confirmation handler — non-command messages from admin
            @self.client.on(events.NewMessage(chats=[admin_chat_id]))
            async def handle_confirm(event):
                text = event.message.text.strip()
                if text.startswith("/"):
                    return
                sender = await event.get_sender()
                username = (getattr(sender, "username", "") or "").lower()
                if cfg.admin_usernames and username not in cfg.admin_usernames:
                    return
                if event.chat_id in self.pending_withdraw:
                    await self.handle_withdraw_reply(event.chat_id, text)
                elif event.chat_id in self.pending_increase:
                    await self.handle_increase_reply(event.chat_id, text)
                else:
                    await self.handle_close_confirmation(event.chat_id, text)

            self.logger.info(f"Admin chat configured: {admin_chat_id}")

        self.logger.info(f"Telegram initialized, monitoring {len(resolved_channels)} channel(s)")

    # ──────────────────────────────────────────────────────────────────────
    # Bot API polling loop (DM command interface)
    # ──────────────────────────────────────────────────────────────────────

    async def bot_api_polling_loop(self):
        """Poll the Bot HTTP API for DM commands.

        Runs alongside Telethon (which only reads VIP signal channels).
        All user interaction — commands, confirmations, responses — flows
        through here instead of the Telethon admin chat.
        """
        token = self.cfg.telegram_bot_token
        self.logger.info("Bot API polling loop started")

        # Flush any old updates so we only process new messages
        _, self._bot_update_offset = await bot_api.get_updates(token, offset=0, timeout=0)

        while True:
            try:
                updates, self._bot_update_offset = await bot_api.get_updates(
                    token, offset=self._bot_update_offset, timeout=30
                )
                for update in updates:
                    # Handle both private messages and channel posts
                    msg = update.get("message") or update.get("channel_post")
                    if not msg:
                        continue
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    chat_id = msg["chat"]["id"]

                    # ── Auth check: only allow admins ──
                    sender = msg.get("from", {})
                    sender_username = (sender.get("username") or "").lower()
                    sender_id = str(sender.get("id", ""))
                    allowed = False
                    if self.cfg.admin_usernames and sender_username in self.cfg.admin_usernames:
                        allowed = True
                    elif self.cfg.bot_admin_chat_id and sender_id == self.cfg.bot_admin_chat_id:
                        allowed = True
                    elif str(chat_id) == str(self.cfg.admin_chat):
                        allowed = True
                    if not allowed:
                        self.logger.warning(
                            f"Unauthorized Bot API message from @{sender_username} "
                            f"(id={sender_id}, chat={chat_id}), ignoring."
                        )
                        continue

                    # Track this chat as a Bot API chat for response routing
                    self._bot_api_chats.add(chat_id)

                    if text.startswith("/"):
                        await self.process_admin_command(text, chat_id)
                    elif chat_id in self.pending_withdraw:
                        await self.handle_withdraw_reply(chat_id, text)
                    elif chat_id in self.pending_increase:
                        await self.handle_increase_reply(chat_id, text)
                    else:
                        await self.handle_close_confirmation(chat_id, text)

            except asyncio.CancelledError:
                self.logger.info("Bot API polling loop cancelled")
                return
            except Exception as e:
                self.logger.error(f"Bot API polling error: {e}")
                await asyncio.sleep(5)

    # ──────────────────────────────────────────────────────────────────────
    # Admin command dispatcher
    # ──────────────────────────────────────────────────────────────────────

    async def process_admin_command(self, text: str, chat_id: int):
        try:
            parts = text.strip().split()
            cmd = parts[0].lower()

            if cmd == "/help":
                await self.send_message(chat_id, HELP_TEXT)
            elif cmd == "/status":
                await self.cmd_status(chat_id)
            elif cmd == "/positions":
                await self.cmd_positions(chat_id)
            elif cmd == "/close":
                arg = parts[1] if len(parts) > 1 else None
                await self.cmd_close(chat_id, arg)
            elif cmd == "/confirm":
                if chat_id in self.pending_withdraw:
                    await self.handle_withdraw_reply(chat_id, "CONFIRM")
                else:
                    await self.handle_close_confirmation(chat_id, "YES")
            elif cmd == "/halt":
                reason = " ".join(parts[1:]) if len(parts) > 1 else "Manual halt"
                await self.halt_trading(reason)
            elif cmd == "/resume":
                reason = " ".join(parts[1:]) if len(parts) > 1 else "Manual resume"
                await self.resume_trading(reason)
            elif cmd == "/winrate":
                sym = parts[1].upper() if len(parts) > 1 else None
                n = None
                if len(parts) > 2:
                    try:
                        n = int(parts[2])
                    except ValueError:
                        await self.send_message(chat_id, "Invalid count. Usage: /winrate [SYMBOL] [N]")
                        return
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
            elif cmd == "/lastsignal":
                await self.cmd_lastsignal(chat_id)
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
            elif cmd == "/consolidate":
                await self.cmd_consolidate(chat_id)
            elif cmd == "/gas":
                await self.cmd_gas(chat_id)
            elif cmd == "/tradesize":
                arg = " ".join(parts[1:]) if len(parts) > 1 else None
                await self.cmd_tradesize(chat_id, arg)
            elif cmd == "/retryqueue":
                await self.cmd_retryqueue(chat_id)
            elif cmd == "/pdf":
                await self.cmd_pdf(chat_id)
            elif cmd == "/sync":
                await self.cmd_sync(chat_id)
            elif cmd == "/withdraw":
                arg = " ".join(parts[1:]) if len(parts) > 1 else None
                await self.cmd_withdraw(chat_id, arg)
            elif cmd == "/deposit":
                await self.cmd_deposit(chat_id)
            elif cmd == "/cancel":
                if chat_id in self.pending_withdraw:
                    state = self.pending_withdraw[chat_id].get("state", "")
                    if state in ("awaiting_address", "confirm"):
                        del self.pending_withdraw[chat_id]
                        await self.send_message(chat_id, "Withdrawal cancelled.")
                    else:
                        await self.send_message(
                            chat_id,
                            "Cannot cancel — transfer already in progress."
                        )
                elif chat_id in self.pending_closes:
                    del self.pending_closes[chat_id]
                    await self.send_message(chat_id, "Close cancelled.")
                else:
                    await self.send_message(chat_id, "Nothing to cancel.")
            else:
                await self.send_message(chat_id, "Unknown command. Type /help")

        except Exception as e:
            self.logger.error(f"Admin command error: {e}\n{traceback.format_exc()}")
            await self.send_message(chat_id, f"Command error: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # Halt / Resume
    # ──────────────────────────────────────────────────────────────────────

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
    # /status
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_status(self, chat_id: int):
        cfg = self.cfg
        health = self.get_health_report()
        status = "HALTED" if health["is_halted"] else "ACTIVE"
        uptime_hours = health["uptime_seconds"] / 3600
        total_exposure = sum(p.size_usd for p in self.positions.values() if p.is_open)
        wallet_roles = {1: "swing", 2: "scalp", 3: "scalp", 4: "scalp"}
        wallet_lines = []
        for wid, acct in self._all_wallets():
            role = wallet_roles.get(wid, "scalp")
            wallet_lines.append(f"W{wid} ({role}): {acct.address[:8]}...{acct.address[-6:]}")
        wallet_str = "\n".join(wallet_lines) if wallet_lines else "N/A"
        msg = (
            "**GMX V2 Bot Status**\n\n"
            f"Status: {status}\n"
            f"Mode: {'DRY RUN' if cfg.dry_run else 'LIVE'}\n"
            f"{wallet_str}\n"
            f"Network: {cfg.network.upper()}\n"
            f"Uptime: {uptime_hours:.1f}h\n\n"
            f"Positions: {health['open_positions']}\n"
            f"Exposure: ${total_exposure:.0f}\n"
            f"Signals: {health['signals_processed']}\n"
            f"Trades: {health['trades_executed']}\n"
            f"Errors: {health['errors']}"
        )
        if health["is_halted"]:
            msg += f"\n\nHalt reason: {self.halt_reason}"
        await self.send_message(chat_id, msg)

    # ──────────────────────────────────────────────────────────────────────
    # Shared: fetch all positions + orders across wallets
    # ──────────────────────────────────────────────────────────────────────

    async def _fetch_all_positions_and_orders(self):
        all_positions = []
        all_orders = []
        for wid, acct in self._all_wallets():
            try:
                pos, ords = await asyncio.gather(
                    asyncio.to_thread(chain_fetch_positions, self.w3, acct.address),
                    asyncio.to_thread(fetch_open_orders, self.w3, acct.address),
                )
                for p in pos:
                    p._wallet_acct = acct
                    p._wallet_id = wid
                all_positions.extend(pos)
                for o in ords:
                    o["_wallet_acct"] = acct
                    o["_wallet_id"] = wid
                all_orders.extend(ords)
            except Exception as e:
                self.logger.warning(f"Failed to fetch from W{wid} {acct.address[:10]}: {e}")
        return all_positions, all_orders

    # ──────────────────────────────────────────────────────────────────────
    # /positions
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_positions(self, chat_id: int):
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

        if positions:
            msg += f"**Positions ({len(positions)})**\n"
            for i, pos in enumerate(positions, 1):
                side = "LONG" if pos.is_long else "SHORT"
                display_price = pos.current_price

                # Use on-chain PnL when available, otherwise calculate locally
                if getattr(pos, 'pnl_source', 'local') == "onchain":
                    pnl = pos.unrealized_pnl
                    pnl_pct = pos.pnl_percentage
                elif display_price and pos.entry_price and pos.entry_price > 0:
                    if pos.is_long:
                        price_diff = display_price - pos.entry_price
                    else:
                        price_diff = pos.entry_price - display_price
                    pnl = (price_diff / pos.entry_price) * pos.size_usd
                    pnl_pct = (pnl / pos.collateral_amount) * 100 if pos.collateral_amount else 0
                else:
                    pnl = pos.unrealized_pnl if pos.unrealized_pnl else 0.0
                    pnl_pct = pos.pnl_percentage if pos.pnl_percentage else 0.0
                pnl_icon = "+" if pnl >= 0 else ""

                pos_orders = [o for o in orders if o["market"].lower() == pos.market.lower()]
                tp_orders = sorted([o for o in pos_orders if o["order_type"] == 5],
                                   key=lambda o: o["trigger_price"])
                sl_orders = [o for o in pos_orders if o["order_type"] == 6]
                limit_orders = [o for o in pos_orders if o["order_type"] in (2, 3)]

                wid_label = ""
                wid = getattr(pos, '_wallet_id', 1)
                if hasattr(pos, '_wallet_id'):
                    wid_label = f" [W{wid}]"

                # Look up internal position for TP hit / realized PnL info
                internal = None
                for ip in self.positions.values():
                    if (ip.is_open
                            and ip.market_addr
                            and ip.market_addr.lower() == pos.market.lower()
                            and ip.side == side
                            and ip.wallet_id == wid):
                        internal = ip
                        break

                # Calculate realized PnL from filled TPs
                realized_pnl = 0.0
                tp_hits = 0
                total_tps = 0
                sl_label = None
                if internal:
                    tp_hits = internal.tp_hits_count
                    total_tps = len(internal.take_profits)
                    sl_label = internal.sl_move_label
                    for tp in internal.take_profits:
                        if tp.executed and pos.entry_price and pos.entry_price > 0:
                            tp_size = internal.size_usd * tp.percentage
                            if pos.is_long:
                                realized_pnl += ((tp.price - pos.entry_price) / pos.entry_price) * tp_size
                            else:
                                realized_pnl += ((pos.entry_price - tp.price) / pos.entry_price) * tp_size

                current_str = f"${display_price:,.2f}" if display_price else "N/A"
                entry_str = f"${pos.entry_price:,.2f}" if pos.entry_price else "N/A"
                msg += (
                    f"\n**#{i} {pos.symbol} {side}{wid_label}**\n"
                    f"  Size:    ${pos.size_usd:,.2f} @ {pos.leverage:.1f}x\n"
                    f"  Collateral: ${pos.collateral_amount:,.2f}\n"
                    f"  Entry:   {entry_str}\n"
                    f"  Current: {current_str}\n"
                )

                # On-chain fee breakdown (when available)
                is_onchain = getattr(pos, 'pnl_source', 'local') == "onchain"
                fee_line = ""
                if is_onchain:
                    total_fees = pos.borrowing_fee_usd + pos.funding_fee_usd + pos.closing_fee_usd
                    fee_line = f"  Fees:    -${total_fees:,.2f} (borrow: ${pos.borrowing_fee_usd:,.2f}, fund: ${pos.funding_fee_usd:,.2f}, close: ${pos.closing_fee_usd:,.2f})\n"

                if tp_hits > 0:
                    total_pnl_combined = realized_pnl + pnl
                    r_sign = "+" if realized_pnl >= 0 else ""
                    t_sign = "+" if total_pnl_combined >= 0 else ""
                    msg += (
                        f"  TPs:     {tp_hits}/{total_tps} hit"
                        + (f" (SL → {sl_label})" if sl_label else "")
                        + "\n"
                        f"  Realized:   {r_sign}${realized_pnl:,.2f}\n"
                        f"  Unrealized: {pnl_icon}${pnl:,.2f} ({pnl_icon}{pnl_pct:.1f}%)\n"
                    )
                    if fee_line:
                        msg += fee_line
                    msg += f"  **Total PnL: {t_sign}${total_pnl_combined:,.2f}**\n"
                else:
                    msg += f"  PnL:     {pnl_icon}${pnl:,.2f} ({pnl_icon}{pnl_pct:.1f}%)\n"
                    if fee_line:
                        msg += fee_line

                if sl_orders or tp_orders:
                    msg += "  TP & SL:\n"
                    # Shorts: sort TPs highest→lowest (price drops toward targets)
                    # Longs: sort TPs lowest→highest (price rises toward targets)
                    sorted_tps = sorted(
                        tp_orders,
                        key=lambda x: x.get("trigger_price", 0) or 0,
                        reverse=not pos.is_long,
                    )
                    for j, o in enumerate(sorted_tps, 1):
                        tp_price = o.get("trigger_price", 0) or 0

                        # % of position closing at this TP
                        tp_size = o.get("size_usd", 0) or 0
                        close_pct_str = ""
                        if tp_size > 0 and pos.size_usd > 0:
                            close_pct = (tp_size / pos.size_usd) * 100
                            close_pct_str = f" (closes {close_pct:.0f}%)"
                        elif internal:
                            # Fall back to internal TP percentage if available
                            remaining_tps = [t for t in internal.take_profits if not t.executed]
                            remaining_tps_sorted = sorted(remaining_tps, key=lambda t: t.price, reverse=(not pos.is_long))
                            if j - 1 < len(remaining_tps_sorted):
                                close_pct_str = f" (closes {remaining_tps_sorted[j-1].percentage:.0%})"

                        if tp_price and pos.entry_price and pos.entry_price > 0:
                            # Token price change % — raw direction (negative for shorts)
                            price_chg = ((tp_price - pos.entry_price) / pos.entry_price) * 100

                            # Projected PnL — only for the portion closing at this TP
                            if pos.is_long:
                                pnl_per_dollar = (tp_price - pos.entry_price) / pos.entry_price
                            else:
                                pnl_per_dollar = (pos.entry_price - tp_price) / pos.entry_price

                            tp_close_size = tp_size if tp_size > 0 else pos.size_usd
                            proj = pnl_per_dollar * tp_close_size

                            # PnL % on collateral for this TP portion
                            collateral = pos.size_usd / pos.leverage if pos.leverage else pos.size_usd
                            tp_collateral = collateral * (tp_size / pos.size_usd) if tp_size > 0 and pos.size_usd > 0 else collateral
                            pnl_pct = (proj / tp_collateral * 100) if tp_collateral > 0 else 0
                            pnl_pct_sign = "+" if pnl_pct >= 0 else ""

                            proj_sign = "+" if proj >= 0 else ""
                            chg_sign = "+" if price_chg >= 0 else ""
                            sym = pos.symbol or ""
                            msg += (
                                f"  TP{j}{close_pct_str} @ ${tp_price:,.2f}\n"
                                f"     ({pnl_pct_sign}{pnl_pct:.1f}% PnL, {proj_sign}${proj:,.2f} projected, {sym} {chg_sign}{price_chg:.2f}%)\n"
                            )
                        elif tp_price:
                            msg += f"  TP{j}{close_pct_str} @ ${tp_price:,.2f}\n"
                        else:
                            msg += f"  TP{j} @ unknown\n"

                    # SL at the bottom — deduplicate if multiple exist on-chain
                    if len(sl_orders) > 1:
                        msg += f"  ⚠️ {len(sl_orders)} SL orders found (cleaning up duplicates)\n"
                        asyncio.create_task(self._cleanup_duplicate_sl_orders(pos, sl_orders, orders))
                    shown_sl = sl_orders[:1]
                    for o in shown_sl:
                        sl_price = o.get("trigger_price", 0) or 0
                        if sl_price and pos.entry_price and pos.entry_price > 0:
                            price_chg = ((sl_price - pos.entry_price) / pos.entry_price) * 100

                            if pos.is_long:
                                pnl_per_dollar = (sl_price - pos.entry_price) / pos.entry_price
                            else:
                                pnl_per_dollar = (pos.entry_price - sl_price) / pos.entry_price

                            proj = pnl_per_dollar * pos.size_usd

                            collateral = pos.size_usd / pos.leverage if pos.leverage else pos.size_usd
                            pnl_pct = (proj / collateral * 100) if collateral > 0 else 0
                            pnl_pct_sign = "+" if pnl_pct >= 0 else ""

                            proj_sign = "+" if proj >= 0 else ""
                            chg_sign = "+" if price_chg >= 0 else ""
                            sym = pos.symbol or ""
                            msg += (
                                f"  SL  @ ${sl_price:,.2f}\n"
                                f"     ({pnl_pct_sign}{pnl_pct:.1f}% PnL, {proj_sign}${proj:,.2f} projected, {sym} {chg_sign}{price_chg:.2f}%)\n"
                            )
                        elif sl_price:
                            msg += f"  SL  @ ${sl_price:,.2f}\n"
                        else:
                            msg += f"  SL  @ unknown\n"

                if limit_orders:
                    msg += "  Limit Orders:\n"
                    for o in limit_orders:
                        lp = o.get("trigger_price", 0) or 0
                        price_str = f"${lp:,.2f}" if lp else "market"
                        size = o.get("size_usd", 0) or 0
                        msg += f"    Limit @ {price_str}  (${size:,.2f})\n"

            total_pnl = sum(p.unrealized_pnl for p in positions)
            total_size = sum(p.size_usd for p in positions)
            any_onchain = any(getattr(p, 'pnl_source', 'local') == "onchain" for p in positions)
            pnl_tag = " (on-chain)" if any_onchain else ""
            msg += f"\nTotal Size: ${total_size:,.2f}  |  Total PnL: ${total_pnl:+.2f}{pnl_tag}\n"

        # Pending limit entry orders
        pending_entries = [
            o for o in orders
            if o["order_type"] in (2, 3)
            and o["market"].lower() not in open_pos_markets
        ]
        if pending_entries:
            msg += f"\n**Limit Orders ({len(pending_entries)})** _(pending entry)_\n"
            for o in pending_entries:
                side = "LONG" if o.get("is_long") else "SHORT"
                tp = o.get("trigger_price", 0) or 0
                price_str = f"${tp:,.2f}" if tp else "market"
                msg += f"  {o['symbol']} {side} @ {price_str}  (${o['size_usd']:,.2f})\n"

        await self.send_message(chat_id, msg)

    # ──────────────────────────────────────────────────────────────────────
    # Duplicate SL cleanup
    # ──────────────────────────────────────────────────────────────────────

    async def _cleanup_duplicate_sl_orders(self, chain_pos, sl_orders: list, all_orders: list):
        """Cancel duplicate SL orders for a position, keeping only the most recent one.

        This can happen when move_sl partially fails or when the bot restarts
        and re-places an SL that already exists on-chain.
        """
        if len(sl_orders) <= 1:
            return

        cfg = self.cfg
        if cfg.dry_run:
            self.logger.info(f"[DRY_RUN] Would clean up {len(sl_orders) - 1} duplicate SL order(s)")
            return

        # Keep the last SL order (most recently created), cancel the rest
        to_cancel = sl_orders[:-1]
        kept = sl_orders[-1]

        wid = getattr(chain_pos, '_wallet_id', 1)
        acct = self._get_account(wid)
        exchange = self.w3.eth.contract(
            address=Web3.to_checksum_address(cfg.exchange_router),
            abi=EXCHANGE_ROUTER_ABI,
        )
        wallet = Web3.to_checksum_address(acct.address)

        cancelled = 0
        for o in to_cancel:
            key_hex = o.get("key_hex")
            if not key_hex:
                continue
            try:
                key_bytes = bytes.fromhex(key_hex)
                data = exchange.encode_abi("cancelOrder", [key_bytes])
                tx = _open_mod.build_tx(self.w3, wallet, exchange.address, data, value=0)
                txh = _open_mod.sign_send(self.w3, acct, tx, dry_run=False)
                _open_mod.wait_receipt(self.w3, txh)
                cancelled += 1
                self.logger.info(
                    f"Cleaned up duplicate SL for {chain_pos.symbol}: "
                    f"cancelled @ ${o['trigger_price']:,.2f} (key=0x{key_hex[:12]}...)"
                )
            except Exception as e:
                self.logger.warning(f"Failed to cancel duplicate SL: {e}")

        if cancelled > 0:
            kept_price = kept.get('trigger_price', 0)
            await self.notify(
                f"🧹 {chain_pos.symbol}: Cleaned up {cancelled} duplicate SL order(s). "
                f"Kept SL @ ${kept_price:,.2f}"
            )

    # ──────────────────────────────────────────────────────────────────────
    # /close + confirmation handler
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_close(self, chat_id: int, arg: Optional[str]):
        cfg = self.cfg
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
            msg = ""
            open_pos_markets = {p.market.lower() for p in positions} if positions else set()

            if positions:
                msg += f"**Positions ({len(positions)})**\n"
                for i, pos in enumerate(positions, 1):
                    side = "LONG" if pos.is_long else "SHORT"
                    pos_orders = [o for o in orders if o["market"].lower() == pos.market.lower()]
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
                            price = o.get('trigger_price', 0) or 0
                            msg += f"    SL  @ ${price:,.2f}\n"
                        for j, o in enumerate(tp_orders, 1):
                            price = o.get('trigger_price', 0) or 0
                            msg += f"    TP{j} @ ${price:,.2f}\n"

            orphaned = [
                o for o in orders
                if o["market"].lower() not in open_pos_markets
            ] if orders else []
            if orphaned:
                order_type_names = {2: "MarketIncrease", 3: "LimitIncrease", 4: "MarketDecrease", 5: "TP", 6: "SL"}
                msg += f"\n**Orphaned Orders ({len(orphaned)}):**\n"
                for o in orphaned:
                    label = order_type_names.get(o.get("order_type", 0), f"Type{o.get('order_type', '?')}")
                    price = o.get('trigger_price', 0) or 0
                    sym = o.get('symbol', '???')
                    msg += f"  {sym} {label} @ ${price:,.2f}\n"

            msg += "\nReply with:\n"
            if positions:
                msg += "  /close BTC — close by symbol\n"
            msg += "  /close all — close all positions + cancel all orders"
            await self.send_message(chat_id, msg)
            return

        arg_upper = arg.upper()
        to_close: List[GMXPosition] = []
        also_cancel_orders = False

        if arg_upper == "ALL":
            to_close = positions if positions else []
            also_cancel_orders = True
        else:
            if positions:
                for pos in positions:
                    if arg_upper in pos.symbol.upper():
                        to_close.append(pos)
            if not to_close:
                await self.send_message(chat_id, f"No position found matching '{arg}'")
                return

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
        cfg = self.cfg
        text_upper = text.strip().upper()

        if chat_id not in self.pending_closes:
            return

        pending = self.pending_closes[chat_id]

        if time.time() - pending["created_at"] > 120:
            del self.pending_closes[chat_id]
            await self.send_message(chat_id, "Close request expired (2min). Use /close again.")
            return

        if text_upper in ("YES", "Y", "CONFIRM"):
            del self.pending_closes[chat_id]
            positions_to_close = pending["positions"]
            also_cancel_orders = pending.get("also_cancel_orders", False)

            if also_cancel_orders and not positions_to_close:
                await self.send_message(chat_id, "Closing all open orders...")
            elif also_cancel_orders and positions_to_close:
                await self.send_message(chat_id, "Closing all open positions & orders...")
            elif positions_to_close:
                labels = [f"{p.symbol} {'LONG' if p.is_long else 'SHORT'}" for p in positions_to_close]
                await self.send_message(chat_id, f"Closing {', '.join(labels)} & SL/TP...")

            close_failed = False
            if positions_to_close:
                for pos in positions_to_close:
                    side = "LONG" if pos.is_long else "SHORT"
                    pos_acct = getattr(pos, '_wallet_acct', self.account)
                    wid = getattr(pos, '_wallet_id', 1)
                    tx_hash = await self.execute_close(pos, 1.0, acct=pos_acct)
                    if tx_hash:
                        arb_url = f"https://arbiscan.io/tx/{tx_hash}" if not tx_hash.startswith("dry_run") else "DRY RUN"
                        unrealized = pos.unrealized_pnl if pos.unrealized_pnl else 0.0
                        pnl_sign = "+" if unrealized >= 0 else ""
                        pnl_pct = pos.pnl_percentage if pos.pnl_percentage else 0.0
                        current_str = f"${pos.current_price:,.2f}" if pos.current_price else "N/A"
                        entry_str = f"${pos.entry_price:,.2f}" if pos.entry_price else "N/A"
                        await self.send_message(
                            chat_id,
                            f"{pos.symbol} {side}\n"
                            f"Entry: {entry_str}  |  Current: {current_str}\n"
                            f"PnL: {pnl_sign}${unrealized:,.2f} ({pnl_sign}{pnl_pct:.1f}%)\n"
                            f"TX: {tx_hash}\n{arb_url}"
                        )

                        # Find the matching internal Position and close it properly.
                        # Mark is_open=False FIRST to prevent check_position_closed
                        # from racing and recording a duplicate trade.
                        matched = False
                        for internal_pos in self.positions.values():
                            if (internal_pos.is_open
                                    and internal_pos.market_addr
                                    and internal_pos.market_addr.lower() == pos.market.lower()
                                    and internal_pos.side == side
                                    and internal_pos.wallet_id == wid):
                                # Immediately mark closed to prevent race with
                                # check_position_closed running during our awaits
                                internal_pos.is_open = False
                                internal_pos.closed_at = time.time()
                                internal_pos.current_price = pos.current_price
                                internal_pos.unrealized_pnl = pos.unrealized_pnl
                                internal_pos.exit_reason = "manual"
                                self._record_trade(internal_pos, exit_reason="manual")
                                matched = True
                                break
                        if not matched:
                            self.logger.info(f"No internal position found for {pos.symbol} {side} [W{wid}] — skipping trade record")

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

            n_cancelled = 0
            try:
                exchange = self.w3.eth.contract(
                    address=Web3.to_checksum_address(cfg.exchange_router),
                    abi=EXCHANGE_ROUTER_ABI,
                )
                if also_cancel_orders:
                    for _, acct in self._all_wallets():
                        n_cancelled += await asyncio.to_thread(
                            cancel_all_orders, self.w3, acct, exchange, cfg.dry_run,
                        )
                elif positions_to_close:
                    for pos in positions_to_close:
                        pos_acct = getattr(pos, '_wallet_acct', self.account)
                        n_cancelled += await asyncio.to_thread(
                            cancel_orders_for_market, self.w3, pos_acct, exchange, pos.market, cfg.dry_run,
                        )
            except Exception as e:
                self.logger.error(f"Failed to cancel orders: {e}")
                await self.send_message(chat_id, f"Warning: could not cancel orders: {e}")

            if not close_failed:
                if also_cancel_orders and not positions_to_close:
                    await self.send_message(chat_id, f"Successfully cancelled {n_cancelled} open order(s).")
                elif also_cancel_orders and positions_to_close:
                    await self.send_message(chat_id, "Successfully closed all positions & orders.")
                elif positions_to_close:
                    labels = [f"{p.symbol} {'LONG' if p.is_long else 'SHORT'}" for p in positions_to_close]
                    await self.send_message(chat_id, f"Successfully closed {', '.join(labels)} & SL/TP.")

            if positions_to_close:
                await self.topup_eth_if_needed()
                await self._rebalance_wallets()

        elif text_upper in ("NO", "N", "CANCEL"):
            del self.pending_closes[chat_id]
            await self.send_message(chat_id, "Close cancelled.")

    # ──────────────────────────────────────────────────────────────────────
    # /increase + handler
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_increase(self, chat_id: int, amount_str: str = None):
        all_positions = []
        for wid, acct in self._all_wallets():
            try:
                chain_positions = await asyncio.to_thread(chain_fetch_positions, self.w3, acct.address)
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
        cfg = self.cfg
        pending = self.pending_increase.get(chat_id)
        if not pending:
            return

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

        additional_size = amount * cp.leverage

        await self.send_message(
            chat_id,
            f"Increasing {cp.symbol} {side} [W{wid}]\n"
            f"Adding: ${amount:.2f} collateral → ${additional_size:.2f} size @ {cp.leverage:.1f}x\n"
            "Executing..."
        )

        try:
            current_price = await asyncio.to_thread(fetch_current_price, cp.symbol, self.w3)
            exchange = self.w3.eth.contract(
                address=Web3.to_checksum_address(cfg.exchange_router),
                abi=EXCHANGE_ROUTER_ABI,
            )
            wallet = Web3.to_checksum_address(acct.address)
            market = Web3.to_checksum_address(cp.market)
            collateral_token = Web3.to_checksum_address(cfg.collateral_token)
            order_vault = Web3.to_checksum_address(cfg.order_vault)

            txh = await asyncio.to_thread(
                create_market_increase_order,
                self.w3, acct, exchange, wallet, market,
                collateral_token, order_vault, additional_size, amount,
                current_price, cp.symbol, cp.is_long,
                cfg.slippage_bps, cfg.execution_fee_wei, cfg.dry_run,
            )

            await self.send_message(
                chat_id,
                f"✅ {cp.symbol} {side} increased\n"
                f"Added ${amount:.2f} collateral (${additional_size:.2f} size)\n"
                f"TX: {txh}"
            )

            for pos in self.positions.values():
                if (pos.is_open and pos.market_addr
                        and pos.market_addr.lower() == cp.market.lower()
                        and pos.wallet_id == wid and pos.side == side):
                    pos.size_usd += additional_size
                    # Recalculate leverage from new on-chain size/collateral
                    new_collateral = pos.size_usd / pos.leverage + amount
                    if new_collateral > 0:
                        pos.leverage = pos.size_usd / new_collateral
                    break

        except Exception as e:
            self.logger.error(f"Increase failed: {e}\n{traceback.format_exc()}")
            await self.send_message(chat_id, f"Failed to increase position: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # /lastmsg
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_lastmsg(self, chat_id: int):
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

    # ──────────────────────────────────────────────────────────────────────
    # /lastsignal
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_lastsignal(self, chat_id: int):
        """Re-run the last parsed signal through process_signal()."""
        if not self.last_signal_text:
            await self.send_message(chat_id, "No signal stored yet. Wait for a signal from the channel first.")
            return

        preview = self.last_signal_text[:200]
        await self.send_message(chat_id, f"Re-running last signal:\n\n{preview}\n\nExecuting...")
        await self.process_signal(self.last_signal_text)

    # ──────────────────────────────────────────────────────────────────────
    # /tradesize
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_tradesize(self, chat_id: int, arg: Optional[str] = None):
        cfg = self.cfg
        try:
            if not arg or not arg.strip():
                total_portfolio = await self._get_total_portfolio_value()
                collateral = total_portfolio * cfg.portfolio_pct
                pct_display = cfg.portfolio_pct * 100
                msg = (
                    "**Trade Size**\n\n"
                    f"Portfolio: ${total_portfolio:,.2f}\n"
                    f"Trade size: {pct_display:.0f}% → ${collateral:,.2f} collateral per trade\n\n"
                    "Usage: `/tradesize <percent>` to change\n"
                    "Example: `/tradesize 15` → 15% per trade"
                )
                await self.send_message(chat_id, msg)
                return

            new_val = float(arg.strip().replace("%", ""))
            new_pct = new_val / 100.0 if new_val > 1.0 else new_val

            if new_pct < 0.01 or new_pct > 0.50:
                await self.send_message(chat_id, "Trade size must be between 1% and 50%.")
                return

            old_pct = cfg.portfolio_pct
            cfg.portfolio_pct = new_pct

            total_portfolio = await self._get_total_portfolio_value()
            new_collateral = total_portfolio * cfg.portfolio_pct

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

    # ──────────────────────────────────────────────────────────────────────
    # Daily summary loop & sender
    # ──────────────────────────────────────────────────────────────────────

    async def daily_summary_loop(self):
        ET = ZoneInfo("America/New_York")
        while True:
            try:
                now = datetime.now(ET)
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
                await asyncio.sleep(3600)

    # Last known PnL snapshot for change detection (set by send_hourly_pnl)
    _last_pnl_snapshot: Optional[dict] = None

    async def hourly_pnl_loop(self):
        """Send a PnL Update between 9 AM and 11 PM ET, weekdays only.

        Only sends if PnL has changed since the last update.
        Also records a balance snapshot every hour for 24h tracking.
        """
        while True:
            try:
                await asyncio.sleep(3600)  # 1 hour

                # Record balance snapshot every hour (for /balance 24h change)
                try:
                    total_portfolio = await self._get_total_portfolio_value()
                    self._save_balance_snapshot(total_portfolio)
                except Exception as e:
                    self.logger.debug(f"Balance snapshot failed: {e}")

                # Silent hourly re-sync from on-chain (under signal lock to avoid races)
                try:
                    async with self._signal_lock:
                        await self._sync_on_chain_positions()
                    self.logger.debug("Hourly position sync complete")
                except Exception as e:
                    self.logger.debug(f"Hourly position sync failed: {e}")

                ET = ZoneInfo("America/New_York")
                now_et = datetime.now(ET)
                hour = now_et.hour
                weekday = now_et.weekday()  # 0=Mon … 6=Sun

                # Skip weekends (Sat=5, Sun=6)
                if weekday >= 5:
                    self.logger.debug(f"PnL Update skipped — weekend ({now_et.strftime('%A')})")
                    continue

                if 9 <= hour <= 22:  # 9 AM to 11 PM ET
                    await self.send_hourly_pnl()
                else:
                    self.logger.debug(f"PnL Update skipped — outside 9AM-11PM ET (currently {hour}:00)")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"PnL Update loop error: {e}")
                await asyncio.sleep(3600)

    async def send_hourly_pnl(self):
        """Build and send the PnL Update alert.

        Uses on-chain event logs for realized trade history (all wallets combined)
        and on-chain data for open position unrealized PnL.

        Only sends if PnL has changed since the last update.
        """
        ET = ZoneInfo("America/New_York")
        now = datetime.now(ET)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_cutoff = int(today_start.timestamp())

        PNL_SYMBOLS = {"BTC", "ETH", "SOL"}

        # Build reverse map: market_address (lower) → symbol
        market_to_sym = {}
        for sym, addr in self.cfg.markets.items():
            if sym in PNL_SYMBOLS:
                market_to_sym[addr.lower()] = sym

        # ── Today's realized trades from on-chain (all wallets, stored locally) ──
        reset_ts = self._get_pnl_reset_ts()
        since = max(today_cutoff, reset_ts) if reset_ts else today_cutoff

        all_stored = await self._fetch_and_store_trades()
        today_trades = [t for t in all_stored if t.get("timestamp", 0) >= since]

        # Filter to PNL_SYMBOLS, exclude dust (< $1), and sum.
        # Use net_pnl_usd (includes fees + price impact) with fallback to
        # pnl_usd for older stored trades that lack the field.
        realized_pnl = 0.0
        realized_count = 0
        for t in today_trades:
            pnl = t.get("net_pnl_usd", t.get("pnl_usd", 0))
            if abs(pnl) < 1:
                continue
            sym = market_to_sym.get((t.get("market_address") or "").lower())
            if sym:
                realized_pnl += pnl
                realized_count += 1

        # ── Open positions on-chain (unrealized PnL) ──
        unrealized_pnl = 0.0
        open_count = 0
        open_lines = []
        try:
            for wid, acct in self._all_wallets():
                cps = await asyncio.to_thread(chain_fetch_positions, self.w3, acct.address)
                for cp in cps:
                    sym = cp.symbol.upper().split("/")[0]
                    if sym not in PNL_SYMBOLS:
                        continue
                    unrealized_pnl += cp.unrealized_pnl
                    open_count += 1
                    side = "LONG" if cp.is_long else "SHORT"
                    # Realized PnL from executed TPs (calculate from TP data)
                    pos_realized = 0.0
                    for ip in self.positions.values():
                        if (ip.is_open and ip.market_addr
                                and ip.market_addr.lower() == cp.market.lower()
                                and ip.side == side and ip.wallet_id == wid):
                            for tp in ip.take_profits:
                                if tp.executed and cp.entry_price and cp.entry_price > 0:
                                    tp_size = ip.size_usd * tp.percentage
                                    if cp.is_long:
                                        pos_realized += ((tp.price - cp.entry_price) / cp.entry_price) * tp_size
                                    else:
                                        pos_realized += ((cp.entry_price - tp.price) / cp.entry_price) * tp_size
                            break
                    total_pos = cp.unrealized_pnl + pos_realized
                    t_sign = "+" if total_pos >= 0 else ""
                    detail = ""
                    if pos_realized:
                        r_s = "+" if pos_realized >= 0 else ""
                        u_s = "+" if cp.unrealized_pnl >= 0 else ""
                        detail = f" (rlz: {r_s}${pos_realized:,.2f}, unrlz: {u_s}${cp.unrealized_pnl:,.2f})"
                    open_lines.append(
                        f"  {sym} {side} [W{wid}]: {t_sign}${total_pos:,.2f}{detail}"
                    )
        except Exception as e:
            self.logger.warning(f"PnL Update: could not fetch positions: {e}")

        # Today's total = closed trades + open unrealized
        today_total = realized_pnl + unrealized_pnl

        # ── Change detection: skip if nothing changed ──
        current_snapshot = {
            "realized_pnl": round(realized_pnl, 2),
            "realized_count": realized_count,
            "unrealized_pnl": round(unrealized_pnl, 2),
            "open_count": open_count,
        }
        if self._last_pnl_snapshot == current_snapshot:
            self.logger.debug("PnL Update skipped — no change since last update")
            return
        self._last_pnl_snapshot = current_snapshot

        # Build message
        r_sign = "+" if realized_pnl >= 0 else ""
        u_sign = "+" if unrealized_pnl >= 0 else ""
        t_sign = "+" if today_total >= 0 else ""

        msg = f"PnL Update — {now.strftime('%I:%M %p ET')}\n\n"

        msg += f"**Today ({realized_count} closed)**\n"
        msg += f"  Closed:     {r_sign}${realized_pnl:,.2f}\n"
        msg += f"  Unrealized: {u_sign}${unrealized_pnl:,.2f} ({open_count} open)\n"
        if open_lines:
            msg += "\n".join(open_lines) + "\n"
        msg += f"  Today Total: {t_sign}${today_total:,.2f}"

        await self.notify(msg)
        self.logger.info(f"PnL Update sent: today={t_sign}${today_total:,.2f}")

    async def send_daily_summary(self):
        """Build and send the daily summary using on-chain trade data.

        Uses the same on-chain data source as /pnl and PnL Update
        (fetched via _fetch_and_store_trades) for accurate numbers.
        """
        ET = ZoneInfo("America/New_York")
        now = datetime.now(ET)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_cutoff = int(today_start.timestamp())

        PNL_SYMBOLS = {"BTC", "ETH", "SOL"}

        # Build reverse map: market_address (lower) → symbol
        market_to_sym = {}
        for sym, addr in self.cfg.markets.items():
            if sym in PNL_SYMBOLS:
                market_to_sym[addr.lower()] = sym

        # ── Fetch on-chain trades (merged with local store) ──
        all_stored = await self._fetch_and_store_trades()
        reset_ts = self._get_pnl_reset_ts()
        all_trades = [t for t in all_stored if t.get("timestamp", 0) >= reset_ts] if reset_ts else all_stored

        def _net(t):
            return t.get("net_pnl_usd", t.get("pnl_usd", 0))

        # Tag trades with symbol, exclude dust (< $1)
        tagged = []
        for t in all_trades:
            if abs(_net(t)) < 1:
                continue
            sym = market_to_sym.get((t.get("market_address") or "").lower())
            if sym:
                entry = dict(t)
                entry["_sym"] = sym
                tagged.append(entry)

        todays_trades = [t for t in tagged if t.get("timestamp", 0) >= today_cutoff]

        # ── Daily stats ──
        daily_pnl = sum(_net(t) for t in todays_trades)
        daily_wins = sum(1 for t in todays_trades if _net(t) > 0)
        daily_losses = sum(1 for t in todays_trades if _net(t) <= 0)
        daily_count = len(todays_trades)
        daily_winrate = (daily_wins / daily_count * 100) if daily_count else 0.0

        # ── Lifetime stats (since reset) ──
        lifetime_pnl = sum(_net(t) for t in tagged)
        lifetime_wins = sum(1 for t in tagged if _net(t) > 0)
        lifetime_losses = sum(1 for t in tagged if _net(t) <= 0)
        lifetime_count = len(tagged)
        lifetime_winrate = (lifetime_wins / lifetime_count * 100) if lifetime_count else 0.0

        # ── Per-symbol breakdown (today) ──
        symbol_lines = []
        for sym in ("BTC", "ETH", "SOL"):
            sym_trades = [t for t in todays_trades if t["_sym"] == sym]
            if sym_trades:
                sym_pnl = sum(_net(t) for t in sym_trades)
                sym_sign = "+" if sym_pnl >= 0 else ""
                sym_w = sum(1 for t in sym_trades if _net(t) > 0)
                symbol_lines.append(f"  {sym}: {sym_sign}${sym_pnl:,.2f} ({sym_w}/{len(sym_trades)} wins)")

        # ── Wallet balances ──
        balance_lines = []
        total_usdc = 0.0
        total_deployed = 0.0
        try:
            for wid, acct in self._all_wallets():
                usdc = await asyncio.to_thread(self._get_portfolio_value_for, acct)
                total_usdc += usdc
                try:
                    positions = await asyncio.to_thread(chain_fetch_positions, self.w3, acct.address)
                    deployed = sum(p.collateral_amount for p in positions) if positions else 0.0
                except Exception:
                    deployed = 0.0
                total_deployed += deployed
                addr = f"{acct.address[:8]}...{acct.address[-6:]}"
                balance_lines.append(f"  W{wid} ({addr}): ${usdc:,.2f} USDC")
        except Exception as e:
            self.logger.warning(f"Daily summary: balance fetch failed: {e}")
            balance_lines.append("  (could not fetch balances)")

        # ── Open positions (unrealized PnL) ──
        open_pnl = 0.0
        open_count = 0
        try:
            for _, acct in self._all_wallets():
                cps = await asyncio.to_thread(chain_fetch_positions, self.w3, acct.address)
                for cp in cps:
                    sym = cp.symbol.upper().split("/")[0]
                    if sym in PNL_SYMBOLS:
                        open_pnl += cp.unrealized_pnl
                        open_count += 1
        except Exception:
            pass

        d_sign = "+" if daily_pnl >= 0 else ""
        l_sign = "+" if lifetime_pnl >= 0 else ""
        o_sign = "+" if open_pnl >= 0 else ""

        msg = (
            f"Daily Summary — {now.strftime('%b %d, %Y')}\n\n"
            f"**Today ({daily_count} trades)**\n"
            f"  PnL: {d_sign}${daily_pnl:,.2f}\n"
            f"  Win Rate: {daily_winrate:.0f}% ({daily_wins}W / {daily_losses}L)\n"
        )
        if symbol_lines:
            msg += "\n".join(symbol_lines) + "\n"

        msg += (
            f"\n**Lifetime ({lifetime_count} trades)**\n"
            f"  PnL: {l_sign}${lifetime_pnl:,.2f}\n"
            f"  Win Rate: {lifetime_winrate:.0f}% ({lifetime_wins}W / {lifetime_losses}L)\n"
        )

        if open_count:
            msg += f"\n**Open Positions ({open_count})**\n  Unrealized: {o_sign}${open_pnl:,.2f}\n"

        msg += "\n**Account Balance**\n" + "\n".join(balance_lines) + "\n"
        msg += f"  Total USDC: ${total_usdc:,.2f}\n  Deployed: ${total_deployed:,.2f}"

        await self.notify(msg)
        self.logger.info(f"Daily summary sent: daily PnL={d_sign}${daily_pnl:,.2f}")
