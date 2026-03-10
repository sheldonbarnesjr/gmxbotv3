"""
Telegram Bot API helpers for admin notifications.

Sends messages and files to ADMIN_CHAT_ID via the Bot HTTP API.
Completely independent of the Telethon user session used for
reading VIP channels and handling commands.

Uses `requests` (already available as a web3 transitive dep).
All public functions are async (wrap blocking I/O with to_thread).
"""

import asyncio
import logging

import requests

logger = logging.getLogger("GMXBot.bot_api")

API_BASE = "https://api.telegram.org/bot{token}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public async functions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def send_admin_message(token: str, chat_id: str, text: str) -> bool:
    """Send a text message to ADMIN_CHAT_ID via Bot API.

    Returns True on success, False on failure or if token/chat_id are empty.
    """
    if not token or not chat_id:
        logger.warning("Bot API: missing token or chat_id — notification dropped")
        return False
    try:
        return await asyncio.to_thread(_send_message, token, chat_id, text)
    except Exception as e:
        logger.error(f"Bot API send_admin_message failed: {e}")
        return False


async def send_admin_pdf(token: str, chat_id: str, file_path: str, caption: str = "") -> bool:
    """Send a PDF document to ADMIN_CHAT_ID via Bot API.

    Returns True on success, False on failure or if token/chat_id are empty.
    """
    if not token or not chat_id:
        return False
    try:
        return await asyncio.to_thread(_send_document, token, chat_id, file_path, caption)
    except Exception as e:
        logger.error(f"Bot API send_admin_pdf failed: {e}")
        return False


async def send_admin_photo(token: str, chat_id: str, file_path: str, caption: str = "") -> bool:
    """Send a photo to ADMIN_CHAT_ID via Bot API.

    Returns True on success, False on failure or if token/chat_id are empty.
    """
    if not token or not chat_id:
        return False
    try:
        return await asyncio.to_thread(_send_photo, token, chat_id, file_path, caption)
    except Exception as e:
        logger.error(f"Bot API send_admin_photo failed: {e}")
        return False


async def get_updates(token: str, offset: int = 0, timeout: int = 1) -> tuple:
    """Poll Bot API for new messages.

    Uses long polling with the given timeout (seconds).
    Returns (list_of_updates, new_offset).
    """
    if not token:
        return [], offset
    try:
        updates, new_offset = await asyncio.to_thread(
            _get_updates, token, offset, timeout
        )
        return updates, new_offset
    except Exception as e:
        logger.error(f"Bot API get_updates failed: {e}")
        return [], offset


async def test_bot_api(token: str, chat_id: str) -> bool:
    """Send a test message to verify Bot API connectivity.

    Returns True if the message was delivered successfully.
    """
    return await send_admin_message(token, chat_id, "Bot API test — connection OK.")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Blocking helpers (run inside asyncio.to_thread)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _send_message(token: str, chat_id: str, text: str) -> bool:
    url = f"{API_BASE.format(token=token)}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }, timeout=15)
    if resp.status_code != 200:
        logger.error(f"Bot API HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not data.get("ok"):
        # Retry without parse_mode if Markdown formatting caused the failure
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
        }, timeout=15)
        data = resp.json()
        if not data.get("ok"):
            logger.error(f"Bot API sendMessage error: {data}")
            return False
    return True


def _get_updates(token: str, offset: int, timeout: int) -> tuple:
    url = f"{API_BASE.format(token=token)}/getUpdates"
    resp = requests.get(url, params={
        "offset": offset,
        "timeout": timeout,
    }, timeout=timeout + 10)
    if resp.status_code != 200:
        logger.error(f"Bot API getUpdates HTTP {resp.status_code}: {resp.text[:200]}")
        return [], offset
    data = resp.json()
    if not data.get("ok"):
        logger.error(f"Bot API getUpdates error: {data}")
        return [], offset
    updates = data.get("result", [])
    new_offset = offset
    if updates:
        new_offset = updates[-1]["update_id"] + 1
    return updates, new_offset


def _send_photo(token: str, chat_id: str, file_path: str, caption: str) -> bool:
    url = f"{API_BASE.format(token=token)}/sendPhoto"
    with open(file_path, "rb") as f:
        files = {"photo": f}
        payload = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "Markdown"
        resp = requests.post(url, data=payload, files=files, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        logger.error(f"Bot API sendPhoto error: {data}")
        return False
    return True


def _send_document(token: str, chat_id: str, file_path: str, caption: str) -> bool:
    url = f"{API_BASE.format(token=token)}/sendDocument"
    with open(file_path, "rb") as f:
        files = {"document": f}
        payload = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "Markdown"
        resp = requests.post(url, data=payload, files=files, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        logger.error(f"Bot API sendDocument error: {data}")
        return False
    return True
