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
  - WalletMixin (wallet_mgmt.py): cmd_balance(), cmd_topup(), cmd_balance_wallets()
  - PriceFeedsMixin (price_feeds.py): cmd_prices()
  - AnalyticsMixin (analytics.py): cmd_winrate(), cmd_pnl(), cmd_health()

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

/balance — Wallet balances (auto-rebalances)
/close — Close positions
/collateral — Add or remove collateral (+/- amount)
/gas — Swap USDC to ETH for gas (shows gas balances with no args)
/pnl — Push hourly PnL update now
/positions — Show on-chain positions & orders
/signals — Recent signals (pick one to open)
/sl — Move stop loss
/status — Bot status, health & halt/resume
/trades — Trade history & PnL report (PDF)
/tradesize — Show/change trade size
/wallet — Deposit (d) or withdraw (w) USDC

**Wallets:** W1=swing, W2-W4=scalps"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CoreTelegramMixin — core Telegram methods for GMXBot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CoreTelegramMixin:
    """Mixin providing Telegram init, command routing, and core commands.

    Expected attributes on the host class (GMXBot):
        cfg, client, w3, account, account2, account3, account4,
        positions, trade_history, pending_closes, pending_collateral,
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
        cmd_balance(), cmd_topup(), cmd_balance_wallets() — from WalletMixin
        cmd_prices() — from PriceFeedsMixin
        cmd_winrate(), cmd_pnl(), cmd_health() — from AnalyticsMixin
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
                if not event.message.text:
                    return
                text = event.message.text.strip()
                if text.startswith("/"):
                    return
                sender = await event.get_sender()
                username = (getattr(sender, "username", "") or "").lower()
                if cfg.admin_usernames and username not in cfg.admin_usernames:
                    return
                if event.chat_id in self.pending_withdraw:
                    await self.handle_withdraw_reply(event.chat_id, text)
                elif event.chat_id in self.pending_collateral:
                    await self.handle_collateral_reply(event.chat_id, text)
                elif event.chat_id in self.pending_signals:
                    await self.handle_signals_reply(event.chat_id, text)
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
                    elif chat_id in self.pending_collateral:
                        await self.handle_collateral_reply(chat_id, text)
                    elif chat_id in self.pending_signals:
                        await self.handle_signals_reply(chat_id, text)
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

            if cmd in ("/help", "/start"):
                await self.send_message(chat_id, HELP_TEXT)
            elif cmd == "/status":
                arg = parts[1].lower() if len(parts) > 1 else None
                await self.cmd_status(chat_id, arg)
            elif cmd == "/positions":
                await self.cmd_positions(chat_id)
            elif cmd == "/close":
                arg = parts[1] if len(parts) > 1 else None
                await self.cmd_close(chat_id, arg)
            elif cmd == "/confirm":
                if chat_id in self.pending_withdraw:
                    await self.handle_withdraw_reply(chat_id, "CONFIRM")
                elif chat_id in self.pending_collateral:
                    await self.handle_collateral_reply(chat_id, "CONFIRM")
                else:
                    await self.handle_close_confirmation(chat_id, "YES")
            elif cmd == "/balance":
                await self.cmd_balance(chat_id)
            elif cmd == "/pnl":
                await self.send_message(chat_id, "Fetching PnL data...")
                await self.send_hourly_pnl()
            elif cmd in ("/lastsignal", "/signals"):
                await self.cmd_signals(chat_id)
            elif cmd == "/collateral":
                arg = parts[1] if len(parts) > 1 else None
                await self.cmd_collateral(chat_id, arg)
            elif cmd == "/gas":
                arg = " ".join(parts[1:]) if len(parts) > 1 else None
                await self.cmd_topup(chat_id, arg)
            elif cmd == "/sl":
                arg = " ".join(parts[1:]) if len(parts) > 1 else None
                await self.cmd_sl(chat_id, arg)
            elif cmd == "/tradesize":
                arg = " ".join(parts[1:]) if len(parts) > 1 else None
                await self.cmd_tradesize(chat_id, arg)
            elif cmd == "/trades":
                await self.cmd_pdf(chat_id)
            elif cmd == "/wallet":
                arg = " ".join(parts[1:]) if len(parts) > 1 else None
                await self.cmd_wallet(chat_id, arg)
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
                elif chat_id in self.pending_collateral:
                    del self.pending_collateral[chat_id]
                    await self.send_message(chat_id, "Collateral change cancelled.")
                elif chat_id in self.pending_signals:
                    del self.pending_signals[chat_id]
                    await self.send_message(chat_id, "Signal selection cancelled.")
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

    async def cmd_status(self, chat_id: int, arg: str = None):
        # Handle halt/resume sub-commands
        if arg == "halt":
            await self.halt_trading("Manual halt")
            return
        if arg == "resume":
            await self.resume_trading("Manual resume")
            return

        cfg = self.cfg
        health = self.get_health_report()
        is_halted = health["is_halted"]
        status = "HALTED" if is_halted else "ACTIVE"
        uptime_hours = health["uptime_seconds"] / 3600
        heartbeat_age = time.time() - self.last_heartbeat
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
            f"Uptime: {uptime_hours:.1f}h\n"
            f"Heartbeat: {heartbeat_age:.0f}s ago\n\n"
            f"Positions: {health['open_positions']}/{health['total_positions']}\n"
            f"Exposure: ${total_exposure:.0f}\n"
            f"Price updates: {health['price_updates']}\n"
            f"Signals: {health['signals_processed']}\n"
            f"Trades: {health['trades_executed']}\n"
            f"Errors: {health['errors']}"
        )
        if is_halted:
            msg += f"\n\nHalt reason: {self.halt_reason}"
            msg += "\n\nUse `/status resume` to resume trading."
        else:
            msg += "\n\nUse `/status halt` to halt trading."
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

                wid = getattr(pos, '_wallet_id', 1)
                pos_orders = [o for o in orders
                              if o["market"].lower() == pos.market.lower()
                              and o.get("_wallet_id", 1) == wid]
                tp_orders = sorted([o for o in pos_orders if o["order_type"] == 5],
                                   key=lambda o: o["trigger_price"])
                sl_orders = [o for o in pos_orders if o["order_type"] == 6]
                limit_orders = [o for o in pos_orders if o["order_type"] in (2, 3)]

                wid_label = ""
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

                # TP hit info from internal state — use verified_decreases as source of truth
                tp_hits = 0
                total_tps = 0
                sl_label = None
                realized_pnl = 0.0
                if internal:
                    # Use verified_decreases as source of truth for TP hits
                    tp_hits = internal.tp_hits_count
                    total_tps = len([tp for tp in internal.take_profits if tp.price > 0])
                    tp_hits = min(tp_hits, total_tps)
                    sl_label = internal.sl_move_label
                    # Use verified realized PnL from internal state (validated by sync)
                    realized_pnl = internal.realized_pnl if tp_hits > 0 else 0.0

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
                    msg += f"  Total PnL: {t_sign}${total_pnl_combined:,.2f}\n"
                else:
                    msg += f"  PnL:     {pnl_icon}${pnl:,.2f} ({pnl_icon}{pnl_pct:.1f}%)\n"
                    if fee_line:
                        msg += fee_line

                if sl_orders or tp_orders or (internal and tp_hits > 0):
                    msg += "  TP & SL:\n"

                    # Show verified hit TPs first with checkmark
                    hit_tp_num = 0
                    if internal and internal.verified_decreases:
                        # Build hit TPs from verified_decreases (source of truth)
                        hit_prices = [
                            d.get("matched_tp_price", 0)
                            for d in internal.verified_decreases
                            if d.get("matched_tp_price", 0) > 0
                        ]
                        hit_prices_sorted = sorted(hit_prices, reverse=(not pos.is_long))
                        for hp in hit_prices_sorted:
                            hit_tp_num += 1
                            msg += f"  TP{hit_tp_num} ✅ ${hp:,.2f} — HIT\n"

                    # Remaining on-chain TPs — numbered after hit TPs
                    sorted_tps = sorted(
                        tp_orders,
                        key=lambda x: x.get("trigger_price", 0) or 0,
                        reverse=not pos.is_long,
                    )
                    tp_num_offset = hit_tp_num  # offset so TP numbering continues
                    # Total TP size across all orders — use this as denominator so
                    # percentages stay correct even after adding/removing collateral.
                    total_tp_size = sum(o.get("size_usd", 0) or 0 for o in sorted_tps)
                    for j, o in enumerate(sorted_tps, 1):
                        tp_num = tp_num_offset + j
                        tp_price = o.get("trigger_price", 0) or 0

                        # % of position closing at this TP
                        tp_size = o.get("size_usd", 0) or 0
                        close_pct_str = ""
                        if tp_size > 0 and total_tp_size > 0:
                            close_pct = (tp_size / total_tp_size) * 100
                            fmt = f"{close_pct:.0f}" if close_pct >= 1 else f"{close_pct:.1f}"
                            close_pct_str = f" (closes {fmt}%)"
                        elif internal:
                            hit_tp_prices = {d.get("matched_tp_price", 0) for d in internal.verified_decreases}
                            remaining_tps = [t for t in internal.take_profits if t.price not in hit_tp_prices]
                            remaining_tps_sorted = sorted(remaining_tps, key=lambda t: t.price, reverse=(not pos.is_long))
                            if j - 1 < len(remaining_tps_sorted):
                                close_pct_str = f" (closes {remaining_tps_sorted[j-1].percentage:.0%})"

                        if tp_price and pos.entry_price and pos.entry_price > 0:
                            price_chg = ((tp_price - pos.entry_price) / pos.entry_price) * 100

                            if pos.is_long:
                                pnl_per_dollar = (tp_price - pos.entry_price) / pos.entry_price
                            else:
                                pnl_per_dollar = (pos.entry_price - tp_price) / pos.entry_price

                            tp_close_size = tp_size if tp_size > 0 else pos.size_usd
                            proj = pnl_per_dollar * tp_close_size

                            collateral = pos.size_usd / pos.leverage if pos.leverage else pos.size_usd
                            tp_collateral = collateral * (tp_size / pos.size_usd) if tp_size > 0 and pos.size_usd > 0 else collateral
                            pnl_pct = (proj / tp_collateral * 100) if tp_collateral > 0 else 0
                            pnl_pct_sign = "+" if pnl_pct >= 0 else ""

                            proj_sign = "+" if proj >= 0 else ""
                            chg_sign = "+" if price_chg >= 0 else ""
                            sym = pos.symbol or ""
                            msg += (
                                f"  TP{tp_num}{close_pct_str} @ ${tp_price:,.2f}\n"
                                f"     ({pnl_pct_sign}{pnl_pct:.1f}% PnL, {proj_sign}${proj:,.2f} projected, {sym} {chg_sign}{price_chg:.2f}%)\n"
                            )
                        elif tp_price:
                            msg += f"  TP{tp_num}{close_pct_str} @ ${tp_price:,.2f}\n"
                        else:
                            msg += f"  TP{tp_num} @ unknown\n"

                    # SL at the bottom — deduplicate if multiple exist on-chain
                    if len(sl_orders) > 1:
                        msg += f"  ⚠️ {len(sl_orders)} SL orders found\n"
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
            total_collateral = sum(p.collateral_amount for p in positions)
            pnl_pct_str = f" ({total_pnl / total_collateral * 100:+.1f}%)" if total_collateral > 0 else ""
            any_onchain = any(getattr(p, 'pnl_source', 'local') == "onchain" for p in positions)
            pnl_tag = " (on-chain)" if any_onchain else ""
            msg += f"\nTotal Size: ${total_size:,.2f}  |  Collateral: ${total_collateral:,.2f}  |  PnL: ${total_pnl:+.2f}{pnl_pct_str}{pnl_tag}\n"

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
                    wid_c = getattr(pos, '_wallet_id', 1)
                    pos_orders = [o for o in orders
                                  if o["market"].lower() == pos.market.lower()
                                  and o.get("_wallet_id", 1) == wid_c]
                    tp_orders = sorted([o for o in pos_orders if o["order_type"] == 5],
                                       key=lambda o: o["trigger_price"])
                    sl_orders = [o for o in pos_orders if o["order_type"] == 6]
                    pnl_pct_c = pos.pnl_percentage if hasattr(pos, 'pnl_percentage') else 0.0
                    msg += (
                        f"\n**#{i} {pos.symbol} {side}**\n"
                        f"  Size: ${pos.size_usd:,.2f} @ {pos.leverage:.1f}x\n"
                        f"  PnL:  ${pos.unrealized_pnl:+.2f} ({pnl_pct_c:+.1f}%)\n"
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
                    if arg_upper == pos.symbol.upper() or arg_upper == pos.symbol.upper().split("/")[0]:
                        to_close.append(pos)
            if not to_close:
                await self.send_message(chat_id, f"No position found matching '{arg}'")
                return

        msg = "**Confirm close:**\n\n"
        if to_close:
            for pos in to_close:
                side = "LONG" if pos.is_long else "SHORT"
                pnl_pct_c = pos.pnl_percentage if hasattr(pos, 'pnl_percentage') else 0.0
                msg += f"  {pos.symbol} {side} — ${pos.size_usd:,.2f} — PnL: ${pos.unrealized_pnl:+.2f} ({pnl_pct_c:+.1f}%)\n"
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
                                await self._record_trade(internal_pos, exit_reason="manual")
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
    # /collateral + handler
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_collateral(self, chat_id: int, amount_str: str = None):
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

        msg = "**Adjust collateral:**\n\n"
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
            msg += f"Amount: {amount_str} USDC\nReply with position number (1-{len(all_positions)})"
        else:
            msg += (
                "Reply with: <number> <+/-amount>\n"
                "Example: 1 +25  (add $25 collateral)\n"
                "Example: 1 -25  (remove $25 collateral)"
            )

        self.pending_collateral[chat_id] = {
            "positions": all_positions,
            "amount": amount_str if amount_str else None,
            "created_at": time.time(),
        }
        await self.send_message(chat_id, msg)

    async def handle_collateral_reply(self, chat_id: int, text: str):
        cfg = self.cfg
        pending = self.pending_collateral.get(chat_id)
        if not pending:
            return

        if time.time() - pending["created_at"] > 120:
            del self.pending_collateral[chat_id]
            await self.send_message(chat_id, "Collateral request expired (2min). Use /collateral again.")
            return

        text = text.strip().upper()
        if text in ("CANCEL", "NO", "N"):
            del self.pending_collateral[chat_id]
            await self.send_message(chat_id, "Collateral change cancelled.")
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

        # Parse amount with +/- sign
        raw_amount = pending.get("amount")
        if raw_amount is None:
            try:
                raw_amount = parts[1]
            except IndexError:
                await self.send_message(chat_id, "Include amount: e.g. '1 +25' or '1 -25'")
                return

        try:
            amount_val = float(raw_amount)
        except ValueError:
            await self.send_message(chat_id, f"Invalid amount: {raw_amount}")
            del self.pending_collateral[chat_id]
            return

        if amount_val == 0:
            await self.send_message(chat_id, "Amount cannot be zero.")
            del self.pending_collateral[chat_id]
            return

        is_increase = amount_val > 0
        amount = abs(amount_val)

        wid, acct, cp = positions[idx]
        side = "LONG" if cp.is_long else "SHORT"
        del self.pending_collateral[chat_id]

        if is_increase:
            await self._collateral_increase(chat_id, cfg, wid, acct, cp, side, amount)
        else:
            await self._collateral_decrease(chat_id, cfg, wid, acct, cp, side, amount)

    async def _collateral_increase(self, chat_id, cfg, wid, acct, cp, side, amount):
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
                f"✅ {cp.symbol} {side} collateral increased\n"
                f"Added ${amount:.2f} collateral (${additional_size:.2f} size)\n"
                f"TX: {txh}"
            )

            for pos in self.positions.values():
                if (pos.is_open and pos.market_addr
                        and pos.market_addr.lower() == cp.market.lower()
                        and pos.wallet_id == wid and pos.side == side):
                    old_collateral = pos.size_usd / pos.leverage if pos.leverage > 0 else pos.size_usd
                    pos.size_usd += additional_size
                    new_collateral = old_collateral + amount
                    if new_collateral > 0:
                        pos.leverage = pos.size_usd / new_collateral
                    break

        except Exception as e:
            self.logger.error(f"Collateral increase failed: {e}\n{traceback.format_exc()}")
            await self.send_message(chat_id, f"Failed to increase collateral: {e}")

    async def _collateral_decrease(self, chat_id, cfg, wid, acct, cp, side, amount):
        if amount >= cp.collateral_amount * 0.95:
            await self.send_message(
                chat_id,
                f"Cannot remove ${amount:.2f} — collateral is only ${cp.collateral_amount:.2f}.\n"
                f"Max removable: ${cp.collateral_amount * 0.90:.2f}. Use /close for full exit."
            )
            return

        decrease_pct = amount / cp.collateral_amount
        size_reduction = cp.size_usd * decrease_pct

        await self.send_message(
            chat_id,
            f"Decreasing {cp.symbol} {side} [W{wid}]\n"
            f"Removing: ${amount:.2f} collateral → -${size_reduction:.2f} size @ {cp.leverage:.1f}x\n"
            "Executing..."
        )

        try:
            txh = await asyncio.to_thread(
                create_close_order,
                self.w3, acct, cp,
                percentage=decrease_pct,
                dry_run=cfg.dry_run,
            )

            await self.send_message(
                chat_id,
                f"✅ {cp.symbol} {side} collateral decreased\n"
                f"Removed ${amount:.2f} collateral (-${size_reduction:.2f} size)\n"
                f"TX: {txh}"
            )

            for pos in self.positions.values():
                if (pos.is_open and pos.market_addr
                        and pos.market_addr.lower() == cp.market.lower()
                        and pos.wallet_id == wid and pos.side == side):
                    pos.size_usd -= size_reduction
                    break

        except Exception as e:
            self.logger.error(f"Collateral decrease failed: {e}\n{traceback.format_exc()}")
            await self.send_message(chat_id, f"Failed to decrease collateral: {e}")

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
    # /signals — show last 5 actionable signals, prompt to open
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_signals(self, chat_id: int):
        """Show last 5 actionable signals (store first, then channel scan), prompt to open."""
        from datetime import datetime
        from open import parse_signal
        from config import ALLOWED_SYMBOLS

        # Count how many wallets have each symbol+side open.
        # A signal is only "fully occupied" if ALL eligible wallets are busy.
        pair_wallet_count = {}  # (symbol, side) -> set of wallet_ids that have it
        for p in self.positions.values():
            if p.is_open:
                key = (p.symbol, p.side)
                pair_wallet_count.setdefault(key, set()).add(p.wallet_id)
        for wid, acct in self._all_wallets():
            try:
                chain_pos = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                for cp in chain_pos:
                    side = "LONG" if cp.is_long else "SHORT"
                    sym = cp.symbol if hasattr(cp, "symbol") else ""
                    if sym:
                        pair_wallet_count.setdefault((sym, side), set()).add(wid)
            except Exception:
                pass

        scalp_wallet_count = len(self._scalp_wallets()) or len(self._all_wallets())

        def _is_fully_occupied(symbol: str, side: str) -> bool:
            """True only if ALL eligible wallets already have this symbol+side."""
            occupied = len(pair_wallet_count.get((symbol, side), set()))
            return occupied >= scalp_wallet_count

        # ── Source 1: Signal store (already parsed & recorded) ──
        actionable = []  # list of (raw_text, parsed_signal, source_label, timestamp)
        seen_fingerprints = set()

        # Build set of open position fingerprints + signal IDs for filtering
        open_position_ids = set()
        open_position_fps = set()
        for p in self.positions.values():
            if p.is_open:
                if p.signal_id:
                    open_position_ids.add(p.signal_id)
                # Also fingerprint by entry price to catch positions without signal_id
                tp_prices = tuple(sorted(tp.price for tp in p.take_profits)) if p.take_profits else ()
                open_position_fps.add((p.symbol, p.side, tp_prices))

        for sig in self.signal_store.get_recent(20):
            # Skip signals that already have an open position (by signal_id or fingerprint)
            if sig.signal_id in open_position_ids:
                continue
            sig_tp_prices = tuple(sorted(tp["price"] for tp in sig.take_profits))
            if (sig.symbol, sig.side, sig_tp_prices) in open_position_fps:
                continue
            if _is_fully_occupied(sig.symbol, sig.side):
                continue
            tp_prices = tuple(sorted(tp["price"] for tp in sig.take_profits))
            fp = (sig.symbol, sig.side, tp_prices)
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)

            # Re-parse to get a proper Signal object for display
            try:
                parsed = parse_signal(sig.raw_text)
            except Exception:
                continue

            source = sig.source_channel or "store"
            actionable.append((sig.raw_text, parsed, source, sig.timestamp_received))
            if len(actionable) >= 5:
                break

        # ── Source 2: Channel history (catches signals that failed to parse before) ──
        if len(actionable) < 5:
            await self.send_message(chat_id, "Scanning channels for more signals...")
            for ch_id, ch_name in self.resolved_channels.items():
                try:
                    msgs = await self.client.get_messages(ch_id, limit=100)
                    for msg in msgs:
                        if not msg or not msg.text:
                            continue
                        text = msg.text.strip()
                        if len(text) < 10:
                            continue
                        if is_update_message(text):
                            self.logger.debug(f"/signals skip (update): {text[:60]}")
                            continue
                        try:
                            signal = parse_signal(text)
                        except Exception as e:
                            self.logger.debug(f"/signals skip (parse fail): {e} | {text[:60]}")
                            continue
                        if signal.symbol not in ALLOWED_SYMBOLS:
                            self.logger.debug(f"/signals skip (symbol {signal.symbol}): {text[:60]}")
                            continue
                        if _is_fully_occupied(signal.symbol, signal.side):
                            self.logger.debug(f"/signals skip (full): {signal.symbol} {signal.side}")
                            continue
                        tp_prices = tuple(sorted(tp.price for tp in signal.take_profits))
                        fp = (signal.symbol, signal.side, tp_prices)
                        # Skip if this matches an already-open position
                        if fp in open_position_fps:
                            self.logger.debug(f"/signals skip (already open): {signal.symbol} {signal.side}")
                            continue
                        if fp in seen_fingerprints:
                            self.logger.debug(f"/signals skip (dedup): {signal.symbol} {signal.side}")
                            continue
                        seen_fingerprints.add(fp)

                        ts = msg.date.timestamp() if msg.date else time.time()
                        actionable.append((text, signal, ch_name, ts))
                        if len(actionable) >= 5:
                            break
                except Exception as e:
                    self.logger.warning(f"Failed to scan {ch_name}: {e}")
                if len(actionable) >= 5:
                    break

        if not actionable:
            # Show debug info about what was checked
            store_count = len(self.signal_store.get_recent(20))
            await self.send_message(
                chat_id,
                f"No actionable signals found.\n"
                f"Store: {store_count} signals checked\n"
                f"Open pairs: {', '.join(f'{s} {d}' for s, d in pair_wallet_count.keys()) or 'none'}\n"
                f"Scalp wallets: {scalp_wallet_count}\n"
                f"Check bot logs for /signals skip reasons."
            )
            return

        lines = ["**Recent Signals** _(not already open)_\n"]
        for i, (raw, sig, source, ts) in enumerate(actionable, 1):
            ts_str = datetime.fromtimestamp(ts).strftime("%m/%d %H:%M")
            entry = sig.entry_mid
            entry_str = f"${entry:,.0f}" if entry > 0 else "market"
            n_tps = len(sig.take_profits)
            line = (
                f"**{i}.** {sig.symbol} {sig.side} {sig.leverage:.0f}x [{sig.trade_type}]\n"
                f"   Entry: {entry_str} | SL: ${sig.stop_loss:,.0f} | {n_tps} TPs\n"
                f"   _{ts_str} — {source}_"
            )
            lines.append(line)

        lines.append("\nReply with number to open (e.g. `1`) or /cancel")
        await self.send_message(chat_id, "\n".join(lines))

        self.pending_signals[chat_id] = {
            "signals": [(raw, sig) for raw, sig, source, ts in actionable],
            "created_at": time.time(),
        }

    async def handle_signals_reply(self, chat_id: int, text: str):
        """Handle user's reply to /signals prompt."""
        pending = self.pending_signals.get(chat_id)
        if not pending:
            return

        if time.time() - pending["created_at"] > 120:
            del self.pending_signals[chat_id]
            await self.send_message(chat_id, "Signal selection expired (2min). Use /signals again.")
            return

        text = text.strip().upper()
        if text in ("CANCEL", "NO", "N"):
            del self.pending_signals[chat_id]
            await self.send_message(chat_id, "Signal selection cancelled.")
            return

        # Parse number
        try:
            idx = int(text.strip()) - 1
        except ValueError:
            await self.send_message(chat_id, "Reply with a number (e.g. `1`) or /cancel")
            return

        signals = pending["signals"]
        if idx < 0 or idx >= len(signals):
            await self.send_message(chat_id, f"Invalid choice. Pick 1-{len(signals)} or /cancel")
            return

        raw_text, sig = signals[idx]
        del self.pending_signals[chat_id]

        await self.send_message(
            chat_id,
            f"Opening {sig.symbol} {sig.side} {sig.leverage:.0f}x...\n"
            f"Running through execution pipeline."
        )
        await self.process_signal(raw_text)

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
            new_pct = new_val / 100.0 if new_val >= 1.0 else new_val

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
        all_stored = await self._fetch_and_store_trades()
        today_trades = [t for t in all_stored if t.get("timestamp", 0) >= today_cutoff]

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
                    # Use on-chain unrealized PnL directly. After partial TP
                    # fills the position size is reduced on-chain, so this
                    # already reflects the remaining position. Realized PnL
                    # from TP fills shows up in the "Closed" section.
                    total_pos = cp.unrealized_pnl
                    t_sign = "+" if total_pos >= 0 else ""
                    pnl_pct_h = cp.pnl_percentage
                    pnl_pct_sign = "+" if pnl_pct_h >= 0 else ""
                    open_lines.append(
                        f"  {sym} {side} [W{wid}]: {t_sign}${total_pos:,.2f} ({pnl_pct_sign}{pnl_pct_h:.1f}%)"
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
        all_trades = await self._fetch_and_store_trades()

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

        # ── Lifetime stats ──
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
            open_pct_str = f" ({open_pnl / total_deployed * 100:+.1f}%)" if total_deployed > 0 else ""
            msg += f"\n**Open Positions ({open_count})**\n  Unrealized: {o_sign}${open_pnl:,.2f}{open_pct_str}\n"

        msg += "\n**Account Balance**\n" + "\n".join(balance_lines) + "\n"
        msg += f"  Total USDC: ${total_usdc:,.2f}\n  Deployed: ${total_deployed:,.2f}"

        await self.notify(msg)
        self.logger.info(f"Daily summary sent: daily PnL={d_sign}${daily_pnl:,.2f}")
