"""
App Notification Store for GMX Trading Bot.

Bridges bot notifications to the REST API / iOS app.
The bot writes notifications here; the REST API reads and broadcasts them
to connected WebSocket clients.

Notifications are stored in json/app_notifications.json with a monotonic
sequence number so the REST API can detect new entries efficiently.
"""

import time
import logging
from typing import Optional

from state_io import safe_json_read, atomic_json_write

logger = logging.getLogger("GMXBot.app_notifications")

NOTIFICATIONS_FILE = "json/app_notifications.json"
MAX_STORED = 200  # keep last 200 notifications on disk


# ── Notification categories ──────────────────────────────────────────────────

def _classify(message: str) -> Optional[dict]:
    """Classify a notification message and return (category, priority) or None to skip.

    Returns None for excluded notification types:
      - Hourly PnL updates
      - PnL threshold alerts
      - Gas/balance warnings
      - Order retry failures
    """
    msg = message.strip()
    lower = msg.lower()

    # ── EXCLUDED types ──
    if lower.startswith("pnl update"):
        return None
    if lower.startswith("pnl alert"):
        return None
    if "gas balance" in lower or "gas top" in lower or "eth for gas" in lower:
        return None
    if "retry succeeded" in lower and ("tp @" in lower or "sl @" in lower):
        return None
    if "permanently failed after" in lower and ("tp @" in lower or "sl @" in lower):
        return None

    # ── INCLUDED types — classify ──

    # Position opened
    if "position opened" in lower:
        exchange = "bitunix" if "bitunix" in lower else "gmx"
        return {"category": "position_opened", "exchange": exchange, "priority": "critical"}

    # Position closed
    if "position closed" in lower:
        exchange = "bitunix" if "bitunix" in lower else "gmx"
        return {"category": "position_closed", "exchange": exchange, "priority": "critical"}

    # TP hit
    if "target" in lower and "hit" in lower and "✅" in msg:
        exchange = "bitunix" if "bitunix" in lower else "gmx"
        return {"category": "tp_hit", "exchange": exchange, "priority": "critical"}

    # SL moved
    if "sl moved" in lower:
        exchange = "bitunix" if "bitunix" in lower else "gmx"
        return {"category": "sl_moved", "exchange": exchange, "priority": "high"}

    # SL move failed
    if ("sl" in lower or "stop loss" in lower) and ("failed" in lower or "unprotected" in lower):
        return {"category": "sl_move_failed", "priority": "critical"}

    # TP hit but SL move failed
    if "tp hit but failed to move sl" in lower or "tp hit — sl move failed" in lower:
        return {"category": "tp_sl_move_failed", "priority": "critical"}

    # Trading halted
    if lower.startswith("trading halted"):
        return {"category": "trading_halted", "priority": "critical"}

    # Trading resumed
    if lower.startswith("trading resumed"):
        return {"category": "trading_resumed", "priority": "high"}

    # Bot online / offline
    if "bot online" in lower:
        return {"category": "bot_online", "priority": "high"}
    if "bot offline" in lower:
        return {"category": "bot_offline", "priority": "critical"}

    # Signal rejected
    if lower.startswith("rejected "):
        return {"category": "signal_rejected", "priority": "medium"}

    # Duplicate blocked
    if "blocked duplicate" in lower:
        return {"category": "duplicate_blocked", "priority": "medium"}

    # Signal executing
    if lower.startswith("executing "):
        return {"category": "signal_executing", "priority": "high"}

    # Error processing signal
    if "error processing signal" in lower:
        return {"category": "signal_error", "priority": "high"}

    # Channel confirmed TP/SL
    if "channel confirmed" in lower:
        return {"category": "channel_confirmed", "priority": "medium"}

    # Mirror mode alerts
    if "[mirror]" in lower:
        if "failed" in lower or "error" in lower:
            return {"category": "mirror_error", "priority": "high"}
        if "auto-closed" in lower:
            return {"category": "mirror_close", "priority": "high"}
        return {"category": "mirror_info", "priority": "medium"}

    # Bitunix errors
    if "[bitunix]" in lower and ("failed" in lower or "error" in lower):
        return {"category": "bitunix_error", "priority": "high"}

    # Startup SL fix
    if "startup sl fix" in lower:
        if "failed" in lower:
            return {"category": "startup_sl_failed", "priority": "high"}
        return {"category": "startup_sl_fix", "priority": "medium"}

    # Startup cleanup
    if "startup cleanup" in lower:
        return {"category": "startup_cleanup", "priority": "medium"}

    # Missing SL warning
    if "has no sl on-chain" in lower or "missing sl" in lower:
        return {"category": "sl_missing", "priority": "critical"}

    # Weekly summary
    if "weekly summary" in lower or "lifetime stats" in lower:
        return {"category": "weekly_summary", "priority": "medium"}

    # Position override
    if "overriding" in lower and "closing for new" in lower:
        return {"category": "position_override", "priority": "high"}

    # Catch-all: include anything not explicitly excluded
    return {"category": "general", "priority": "medium"}


# ── Store operations ─────────────────────────────────────────────────────────

def push_notification(message: str) -> Optional[dict]:
    """Classify and store a notification. Returns the notification dict or None if excluded."""
    meta = _classify(message)
    if meta is None:
        return None

    store = safe_json_read(NOTIFICATIONS_FILE, {"seq": 0, "notifications": []})
    seq = store.get("seq", 0) + 1

    notification = {
        "id": seq,
        "timestamp": time.time(),
        "message": message.strip(),
        "category": meta["category"],
        "priority": meta["priority"],
    }
    if "exchange" in meta:
        notification["exchange"] = meta["exchange"]

    notifications = store.get("notifications", [])
    notifications.append(notification)

    # Trim to last MAX_STORED
    if len(notifications) > MAX_STORED:
        notifications = notifications[-MAX_STORED:]

    try:
        atomic_json_write(NOTIFICATIONS_FILE, {"seq": seq, "notifications": notifications})
    except Exception as e:
        logger.warning(f"Failed to write app notification: {e}")
        return None

    return notification


def get_notifications(since_seq: int = 0, limit: int = 50) -> dict:
    """Read notifications from the store.

    Args:
        since_seq: Return only notifications with id > since_seq (for polling new ones).
        limit: Max number to return (most recent first).

    Returns:
        {"seq": current_seq, "notifications": [...]}
    """
    store = safe_json_read(NOTIFICATIONS_FILE, {"seq": 0, "notifications": []})
    current_seq = store.get("seq", 0)
    notifications = store.get("notifications", [])

    if since_seq > 0:
        notifications = [n for n in notifications if n.get("id", 0) > since_seq]

    # Return most recent first, limited
    notifications = notifications[-limit:]
    notifications.reverse()

    return {"seq": current_seq, "notifications": notifications}


def get_current_seq() -> int:
    """Get the current sequence number without loading all notifications."""
    store = safe_json_read(NOTIFICATIONS_FILE, {"seq": 0, "notifications": []})
    return store.get("seq", 0)
