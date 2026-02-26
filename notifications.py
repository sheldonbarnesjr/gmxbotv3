"""
Notifications Mixin for GMX V2 Trading Bot.

This mixin provides Telegram notification methods that GMXBot inherits.
It handles sending messages, position updates, and startup notifications.

Host class (GMXBot) must provide:
  - self.cfg: Config object with notify_chat, network, dry_run, portfolio_pct,
              max_leverage, require_tp, require_sl, telegram_channels
  - self.client: TelegramClient instance
  - self.logger: logging.Logger instance
  - self.w3: Web3 instance
  - self._all_wallets(): method returning iterator of (wallet_id, account)
  - self._get_portfolio_value_for(acct): method returning USDC value
"""

import asyncio
import logging
from typing import Optional

from close import fetch_positions as chain_fetch_positions
import bot_api


class NotificationsMixin:
    """Mixin for Telegram notification methods."""

    _notify_chat_warned: bool = False

    async def notify(self, message: str):
        """Send notification to all configured channels.

        Sends via Telethon (notify_chat) AND Bot API (admin DM).
        Returns True if at least one channel succeeded.
        """
        sent = False

        # Telethon path (existing)
        if self.cfg.notify_chat:
            if self.client:
                try:
                    await self.client.send_message(self.cfg.notify_chat, message)
                    sent = True
                except Exception as e:
                    self.logger.error(f"Notify (Telethon) failed: {e}")
            elif not self._notify_chat_warned:
                self.logger.warning("Telegram client not initialised — Telethon notification dropped")

        # Bot API path — send to DM chats + the configured admin chat
        bot_api_chats = set(getattr(self, '_bot_api_chats', set()))
        if self.cfg.bot_admin_chat_id:
            bot_api_chats.add(int(self.cfg.bot_admin_chat_id))
        for chat_id in bot_api_chats:
            try:
                ok = await bot_api.send_admin_message(
                    self.cfg.telegram_bot_token, str(chat_id), message
                )
                if ok:
                    sent = True
            except Exception as e:
                self.logger.error(f"Notify (Bot API) to {chat_id} failed: {e}")

        if not sent and not self._notify_chat_warned:
            self.logger.warning(
                "No notification channels available — set NOTIFY_CHAT or "
                "TELEGRAM_BOT_TOKEN in .env, and DM the bot to register."
            )
            self._notify_chat_warned = True

        return sent

    async def send_message(self, chat_id: int, message: str):
        """Send a message to a specific chat.

        Routes through Bot API for chats that originated from the bot,
        falls back to Telethon for all others.
        Returns True if the message was sent, False otherwise.
        """
        bot_api_chats = getattr(self, '_bot_api_chats', set())
        if chat_id in bot_api_chats:
            return await bot_api.send_admin_message(
                self.cfg.telegram_bot_token, str(chat_id), message
            )

        # Telethon fallback
        if not self.client:
            self.logger.warning("Telegram client not initialised — message dropped")
            return False
        try:
            await self.client.send_message(chat_id, message)
            return True
        except Exception as e:
            self.logger.error(f"Send message to {chat_id} failed: {e}")
            return False

    async def notify_admin(self, message: str) -> bool:
        """Send a message to ADMIN_CHAT_ID via the Bot HTTP API.

        Returns True on success, False if unconfigured or on error.
        """
        return await bot_api.send_admin_message(
            self.cfg.telegram_bot_token, self.cfg.bot_admin_chat_id, message
        )

    async def send_admin_pdf(self, file_path: str, caption: str = "") -> bool:
        """Send a PDF document to ADMIN_CHAT_ID via the Bot HTTP API."""
        return await bot_api.send_admin_pdf(
            self.cfg.telegram_bot_token, self.cfg.bot_admin_chat_id, file_path, caption
        )

    async def notify_position_opened(self, position, order_type: str = "market", signal_tp_count: int = 0):
        """Notify about a newly opened position."""
        tp_lines = ""
        for i, tp in enumerate(position.take_profits):
            tp_lines += f"  TP{i+1}: ${tp.price:,.2f} ({tp.percentage:.0%})\n"
        order_label = "LIMIT ORDER" if order_type == "limit" else "MARKET ORDER"
        sl_str = f"${position.stop_loss:,.2f}" if position.stop_loss is not None else "None"
        wallet_label = f" [W{position.wallet_id}]" if hasattr(position, 'wallet_id') else ""
        placed_count = len(position.take_profits)
        tp_header = f"TPs on-chain: {placed_count}"
        if signal_tp_count > placed_count:
            tp_header += f" (of {signal_tp_count} from signal — {signal_tp_count - placed_count} failed)"
        msg = (
            f"**Position Opened ({order_label})**\n\n"
            f"{position.symbol} {position.side}{wallet_label}\n"
            f"Size: ${position.size_usd:,.2f} @ {position.leverage:.0f}x\n"
            f"Entry: ${position.entry_price:,.2f}\n"
            f"SL: {sl_str}\n"
            f"{tp_header}\n"
            f"{tp_lines}"
            f"TX: {position.tx_hash}"
        )
        if order_type == "limit":
            msg += "\n\nLimit order placed — waiting for price to reach entry."
        await self.notify(msg)

    async def send_startup_notification(self):
        """Send status update to admin when bot comes online."""
        cfg = self.cfg
        try:
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

            collateral_per_trade = total_usdc * cfg.portfolio_pct

            msg = (
                "🟢 **Bot Online**\n\n"
                + "\n".join(wallet_lines) + "\n"
                f"Network: {cfg.network.upper()}\n"
                f"Mode: {'DRY RUN' if cfg.dry_run else 'LIVE'}\n\n"
                f"Combined USDC: ${total_usdc:,.2f}\n"
                f"Deployed: ${total_deployed:,.2f}\n"
                f"Collateral/trade: ${collateral_per_trade:,.2f} ({cfg.portfolio_pct:.0%} of USDC)\n"
                f"Open positions: {pos_count}\n\n"
                f"Max leverage: {cfg.max_leverage:.0f}x\n"
                f"Require TP/SL: {cfg.require_tp}/{cfg.require_sl}\n"
                f"Channels: {', '.join(cfg.telegram_channels)}"
            )
            await self.notify(msg)
        except Exception as e:
            self.logger.error(f"Startup notification error: {e}")
