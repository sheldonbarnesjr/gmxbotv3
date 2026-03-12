"""
Shared cache layer between rest_api.py (writer) and gmx.py bot (reader).

Both processes run as separate systemd services on the same VPS.
They communicate via JSON files in json/ using atomic writes (same
pattern as position_state.json, trade_history.json, etc.).

Cache files:
  - json/live_positions.json  (written every 5s by rest_api.py)
  - json/live_prices.json     (written every 10s by rest_api.py)
  - json/live_balances.json   (written every 30s by rest_api.py)

All readers return None if the cache file is missing, corrupt, or stale.
Callers MUST always fall back to direct RPC/API calls when None is returned.
"""

import time
import logging
from typing import Any, Dict, Optional

from state_io import atomic_json_write, safe_json_read

logger = logging.getLogger("GMXBot.shared_cache")

POSITIONS_CACHE_FILE = "json/live_positions.json"
PRICES_CACHE_FILE = "json/live_prices.json"
BALANCES_CACHE_FILE = "json/live_balances.json"


# ── Writers (called by rest_api.py) ──────────────────────────────────────────

def write_positions_cache(positions_by_wallet: Dict[str, Any], timestamp: float = None) -> None:
    """Write position data keyed by wallet_id.

    positions_by_wallet: {
        "1": [serialized position dicts for wallet 1],
        "2": [...],
        "bitunix": [serialized Bitunix position dicts],
    }
    """
    data = {
        "ts": timestamp or time.time(),
        "positions": positions_by_wallet,
    }
    try:
        atomic_json_write(POSITIONS_CACHE_FILE, data)
    except Exception as e:
        logger.warning(f"Failed to write positions cache: {e}")


def write_prices_cache(prices: Dict[str, float], timestamp: float = None) -> None:
    """Write Chainlink prices: {"BTC": 95000.0, "ETH": 3500.0, ...}"""
    data = {
        "ts": timestamp or time.time(),
        "prices": prices,
    }
    try:
        atomic_json_write(PRICES_CACHE_FILE, data)
    except Exception as e:
        logger.warning(f"Failed to write prices cache: {e}")


def write_balances_cache(balances: Dict[str, Any], timestamp: float = None) -> None:
    """Write balance data: {"wallets": {"1": 1234.56, "2": 789.0}, "bitunix": 500.0}"""
    data = {
        "ts": timestamp or time.time(),
        "balances": balances,
    }
    try:
        atomic_json_write(BALANCES_CACHE_FILE, data)
    except Exception as e:
        logger.warning(f"Failed to write balances cache: {e}")


# ── Readers (called by gmx.py bot) ──────────────────────────────────────────

def read_positions_cache(max_age_s: float = 10.0) -> Optional[Dict]:
    """Read cached positions. Returns None if missing, corrupt, or stale.

    When None is returned, the caller MUST fall back to direct chain fetch.
    """
    data = safe_json_read(POSITIONS_CACHE_FILE, None)
    if data is None:
        return None
    ts = data.get("ts", 0)
    age = time.time() - ts
    if age > max_age_s:
        logger.debug(f"Positions cache stale ({age:.1f}s > {max_age_s}s)")
        return None
    return data.get("positions")


def read_prices_cache(max_age_s: float = 20.0) -> Optional[Dict[str, float]]:
    """Read cached Chainlink prices. Returns None if missing, corrupt, or stale.

    When None is returned, the caller MUST fall back to direct Chainlink RPC.
    """
    data = safe_json_read(PRICES_CACHE_FILE, None)
    if data is None:
        return None
    ts = data.get("ts", 0)
    age = time.time() - ts
    if age > max_age_s:
        logger.debug(f"Prices cache stale ({age:.1f}s > {max_age_s}s)")
        return None
    return data.get("prices")


def read_balances_cache(max_age_s: float = 45.0) -> Optional[Dict]:
    """Read cached wallet balances. Returns None if missing, corrupt, or stale.

    When None is returned, the caller MUST fall back to direct ERC20 balanceOf calls.
    """
    data = safe_json_read(BALANCES_CACHE_FILE, None)
    if data is None:
        return None
    ts = data.get("ts", 0)
    age = time.time() - ts
    if age > max_age_s:
        logger.debug(f"Balances cache stale ({age:.1f}s > {max_age_s}s)")
        return None
    return data.get("balances")
