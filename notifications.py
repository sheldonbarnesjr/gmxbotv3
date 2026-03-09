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
            try:
                bot_api_chats.add(int(self.cfg.bot_admin_chat_id))
            except (ValueError, TypeError):
                self.logger.warning(f"Invalid bot_admin_chat_id: {self.cfg.bot_admin_chat_id!r}")
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

    async def notify_position_opened(self, position, order_type: str = "market"):
        """Notify about a newly opened position."""
        tp_count = len(position.take_profits)
        sl_placed = 1 if position.stop_loss is not None else 0
        total_orders = tp_count + sl_placed
        collateral = position.size_usd / position.leverage if position.leverage else position.size_usd
        msg = (
            f"Position Opened (GMX) ✅\n\n"
            f"{position.symbol} {position.side} {position.leverage:.0f}x\n"
            f"Entry: ${position.entry_price:,.2f}\n"
            f"Size: ${position.size_usd:,.2f} (${collateral:,.2f} collateral)\n"
            f"{total_orders} open orders placed successfully ✅\n"
            f"TX: {position.tx_hash}"
        )
        if order_type == "limit":
            msg += "\n\nLimit order placed — waiting for price to reach entry."
        await self.notify(msg)

    async def send_startup_notification(self):
        """Send status update to admin when bot comes online."""
        await self.notify("🟢 Bot Online")
