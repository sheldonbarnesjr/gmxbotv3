"""
REST API Server for GMX Trading Bot — serves the iOS Multiply app.

Reads bot state from JSON files and on-chain data to expose endpoints
that the iOS app's APIClient.swift expects.

Run standalone:  python rest_api.py
Or with uvicorn: uvicorn rest_api:app --host 0.0.0.0 --port 8000

Requires a running bot instance OR at minimum:
  - .env with config (RPC, keys, etc.)
  - json/ directory with state files
"""

import os
import sys
import json
import time
import hmac
import hashlib
import logging
import asyncio
import secrets
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Depends, Header, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Bot imports ──
from config import load_config, ALLOWED_SYMBOLS, CHAINLINK_FEEDS, CHAINLINK_ABI
from state_io import safe_json_read, atomic_json_write

logger = logging.getLogger("GMXBot.rest_api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# State file paths (same as the bot uses)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
POSITIONS_FILE = "json/positions.json"
TRADE_HISTORY_FILE = "json/trade_history.json"
BALANCE_SNAPSHOTS_FILE = "json/balance_snapshots.json"
SIGNAL_STORE_FILE = "json/signal_store.json"
API_KEYS_FILE = "json/api_keys.json"
CHART_CONFIG_FILE = "json/chart_config.json"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Globals initialized at startup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
cfg = None
w3 = None
accounts = {}  # {wallet_id: Account}
bx_client = None  # BitunixClient (if configured)


def _api_save_balance_snapshot(total: float):
    """Save a balance snapshot from the REST API (standalone, no bot instance)."""
    snapshots = safe_json_read(BALANCE_SNAPSHOTS_FILE, [])
    snapshots.append({"timestamp": time.time(), "total_portfolio": round(total, 2)})
    cutoff = time.time() - (90 * 24 * 3600)
    # Also respect reset_timestamp — don't keep snapshots from before reset
    chart_cfg = safe_json_read(CHART_CONFIG_FILE, {})
    reset_ts = chart_cfg.get("reset_timestamp", 0)
    effective_cutoff = max(cutoff, reset_ts)
    snapshots = [s for s in snapshots if s["timestamp"] >= effective_cutoff]
    try:
        atomic_json_write(BALANCE_SNAPSHOTS_FILE, snapshots)
    except Exception:
        pass


def _load_reset_timestamp() -> float:
    """Load the reset timestamp from chart config (0 if never reset)."""
    chart_cfg = safe_json_read(CHART_CONFIG_FILE, {})
    return chart_cfg.get("reset_timestamp", 0)


start_time = time.time()


def _init_web3_and_accounts():
    """Initialize Web3 connection and wallet accounts."""
    global cfg, w3, accounts, bx_client

    cfg = load_config()

    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(cfg.rpc_url))
    if not w3.is_connected():
        logger.warning(f"Web3 not connected to {cfg.rpc_url}")

    from eth_account import Account
    if cfg.private_key:
        accounts[1] = Account.from_key(cfg.private_key)
    if cfg.private_key_2:
        accounts[2] = Account.from_key(cfg.private_key_2)
    if cfg.private_key_3:
        accounts[3] = Account.from_key(cfg.private_key_3)
    if cfg.private_key_4:
        accounts[4] = Account.from_key(cfg.private_key_4)

    # Initialize Bitunix client if configured
    if cfg.bitunix_api_key and cfg.bitunix_secret_key:
        try:
            from bitunix_api import BitunixClient
            bx_client = BitunixClient(cfg.bitunix_api_key, cfg.bitunix_secret_key)
            logger.info("Bitunix client initialized")
        except Exception as e:
            logger.warning(f"Failed to init Bitunix client: {e}")

    logger.info(f"Initialized {len(accounts)} wallet(s), Web3 connected: {w3.is_connected()}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# API Key auth
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _load_api_keys() -> list:
    """Load valid API keys from disk."""
    return safe_json_read(API_KEYS_FILE, [])


def _save_api_keys(keys: list):
    atomic_json_write(API_KEYS_FILE, keys)


def generate_api_key() -> str:
    """Generate a new API key, save it, and return it."""
    key = f"gmx_{secrets.token_urlsafe(32)}"
    keys = _load_api_keys()
    keys.append({"key": key, "created_at": time.time()})
    _save_api_keys(keys)
    logger.info(f"Generated new API key: {key[:12]}...")
    return key


async def verify_api_key(authorization: Optional[str] = Header(None)):
    """FastAPI dependency to verify Bearer token."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid Authorization format")

    token = authorization[7:]
    keys = _load_api_keys()
    valid_keys = [k["key"] for k in keys]

    if token not in valid_keys:
        raise HTTPException(status_code=403, detail="Invalid API key")

    return token


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helper: read on-chain data
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

# Uniswap V3 SwapRouter on Arbitrum
UNISWAP_V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
USDT_ADDRESS = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"  # Arbitrum USDT
USDC_ADDRESS = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"  # Arbitrum native USDC

# Uniswap V3 SwapRouter ABI (exactInputSingle only)
UNISWAP_V3_ABI = [
    {
        "name": "exactInputSingle",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{
            "name": "params",
            "type": "tuple",
            "components": [
                {"name": "tokenIn", "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "fee", "type": "uint24"},
                {"name": "recipient", "type": "address"},
                {"name": "deadline", "type": "uint256"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMinimum", "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
        }],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
]


def _get_usdc_balance(account) -> float:
    """Get USDC balance for an account."""
    try:
        from web3 import Web3
        token = w3.eth.contract(
            address=Web3.to_checksum_address(cfg.collateral_token),
            abi=ERC20_ABI,
        )
        decimals = token.functions.decimals().call()
        balance_raw = token.functions.balanceOf(account.address).call()
        return balance_raw / (10 ** decimals)
    except Exception as e:
        logger.error(f"Error getting USDC balance for {account.address[:10]}: {e}")
        return 0.0


def _get_eth_balance(account) -> float:
    """Get ETH balance in USD-equivalent (just raw ETH for now)."""
    try:
        balance_wei = w3.eth.get_balance(account.address)
        return float(w3.from_wei(balance_wei, "ether"))
    except Exception:
        return 0.0


def _get_chainlink_price(symbol: str) -> Optional[float]:
    """Fetch price from Chainlink oracle on Arbitrum."""
    feed_addr = CHAINLINK_FEEDS.get(symbol)
    if not feed_addr:
        return None
    try:
        from web3 import Web3
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(feed_addr),
            abi=CHAINLINK_ABI,
        )
        decimals = contract.functions.decimals().call()
        _, answer, _, updated_at, _ = contract.functions.latestRoundData().call()
        price = answer / (10 ** decimals)
        return price
    except Exception as e:
        logger.error(f"Chainlink price fetch failed for {symbol}: {e}")
        return None


async def _fetch_chain_positions(account) -> list:
    """Fetch on-chain positions for an account."""
    try:
        from close import fetch_positions as chain_fetch_positions
        positions = await asyncio.to_thread(chain_fetch_positions, w3, account.address)
        return positions
    except Exception as e:
        logger.error(f"Failed to fetch chain positions: {e}")
        return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pydantic response models (matching iOS APIClient.swift)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FastAPI app
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _calculate_total_portfolio() -> float:
    """Calculate total portfolio value from wallets + positions + Bitunix."""
    total = 0.0
    for wid, acct in accounts.items():
        total += await asyncio.to_thread(_get_usdc_balance, acct)

    positions = safe_json_read(POSITIONS_FILE, {})
    for pid, p in positions.items():
        if p.get("is_open", False):
            size = p.get("size_usd", 0)
            lev = p.get("leverage", 1)
            total += size / lev if lev > 0 else size
            symbol = p.get("symbol", "")
            entry = p.get("entry_price", 0)
            side = p.get("side", "LONG")
            price = await asyncio.to_thread(_get_chainlink_price, symbol)
            if price and entry > 0 and size > 0:
                if side == "LONG":
                    total += (price - entry) / entry * size
                else:
                    total += (entry - price) / entry * size
            else:
                total += p.get("unrealized_pnl", 0)

    if bx_client:
        try:
            from bitunix_executor import get_bitunix_balance, get_bitunix_positions
            bx_bal = await asyncio.to_thread(get_bitunix_balance, bx_client)
            bx_positions = await asyncio.to_thread(get_bitunix_positions, bx_client)
            total += bx_bal
            for bp in bx_positions:
                total += float(bp.get("margin", 0))
                total += float(bp.get("unrealizedPNL", 0))
        except Exception as e:
            logger.warning(f"Bitunix fetch in periodic snapshot failed: {e}")

    return total


async def _periodic_snapshot_task():
    """Background task: save a balance snapshot every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        try:
            total = await _calculate_total_portfolio()
            if total > 0:
                _api_save_balance_snapshot(total)
                logger.info(f"Periodic snapshot saved: ${total:.2f}")
        except Exception as e:
            logger.warning(f"Periodic snapshot failed: {e}")


_snapshot_bg_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _snapshot_bg_task
    _init_web3_and_accounts()
    # Generate initial API key if none exist
    keys = _load_api_keys()
    if not keys:
        key = generate_api_key()
        print(f"\n{'='*60}")
        print(f"  YOUR API KEY (save this for the iOS app):")
        print(f"  {key}")
        print(f"{'='*60}\n")
    # Backfill balance snapshots from trade history if sparse
    _backfill_snapshots_if_needed()
    # Start background snapshot task
    _snapshot_bg_task = asyncio.create_task(_periodic_snapshot_task())
    yield
    # Cleanup
    if _snapshot_bg_task:
        _snapshot_bg_task.cancel()
        try:
            await _snapshot_bg_task
        except asyncio.CancelledError:
            pass


def _backfill_snapshots_if_needed():
    """Reconstruct balance history from trades if snapshots are sparse."""
    snapshots = safe_json_read(BALANCE_SNAPSHOTS_FILE, [])
    if len(snapshots) >= 20:
        return  # enough data already

    reset_ts = _load_reset_timestamp()

    trades = safe_json_read(TRADE_HISTORY_FILE, [])
    # Filter out trades before reset
    trades = [t for t in trades if t.get("closed_at", 0) >= reset_ts]
    if not trades:
        return

    trades.sort(key=lambda t: t.get("closed_at", 0))

    # Estimate current total from wallets
    current_total = 0.0
    try:
        for wid, acct in accounts.items():
            current_total += _get_usdc_balance(acct)
    except Exception:
        return

    positions = safe_json_read(POSITIONS_FILE, {})
    for pid, p in positions.items():
        if p.get("is_open", False):
            lev = p.get("leverage", 1)
            size = p.get("size_usd", 0)
            current_total += size / lev if lev > 0 else size
            current_total += p.get("unrealized_pnl", 0)

    existing_ts = {int(s["timestamp"]) for s in snapshots}
    new_points = []
    running = current_total
    new_points.append({"timestamp": time.time(), "total_portfolio": round(running, 2)})

    for t in reversed(trades):
        closed_at = t.get("closed_at", 0)
        if closed_at <= 0:
            continue
        pnl = t.get("pnl_usd", 0)
        running -= pnl
        if any(abs(closed_at - ts) < 60 for ts in existing_ts):
            continue
        new_points.append({"timestamp": closed_at, "total_portfolio": round(running, 2)})

    all_snapshots = snapshots + new_points
    all_snapshots.sort(key=lambda s: s["timestamp"])
    cutoff = time.time() - (90 * 24 * 3600)
    all_snapshots = [s for s in all_snapshots if s["timestamp"] >= cutoff]

    try:
        atomic_json_write(BALANCE_SNAPSHOTS_FILE, all_snapshots)
        logger.info(f"Backfilled {len(new_points)} snapshot points from trade history")
    except Exception as e:
        logger.warning(f"Failed to backfill snapshots: {e}")


app = FastAPI(
    title="GMX Trading Bot API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HEALTH
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/v1/health")
async def health(token: str = Depends(verify_api_key)):
    """Health check — matches iOS HealthResponse."""
    positions = safe_json_read(POSITIONS_FILE, {})
    trades = safe_json_read(TRADE_HISTORY_FILE, [])
    open_count = sum(1 for p in positions.values() if p.get("is_open", False)) if isinstance(positions, dict) else 0

    return {
        "status": "online",
        "uptime_seconds": time.time() - start_time,
        "signals_processed": len(safe_json_read(SIGNAL_STORE_FILE, [])),
        "trades_executed": len(trades),
        "open_positions": open_count,
        "exchange_mode": cfg.exchange_mode if cfg else "unknown",
        "is_halted": False,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DASHBOARD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/v1/dashboard")
async def dashboard(token: str = Depends(verify_api_key)):
    """Dashboard — matches iOS DashboardResponse."""
    # Calculate free USDC across all GMX wallets
    free_usdc_gmx = 0.0
    for wid, acct in accounts.items():
        free_usdc_gmx += await asyncio.to_thread(_get_usdc_balance, acct)

    # Calculate deployed collateral + unrealized PnL from positions.json
    # (more reliable than on-chain fetch which can fail silently)
    deployed_collateral = 0.0
    unrealized_pnl = 0.0
    positions = safe_json_read(POSITIONS_FILE, {})
    for pid, p in positions.items():
        if p.get("is_open", False):
            size = p.get("size_usd", 0)
            lev = p.get("leverage", 1)
            collateral = size / lev if lev > 0 else size
            deployed_collateral += collateral

            # Get live price for PnL calculation
            symbol = p.get("symbol", "")
            entry = p.get("entry_price", 0)
            side = p.get("side", "LONG")
            price = await asyncio.to_thread(_get_chainlink_price, symbol)
            if price and entry > 0 and size > 0:
                if side == "LONG":
                    unrealized_pnl += (price - entry) / entry * size
                else:
                    unrealized_pnl += (entry - price) / entry * size
            else:
                # Fallback to stored PnL
                unrealized_pnl += p.get("unrealized_pnl", 0)

    # Include Bitunix balance + positions
    free_usdc_bitunix = 0.0
    if bx_client:
        try:
            from bitunix_executor import get_bitunix_balance, get_bitunix_positions
            bx_bal = await asyncio.to_thread(get_bitunix_balance, bx_client)
            bx_positions = await asyncio.to_thread(get_bitunix_positions, bx_client)
            free_usdc_bitunix = bx_bal
            for bp in bx_positions:
                bx_margin = float(bp.get("margin", 0))
                bx_pnl = float(bp.get("unrealizedPNL", 0))
                deployed_collateral += bx_margin
                unrealized_pnl += bx_pnl
        except Exception as e:
            logger.warning(f"Bitunix balance fetch failed: {e}")

    free_usdc = free_usdc_gmx + free_usdc_bitunix
    total_portfolio = free_usdc + deployed_collateral + unrealized_pnl

    # Auto-save balance snapshot every 5 minutes
    snapshots_all = safe_json_read(BALANCE_SNAPSHOTS_FILE, [])
    last_snap_ts = snapshots_all[-1]["timestamp"] if snapshots_all else 0
    time_elapsed = time.time() - last_snap_ts
    if total_portfolio > 0 and time_elapsed >= 300:
        _api_save_balance_snapshot(total_portfolio)

    # 24h change from balance snapshots
    snapshots = safe_json_read(BALANCE_SNAPSHOTS_FILE, [])
    change_24h_usd = 0.0
    change_24h_pct = 0.0
    has_24h_data = False

    if snapshots:
        now = time.time()
        oldest_snap = snapshots[0]["timestamp"]
        # Only show 24h change if we have at least 20h of snapshot history
        if now - oldest_snap >= 20 * 3600:
            target_ts = now - 86400
            closest = min(snapshots, key=lambda s: abs(s["timestamp"] - target_ts))
            if abs(closest["timestamp"] - target_ts) < 3 * 3600:  # 3h tolerance
                old_total = closest["total_portfolio"]
                if old_total > 0:
                    change_24h_usd = total_portfolio - old_total
                    change_24h_pct = (change_24h_usd / old_total) * 100
                    has_24h_data = True

    return {
        "total_portfolio": round(total_portfolio, 2),
        "free_usdc": round(free_usdc, 2),
        "free_usdc_gmx": round(free_usdc_gmx, 2),
        "free_usdc_bitunix": round(free_usdc_bitunix, 2),
        "deployed_collateral": round(deployed_collateral, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "change_24h_usd": round(change_24h_usd, 2),
        "change_24h_pct": round(change_24h_pct, 2),
        "has_24h_data": has_24h_data,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RESET
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.post("/api/v1/dashboard/reset")
async def dashboard_reset(token: str = Depends(verify_api_key)):
    """Reset all chart history. Archives old data and starts fresh from now."""
    import shutil
    now = time.time()

    # Archive existing files
    for src in [BALANCE_SNAPSHOTS_FILE, TRADE_HISTORY_FILE]:
        if os.path.exists(src):
            shutil.copy2(src, f"{src}.bak")

    # Calculate current portfolio for the initial snapshot
    try:
        initial_balance = await _calculate_total_portfolio()
    except Exception:
        initial_balance = 0.0

    # Write fresh snapshots with single current point
    if initial_balance > 0:
        atomic_json_write(BALANCE_SNAPSHOTS_FILE, [
            {"timestamp": now, "total_portfolio": round(initial_balance, 2)}
        ])
    else:
        atomic_json_write(BALANCE_SNAPSHOTS_FILE, [])

    # Clear trade history
    atomic_json_write(TRADE_HISTORY_FILE, [])

    # Save reset timestamp
    atomic_json_write(CHART_CONFIG_FILE, {"reset_timestamp": now})

    logger.info(f"Chart history reset at {now}, initial balance: ${initial_balance:.2f}")

    return {
        "success": True,
        "reset_at": now,
        "initial_balance": round(initial_balance, 2),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHART
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/v1/dashboard/chart")
async def dashboard_chart(
    period: str = Query("24h"),
    mode: str = Query("balance"),
    token: str = Depends(verify_api_key),
):
    """Portfolio chart data from balance snapshots + trade history."""
    snapshots = safe_json_read(BALANCE_SNAPSHOTS_FILE, [])

    # Apply reset_timestamp — never show data from before a reset
    reset_ts = _load_reset_timestamp()

    # Filter by period
    # Calculate YTD hours
    import datetime
    now = datetime.datetime.now()
    jan1 = datetime.datetime(now.year, 1, 1)
    ytd_hours = (now - jan1).total_seconds() / 3600

    period_map = {
        "1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720,
        "90d": 2160, "ytd": ytd_hours, "365d": 8760, "all": 999999,
    }
    period_hours = period_map.get(period, 24)
    cutoff = max(time.time() - (period_hours * 3600), reset_ts)
    filtered = [s for s in snapshots if s["timestamp"] >= cutoff]

    points = [{"timestamp": s["timestamp"], "value": s["total_portfolio"]} for s in filtered]

    # If we don't have enough snapshot data, build from trade history
    if len(points) < 10:
        trades = safe_json_read(TRADE_HISTORY_FILE, [])
        trades.sort(key=lambda t: t.get("closed_at", 0))

        # Get current portfolio value as the end point
        current_total = 0.0
        for wid, acct in accounts.items():
            current_total += await asyncio.to_thread(_get_usdc_balance, acct)
        positions = safe_json_read(POSITIONS_FILE, {})
        for pid, p in positions.items():
            if p.get("is_open", False):
                size = p.get("size_usd", 0)
                lev = p.get("leverage", 1)
                current_total += size / lev if lev > 0 else size
                current_total += p.get("unrealized_pnl", 0)

        # Walk backwards from current total using trade PnL
        trade_points = []
        running_total = current_total
        trade_points.append({"timestamp": time.time(), "value": round(running_total, 2)})

        for t in reversed(trades):
            closed_at = t.get("closed_at", 0)
            if closed_at < cutoff:
                break
            pnl = t.get("pnl_usd", 0)
            running_total -= pnl  # subtract PnL to get value before this trade
            trade_points.append({"timestamp": closed_at, "value": round(running_total, 2)})

        trade_points.reverse()
        # Merge: use trade-derived points where snapshots are missing
        if trade_points:
            points = trade_points

    # PnL chart mode: cumulative realized PnL + current unrealized PnL
    if mode == "pnl":
        trades = safe_json_read(TRADE_HISTORY_FILE, [])
        trades.sort(key=lambda t: t.get("closed_at", 0))
        pnl_points = []
        cumulative = 0.0
        for t in trades:
            closed_at = t.get("closed_at", 0)
            if closed_at <= 0 or closed_at < cutoff:
                # FIXED: Do NOT accumulate PnL from trades before the cutoff.
                # Previously this silently inflated the starting value, causing
                # different timeframes to show wildly different PnL totals.
                continue
            cumulative += t.get("pnl_usd", 0)
            pnl_points.append({"timestamp": closed_at, "value": round(cumulative, 2)})

        # Add current point including unrealized PnL from open positions
        current_unrealized = 0.0
        pos_data = safe_json_read(POSITIONS_FILE, {})
        for pid, p in pos_data.items():
            if p.get("is_open", False):
                symbol = p.get("symbol", "")
                entry = p.get("entry_price", 0)
                side = p.get("side", "LONG")
                size = p.get("size_usd", 0)
                price = await asyncio.to_thread(_get_chainlink_price, symbol)
                if price and entry > 0 and size > 0:
                    if side == "LONG":
                        current_unrealized += (price - entry) / entry * size
                    else:
                        current_unrealized += (entry - price) / entry * size
                else:
                    current_unrealized += p.get("unrealized_pnl", 0)

        # Always add a "now" point so chart extends to current time
        now_value = round(cumulative + current_unrealized, 2)
        pnl_points.append({"timestamp": time.time(), "value": now_value})

        if len(pnl_points) == 1:
            # Need at least 2 points — add a zero-start point at cutoff
            pnl_points.insert(0, {"timestamp": cutoff, "value": 0.0})

        return {
            "period": period,
            "points": pnl_points,
            "point_count": len(pnl_points),
            "oldest_point_ts": pnl_points[0]["timestamp"] if pnl_points else None,
        }

    # Compute live portfolio value for the "now" endpoint
    live_total = 0.0
    for wid, acct in accounts.items():
        live_total += await asyncio.to_thread(_get_usdc_balance, acct)
    positions_data = safe_json_read(POSITIONS_FILE, {})
    for pid, p in positions_data.items():
        if p.get("is_open", False):
            lev = p.get("leverage", 1)
            size = p.get("size_usd", 0)
            live_total += size / lev if lev > 0 else size
            # Use live price for accuracy
            symbol = p.get("symbol", "")
            entry = p.get("entry_price", 0)
            side = p.get("side", "LONG")
            price = await asyncio.to_thread(_get_chainlink_price, symbol)
            if price and entry > 0 and size > 0:
                if side == "LONG":
                    live_total += (price - entry) / entry * size
                else:
                    live_total += (entry - price) / entry * size
            else:
                live_total += p.get("unrealized_pnl", 0)
    if bx_client:
        try:
            from bitunix_executor import get_bitunix_balance, get_bitunix_positions
            bx_bal = await asyncio.to_thread(get_bitunix_balance, bx_client)
            bx_positions = await asyncio.to_thread(get_bitunix_positions, bx_client)
            live_total += bx_bal
            for bp in bx_positions:
                live_total += float(bp.get("margin", 0))
                live_total += float(bp.get("unrealizedPNL", 0))
        except Exception:
            pass

    # Always append a live "now" point so chart reflects current state
    if live_total > 0:
        now_point = {"timestamp": time.time(), "value": round(live_total, 2)}
        if points:
            # Replace last point if it's within 30s (avoid duplicates)
            if time.time() - points[-1]["timestamp"] < 30:
                points[-1] = now_point
            else:
                points.append(now_point)
        else:
            points = [now_point]

    return {
        "period": period,
        "points": points,
        "point_count": len(points),
        "oldest_point_ts": points[0]["timestamp"] if points else None,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# POSITIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _format_position(pid: str, p: dict) -> dict:
    """Format a position dict for API response — matches iOS Position model."""
    tps = p.get("take_profits", [])
    entry = p.get("entry_price", 0)
    current = p.get("current_price", 0)
    size = p.get("size_usd", 0)
    leverage = p.get("leverage", 0)
    side = p.get("side", "LONG")
    upnl = p.get("unrealized_pnl", 0)
    collateral = size / leverage if leverage > 0 else size
    pnl_pct = (upnl / collateral * 100) if collateral > 0 else 0
    opened_at = p.get("opened_at", 0)
    duration_h = (time.time() - opened_at) / 3600 if opened_at > 0 else 0
    tp_hits = len(p.get("verified_decreases", []))

    return {
        "id": pid,
        "symbol": p.get("symbol", ""),
        "side": side,
        "size_usd": size,
        "leverage": leverage,
        "entry_price": entry,
        "current_price": current,
        "unrealized_pnl": round(upnl, 2),
        "realized_pnl": p.get("realized_pnl", 0),
        "pnl_percentage": round(pnl_pct, 2),
        "collateral_usd": round(collateral, 2),
        "stop_loss": p.get("stop_loss"),
        "take_profits": [
            {"price": tp.get("price", 0), "percentage": tp.get("percentage", 0)}
            for tp in tps
        ],
        "is_open": p.get("is_open", True),
        "opened_at": opened_at,
        "duration_hours": round(duration_h, 2),
        "tp_hits": tp_hits,
        "wallet_id": p.get("wallet_id", 1),
        "exchange": p.get("exchange", "gmx"),
    }


@app.get("/api/v1/positions")
async def list_positions(token: str = Depends(verify_api_key)):
    """List open positions — matches iOS PositionsListResponse."""
    positions = safe_json_read(POSITIONS_FILE, {})

    # Update current prices from Chainlink
    for pid, p in positions.items():
        if p.get("is_open"):
            symbol = p.get("symbol", "")
            price = await asyncio.to_thread(_get_chainlink_price, symbol)
            if price:
                p["current_price"] = price
                # Recalculate PnL
                side = p.get("side", "LONG")
                entry = p.get("entry_price", 0)
                size = p.get("size_usd", 0)
                if entry > 0 and size > 0:
                    if side == "LONG":
                        p["unrealized_pnl"] = (price - entry) / entry * size
                    else:
                        p["unrealized_pnl"] = (entry - price) / entry * size

    open_positions = [
        _format_position(pid, p)
        for pid, p in positions.items()
        if p.get("is_open", False)
    ]

    total_pnl = sum(p.get("unrealized_pnl", 0) for p in positions.values() if p.get("is_open"))
    total_value = sum(p.get("size_usd", 0) for p in positions.values() if p.get("is_open"))

    return {
        "positions": open_positions,
        "total_pnl": round(total_pnl, 2),
        "total_value": round(total_value, 2),
        "count": len(open_positions),
    }


@app.get("/api/v1/positions/{position_id}")
async def get_position(position_id: str, token: str = Depends(verify_api_key)):
    """Get a specific position."""
    positions = safe_json_read(POSITIONS_FILE, {})
    if position_id not in positions:
        raise HTTPException(status_code=404, detail="Position not found")

    return _format_position(position_id, positions[position_id])


@app.post("/api/v1/positions/{position_id}/close")
async def close_position(position_id: str, token: str = Depends(verify_api_key)):
    """Close a position on-chain."""
    positions = safe_json_read(POSITIONS_FILE, {})
    if position_id not in positions:
        raise HTTPException(status_code=404, detail="Position not found")

    p = positions[position_id]
    if not p.get("is_open"):
        raise HTTPException(status_code=400, detail="Position already closed")

    wid = p.get("wallet_id", 1)
    acct = accounts.get(wid)
    if not acct:
        raise HTTPException(status_code=500, detail=f"Wallet {wid} not configured")

    symbol = p.get("symbol", "")
    market_addr = cfg.markets.get(symbol, "")
    is_long = p.get("side", "LONG") == "LONG"

    try:
        from close import create_close_order
        from web3 import Web3

        tx_hash = await asyncio.to_thread(
            create_close_order,
            w3,
            acct,
            Web3.to_checksum_address(market_addr),
            Web3.to_checksum_address(cfg.collateral_token),
            is_long,
            int(p.get("size_usd", 0) * 10**30),  # size in USD with 30 decimals
            cfg.execution_fee_wei,
            Web3.to_checksum_address(cfg.order_vault),
            Web3.to_checksum_address(cfg.exchange_router),
        )

        # Save balance snapshot after close
        try:
            total = 0.0
            for wid, acct in accounts.items():
                total += await asyncio.to_thread(_get_usdc_balance, acct)
            _api_save_balance_snapshot(total)
        except Exception:
            pass

        return {
            "success": True,
            "message": f"Close order submitted for {symbol} {p.get('side', '')}",
            "tx_hash": tx_hash if isinstance(tx_hash, str) else tx_hash.hex() if tx_hash else None,
        }
    except Exception as e:
        logger.error(f"Failed to close position {position_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TRADES (history)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _format_trade(t: dict) -> dict:
    """Format a trade record for API response."""
    # Format tp_details: list of {price, pct, pnl}
    raw_tp = t.get("tp_details") or []
    tp_details = []
    for tp in raw_tp:
        if isinstance(tp, dict):
            tp_details.append({
                "price": tp.get("price", 0),
                "pct": tp.get("pct", tp.get("percentage", 0)),
                "pnl": tp.get("pnl", 0),
            })

    # Format sl_details
    raw_sl = t.get("sl_details")
    sl_details = None
    if isinstance(raw_sl, dict):
        sl_details = {
            "price": raw_sl.get("price", 0),
            "pct": raw_sl.get("pct", raw_sl.get("percentage", 0)),
            "pnl": raw_sl.get("pnl", 0),
        }

    return {
        "id": t.get("id", ""),
        "symbol": t.get("symbol", ""),
        "side": t.get("side", ""),
        "entry_price": float(t.get("entry_price", 0)),
        "exit_price": float(t.get("exit_price", 0)),
        "size_usd": float(t.get("size_usd", 0)),
        "leverage": float(t.get("leverage", 0)),
        "duration_hours": float(t.get("duration_hours", 0)),
        "pnl_usd": float(t.get("pnl_usd", 0)),
        "pnl_percentage": float(t.get("pnl_percentage", 0)),
        "exit_reason": t.get("exit_reason", ""),
        "opened_at": t.get("opened_at", 0),
        "closed_at": t.get("closed_at", 0),
        "wallet_id": t.get("wallet_id", 0),
        "exchange": t.get("exchange", "gmx"),
        "tp_hits": t.get("tp_hits", 0),
        "tp_details": tp_details if tp_details else None,
        "sl_details": sl_details,
        "unfilled_targets": t.get("unfilled_targets"),
    }


@app.get("/api/v1/trades")
async def list_trades(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    token: str = Depends(verify_api_key),
):
    """List trade history — matches iOS TradesListResponse."""
    all_trades = safe_json_read(TRADE_HISTORY_FILE, [])

    # Sort by closed_at descending (most recent first)
    all_trades.sort(key=lambda t: t.get("closed_at", 0), reverse=True)

    # Paginate
    start = (page - 1) * limit
    end = start + limit
    page_trades = all_trades[start:end]

    return {
        "trades": [_format_trade(t) for t in page_trades],
        "total": len(all_trades),
        "page": page,
        "limit": limit,
    }


@app.get("/api/v1/trades/stats")
async def trade_stats(
    symbol: Optional[str] = None,
    token: str = Depends(verify_api_key),
):
    """Trade statistics — win rate, avg PnL, etc."""
    all_trades = safe_json_read(TRADE_HISTORY_FILE, [])

    # Filter by symbol if provided
    trades = [t for t in all_trades if abs(t.get("pnl_usd", 0)) >= 1]
    if symbol:
        trades = [t for t in trades if t.get("symbol") == symbol]

    if not trades:
        return {"win_rate": 0, "wins": 0, "losses": 0, "total": 0,
                "avg_win": 0, "avg_loss": 0, "pnl": 0, "best": 0, "worst": 0,
                "avg_tp_hits": 0, "avg_duration_hours": 0, "profit_factor": 0,
                "avg_pnl": 0, "avg_leverage": 0}

    wins = [t for t in trades if t.get("pnl_usd", 0) > 0]
    losses = [t for t in trades if t.get("pnl_usd", 0) < 0]
    total_pnl = sum(t.get("pnl_usd", 0) for t in trades)

    # Duration: compute from opened_at / closed_at
    durations = []
    for t in trades:
        opened = t.get("opened_at", 0)
        closed = t.get("closed_at", 0)
        if opened > 0 and closed > opened:
            durations.append((closed - opened) / 3600)
    avg_duration = sum(durations) / len(durations) if durations else 0

    # TP hits
    tp_hits_list = [len(t.get("tp_details", []) or []) for t in trades]
    avg_tp_hits = sum(tp_hits_list) / len(tp_hits_list) if tp_hits_list else 0

    # Leverage
    leverages = [t.get("leverage", 0) for t in trades if t.get("leverage", 0) > 0]
    avg_leverage = sum(leverages) / len(leverages) if leverages else 0

    # Profit factor
    gross_profit = sum(t["pnl_usd"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl_usd"] for t in losses)) if losses else 0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0

    # Compute percentage-based avg win/loss/pnl (PnL % of collateral)
    def _pnl_pct(t):
        pnl = t.get("pnl_usd", 0)
        size = t.get("size_usd", 0)
        lev = t.get("leverage", 1) or 1
        collateral = size / lev if lev > 0 else size
        return (pnl / collateral * 100) if collateral > 0 else 0

    win_pcts = [_pnl_pct(t) for t in wins]
    loss_pcts = [_pnl_pct(t) for t in losses]
    all_pcts = [_pnl_pct(t) for t in trades]

    return {
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "wins": len(wins),
        "losses": len(losses),
        "total": len(trades),
        "avg_win": round(sum(win_pcts) / len(win_pcts), 1) if win_pcts else 0,
        "avg_loss": round(sum(loss_pcts) / len(loss_pcts), 1) if loss_pcts else 0,
        "avg_win_usd": round(sum(t["pnl_usd"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss_usd": round(sum(t["pnl_usd"] for t in losses) / len(losses), 2) if losses else 0,
        "pnl": round(total_pnl, 2),
        "best": round(max((t["pnl_usd"] for t in trades), default=0), 2),
        "worst": round(min((t["pnl_usd"] for t in trades), default=0), 2),
        "avg_tp_hits": round(avg_tp_hits, 1),
        "avg_duration_hours": round(avg_duration, 1),
        "profit_factor": profit_factor,
        "avg_pnl": round(sum(all_pcts) / len(all_pcts), 1) if all_pcts else 0,
        "avg_leverage": round(avg_leverage, 1),
    }


@app.get("/api/v1/trades/pdf")
async def trades_pdf(token: str = Depends(verify_api_key)):
    """Generate and return a PDF trade history report."""
    all_trades = safe_json_read(TRADE_HISTORY_FILE, [])
    trades = [t for t in all_trades if abs(t.get("pnl_usd", 0)) >= 1]
    trades.sort(key=lambda t: t.get("closed_at", 0), reverse=True)

    if not trades:
        raise HTTPException(404, "No trades to export")

    try:
        from fpdf import FPDF
    except ImportError:
        raise HTTPException(500, "FPDF not installed on server")

    pdf_path = await asyncio.to_thread(_generate_trade_pdf, trades)

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename="trade_history.pdf",
        background=None,
    )


def _generate_trade_pdf(trades: list) -> str:
    """Build PDF with 3-column PnL summary + 3-column trade grid.

    Standalone version of analytics._generate_trade_pdf for REST API use.
    """
    from fpdf import FPDF

    ET = ZoneInfo("America/New_York")
    LMARGIN = 10
    PAGE_W = 210
    RMARGIN = 10
    USABLE_W = PAGE_W - LMARGIN - RMARGIN
    COL_W = USABLE_W / 3
    LINE_H = 5

    # ── Time buckets ──
    now = datetime.now(ET)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_cutoff = int(today_start.timestamp())
    now_ts = int(time.time())
    month_cutoff = now_ts - 30 * 86400

    def _bucket(tlist):
        if not tlist:
            return {"pnl": 0.0, "cnt": 0, "wins": 0}
        pnl = sum(t.get("pnl_usd", 0) for t in tlist)
        w = sum(1 for t in tlist if t.get("pnl_usd", 0) > 0)
        return {"pnl": pnl, "cnt": len(tlist), "wins": w}

    def _s(v):
        return "+" if v >= 0 else "-"

    buckets = []
    for label, cutoff in [("Today", today_cutoff), ("30 Days", month_cutoff), ("All Time", 0)]:
        b = [t for t in trades if t.get("closed_at", 0) >= cutoff]
        stats = _bucket(b)
        sym_stats = {}
        for sym in ("BTC", "ETH"):
            sym_stats[sym] = _bucket([t for t in b if t.get("symbol") == sym])
        losses = stats["cnt"] - stats["wins"]
        wr = f"{stats['wins'] / stats['cnt'] * 100:.0f}%" if stats["cnt"] else "-"
        buckets.append({"label": label, "stats": stats, "sym": sym_stats,
                        "wr": wr, "losses": losses})

    # ── PDF setup ──
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # ── Title ──
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Trade Report", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, f"Generated: {now.strftime('%b %d, %Y %I:%M %p ET')}",
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)

    # ── 3-Column Summary ──
    top_y = pdf.get_y()
    SUMMARY_H = 6 + LINE_H * 5 + 2
    SGAP = 3

    for col_idx, bk in enumerate(buckets):
        x = LMARGIN + col_idx * COL_W + (SGAP / 2)
        box_w = COL_W - SGAP
        y = top_y
        s = bk["stats"]
        pnl_sign = _s(s["pnl"])

        pdf.set_fill_color(245, 245, 248)
        pdf.set_draw_color(220, 220, 220)
        pdf.rect(x, y, box_w, SUMMARY_H, style="DF")

        cx = x + 2
        pdf.set_xy(cx, y + 1)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(box_w - 4, 5, bk["label"])
        y += 7

        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(50, 50, 50)
        lines = [
            f"Trades: {s['cnt']}  ({s['wins']}W / {bk['losses']}L)",
            f"Win Rate: {bk['wr']}",
            f"Net PnL: {pnl_sign}${abs(s['pnl']):,.2f}",
        ]
        for sym in ("BTC", "ETH"):
            ss = bk["sym"][sym]
            wr = f"{ss['wins']}/{ss['cnt']}" if ss["cnt"] else "-"
            lines.append(f"{sym}: {_s(ss['pnl'])}${abs(ss['pnl']):,.2f}  ({wr})")

        for line in lines:
            pdf.set_xy(cx, y)
            pdf.cell(box_w - 4, LINE_H, line)
            y += LINE_H

    pdf.set_text_color(0, 0, 0)
    pdf.set_y(top_y + SUMMARY_H + 5)

    # ── 3-Column Trade Grid ──
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 7, f"Trades ({len(trades)})", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    PAD = 2
    GAP = 3
    CARD_INNER_W = COL_W - GAP
    TP_LINE_H = 3.5
    row_y = pdf.get_y()

    def _card_h(trade):
        base = PAD * 2 + 4 * 5
        tp_count = len(trade.get("tp_details", []))
        sl_count = 1 if trade.get("sl_details") else 0
        unfilled_count = len(trade.get("unfilled_targets", []))
        return base + TP_LINE_H * (tp_count + sl_count + unfilled_count)

    i = 0
    while i < len(trades):
        row_cards = trades[i:i+3]
        max_h = max(_card_h(t) for t in row_cards)

        if i > 0:
            row_y += max_h + GAP
        if row_y + max_h > pdf.h - 15:
            pdf.add_page()
            row_y = pdf.get_y()

        for col, t in enumerate(row_cards):
            card_h = max_h
            x = LMARGIN + col * COL_W + (GAP / 2)
            y = row_y

            pnl = t.get("pnl_usd", 0)
            pnl_sign = _s(pnl)
            pct = t.get("pnl_percentage", 0)
            lev = t.get("leverage", 0)
            exch = t.get("exchange", "gmx").upper()
            ts = t.get("closed_at", 0)
            date_str = datetime.fromtimestamp(ts, tz=ET).strftime("%b %d, %I:%M %p") if ts else "Unknown"
            entry_p = t.get("entry_price", 0)

            pdf.set_draw_color(220, 220, 220)
            pdf.rect(x, y, CARD_INNER_W, card_h)

            if pnl >= 0:
                pdf.set_fill_color(0, 160, 0)
            else:
                pdf.set_fill_color(210, 0, 0)
            pdf.rect(x, y, 1.2, card_h, style="F")

            cx = x + PAD + 1
            cw = CARD_INNER_W - PAD * 2 - 1
            cy = y + PAD

            # Header
            pdf.set_xy(cx, cy)
            pdf.set_text_color(30, 30, 30)
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(cw, 4, f"#{i+col+1} {t.get('symbol', '?')} {t.get('side', '?')} {exch}")
            cy += 4

            # PnL
            pdf.set_xy(cx, cy)
            pdf.set_text_color(0, 140, 0) if pnl >= 0 else pdf.set_text_color(200, 0, 0)
            pdf.set_font("Helvetica", "B", 7.5)
            if pct != 0:
                pct_sign = "+" if pct >= 0 else ""
                pdf.cell(cw, 4, f"PnL: {pnl_sign}${abs(pnl):,.2f} ({pct_sign}{pct:.1f}%)")
            else:
                pdf.cell(cw, 4, f"PnL: {pnl_sign}${abs(pnl):,.2f}")
            cy += 4

            # Size
            pdf.set_text_color(60, 60, 60)
            pdf.set_font("Helvetica", "", 7)
            pdf.set_xy(cx, cy)
            size_usd = t.get("size_usd", 0)
            if lev and lev > 0:
                collateral = size_usd / lev
                pdf.cell(cw, 4, f"Size: ${size_usd:,.2f} @ {lev:.0f}x  (${collateral:,.2f})")
            else:
                pdf.cell(cw, 4, f"Size: ${size_usd:,.2f}")
            cy += 4

            # Entry
            if entry_p:
                pdf.set_xy(cx, cy)
                pdf.cell(cw, 4, f"Entry: ${entry_p:,.2f}")
                cy += 4

            # TP targets
            tp_dets = t.get("tp_details", [])
            for j, tp in enumerate(tp_dets, 1):
                pdf.set_xy(cx, cy)
                pdf.set_font("ZapfDingbats", "", 6)
                pdf.set_text_color(0, 160, 0)
                pdf.cell(3, TP_LINE_H, "4")
                pdf.set_font("Helvetica", "", 6.5)
                pdf.set_text_color(60, 60, 60)
                p_str = f"Target {j}: ${tp.get('price', 0):,.2f}"
                if "pct" in tp:
                    p_str += f" (closed {tp['pct']:.0f}%)"
                if "pnl" in tp:
                    tp_pnl = tp["pnl"]
                    p_str += f" {'+' if tp_pnl >= 0 else '-'}${abs(tp_pnl):,.2f}"
                pdf.cell(cw - 3, TP_LINE_H, p_str)
                cy += TP_LINE_H

            # Trailing SL
            sl_det = t.get("sl_details")
            if sl_det:
                pdf.set_xy(cx, cy)
                pdf.set_font("ZapfDingbats", "", 6)
                pdf.set_text_color(0, 160, 0)
                pdf.cell(3, TP_LINE_H, "4")
                sl_pnl = sl_det.get("pnl", 0)
                pdf.set_font("Helvetica", "", 6.5)
                pdf.set_text_color(60, 60, 60)
                s_str = f"Trailing SL: ${sl_det.get('price', 0):,.2f}"
                if sl_det.get("pct") and sl_det["pct"] > 0:
                    s_str += f" (closed {sl_det['pct']:.0f}%)"
                s_str += f" {'+' if sl_pnl >= 0 else '-'}${abs(sl_pnl):,.2f}"
                pdf.cell(cw - 3, TP_LINE_H, s_str)
                cy += TP_LINE_H

            # Unfilled targets
            unfilled = t.get("unfilled_targets", [])
            tp_count = len(tp_dets)
            for k, uf in enumerate(unfilled, tp_count + 1):
                pdf.set_xy(cx, cy)
                pdf.set_font("ZapfDingbats", "", 6)
                pdf.set_text_color(210, 0, 0)
                pdf.cell(3, TP_LINE_H, "8")
                pdf.set_font("Helvetica", "", 6.5)
                pdf.set_text_color(150, 150, 150)
                pdf.cell(cw - 3, TP_LINE_H, f"Target {k}: ${uf.get('price', 0):,.2f} (Never Hit)")
                cy += TP_LINE_H

            # Date + Duration
            open_ts = t.get("opened_at", 0)
            open_str = datetime.fromtimestamp(open_ts, tz=ET).strftime("%m/%d %I:%M%p") if open_ts and open_ts > 1_000_000_000 else "?"
            close_str = datetime.fromtimestamp(ts, tz=ET).strftime("%m/%d %I:%M%p") if ts and ts > 1_000_000_000 else "?"
            dur_h = t.get("duration_hours", 0)
            if dur_h >= 24:
                dur_str = f"{dur_h / 24:.1f}d"
            elif dur_h >= 1:
                dur_str = f"{dur_h:.1f}h"
            else:
                dur_str = f"{max(dur_h * 60, 1):.0f}m"
            pdf.set_text_color(130, 130, 130)
            pdf.set_font("Helvetica", "", 5.5)
            pdf.set_xy(cx, cy)
            pdf.cell(cw, 4, f"{open_str} - {close_str}  ({dur_str})", align="C")

        i += len(row_cards)

    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, prefix="gmx_trades_")
    pdf.output(tmp.name)
    return tmp.name


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WALLET
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/v1/wallet")
async def wallet_info(token: str = Depends(verify_api_key)):
    """Wallet info — matches iOS WalletResponse."""
    wallets = []
    total_free = 0.0

    for wid, acct in accounts.items():
        usdc = await asyncio.to_thread(_get_usdc_balance, acct)
        total_free += usdc
        wallets.append({
            "wallet_id": wid,
            "address": acct.address,
            "usdc_balance": round(usdc, 2),
        })

    # Include Bitunix balance
    bx_free = 0.0
    if bx_client:
        try:
            from bitunix_executor import get_bitunix_balance
            bx_free = await asyncio.to_thread(get_bitunix_balance, bx_client)
        except Exception:
            pass

    # Total portfolio includes deployed
    deployed = 0.0
    positions = safe_json_read(POSITIONS_FILE, {})
    for pid, p in positions.items():
        if p.get("is_open", False):
            size = p.get("size_usd", 0)
            lev = p.get("leverage", 1)
            deployed += size / lev if lev > 0 else size

    return {
        "total_portfolio": round(total_free + bx_free + deployed, 2),
        "free_usdc": round(total_free, 2),
        "free_usdc_bitunix": round(bx_free, 2),
        "wallets": wallets,
    }


@app.get("/api/v1/wallet/deposit-address")
async def deposit_address(token: str = Depends(verify_api_key)):
    """Get deposit address (W1)."""
    if 1 not in accounts:
        raise HTTPException(status_code=500, detail="No wallet configured")

    return {
        "address": accounts[1].address,
        "network": "Arbitrum One",
    }


class WithdrawRequest(BaseModel):
    amount: float
    to_address: str


@app.post("/api/v1/wallet/withdraw")
async def withdraw(req: WithdrawRequest, token: str = Depends(verify_api_key)):
    """Withdraw USDC — placeholder, requires careful implementation."""
    # Safety: withdrawals need extra verification in production
    raise HTTPException(
        status_code=501,
        detail="Withdrawals via API are disabled for safety. Use Telegram /withdraw command."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SWAP (USDC ↔ USDT via Uniswap V3 on Arbitrum)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Token address mapping
_TOKEN_MAP = {
    "USDC": USDC_ADDRESS,
    "USDT": USDT_ADDRESS,
}

_TOKEN_DECIMALS = {
    "USDC": 6,
    "USDT": 6,
}


def _api_send_tx(to_addr: str, data: bytes, value: int, acct) -> str:
    """Send a raw transaction from the REST API and return tx hash hex string."""
    from web3 import Web3

    if cfg.dry_run:
        import uuid
        return f"dry_run_{uuid.uuid4().hex[:16]}"

    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0)
    priority_fee = w3.to_wei(0.1, "gwei")
    max_fee = base_fee * 2 + priority_fee

    tx = {
        "to": Web3.to_checksum_address(to_addr),
        "from": acct.address,
        "value": value,
        "data": data,
        "nonce": nonce,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "gas": 500000,
        "chainId": w3.eth.chain_id,
        "type": 2,
    }

    try:
        gas_estimate = w3.eth.estimate_gas(tx)
        tx["gas"] = int(gas_estimate * 1.2)
    except Exception as e:
        logger.warning(f"Gas estimation failed: {e}, using 500k default")
        tx["gas"] = 500000

    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)


def _api_wait_receipt(tx_hash: str, timeout: int = 180) -> dict:
    """Wait for a transaction receipt."""
    from web3.exceptions import TransactionNotFound
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = w3.eth.get_transaction_receipt(tx_hash)
            if r is not None:
                return dict(r)
        except TransactionNotFound:
            pass
        time.sleep(1.0)
    raise TimeoutError(f"TX {tx_hash} not confirmed in {timeout}s")


def _get_token_balance(token_address: str, account) -> tuple:
    """Get token balance. Returns (raw_balance, human_balance, decimals)."""
    from web3 import Web3
    token = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=ERC20_ABI,
    )
    decimals = token.functions.decimals().call()
    raw = token.functions.balanceOf(account.address).call()
    return raw, raw / (10 ** decimals), decimals


def _ensure_approval(token_address: str, spender: str, amount_raw: int, acct) -> Optional[str]:
    """Approve spender if allowance is insufficient. Returns tx_hash if approval was needed."""
    from web3 import Web3
    token = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=ERC20_ABI,
    )
    current_allowance = token.functions.allowance(acct.address, Web3.to_checksum_address(spender)).call()
    if current_allowance >= amount_raw:
        return None

    # Approve max uint256 to avoid repeated approvals
    max_uint = 2**256 - 1
    approve_data = token.encode_abi("approve", [Web3.to_checksum_address(spender), max_uint])
    tx_hash = _api_send_tx(token_address, approve_data, 0, acct)
    receipt = _api_wait_receipt(tx_hash)
    if receipt.get("status") != 1:
        raise Exception(f"Approval tx failed: {tx_hash}")
    logger.info(f"Approved {spender[:10]}... to spend {token_address[:10]}... tx={tx_hash}")
    return tx_hash


def _execute_uniswap_swap(
    token_in: str, token_out: str, amount_in_raw: int,
    recipient: str, acct, slippage_bps: int = 50,
) -> tuple:
    """Execute a Uniswap V3 exactInputSingle swap. Returns (tx_hash, amount_out)."""
    from web3 import Web3

    router = w3.eth.contract(
        address=Web3.to_checksum_address(UNISWAP_V3_ROUTER),
        abi=UNISWAP_V3_ABI,
    )

    # Use 0.01% fee tier for stablecoin pairs (100), fallback 0.05% (500)
    fee = 100

    # Slippage: for stablecoin swap, expect ~1:1 minus slippage
    min_out = int(amount_in_raw * (10000 - slippage_bps) / 10000)

    params = (
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        fee,
        Web3.to_checksum_address(recipient),
        int(time.time()) + 600,  # 10 min deadline
        amount_in_raw,
        min_out,
        0,  # sqrtPriceLimitX96 = 0 (no limit)
    )

    swap_data = router.encode_abi("exactInputSingle", [params])
    tx_hash = _api_send_tx(UNISWAP_V3_ROUTER, swap_data, 0, acct)
    receipt = _api_wait_receipt(tx_hash)

    if receipt.get("status") != 1:
        raise Exception(f"Swap tx reverted: {tx_hash}")

    # Parse amount out from Transfer event logs
    amount_out = min_out  # fallback
    for log in receipt.get("logs", []):
        # ERC20 Transfer event topic
        if (
            len(log.get("topics", [])) == 3
            and log["address"].lower() == token_out.lower()
        ):
            try:
                amount_out = int(log["data"], 16) if isinstance(log["data"], str) else int.from_bytes(log["data"], "big")
            except Exception:
                pass

    return tx_hash, amount_out


class SwapQuoteRequest(BaseModel):
    from_token: str
    to_token: str
    amount: float


class SwapExecuteRequest(BaseModel):
    from_token: str
    to_token: str
    amount: float
    destination_address: Optional[str] = None


@app.post("/api/v1/wallet/swap/quote")
async def swap_quote(req: SwapQuoteRequest, token: str = Depends(verify_api_key)):
    """Get a swap quote for USDC ↔ USDT via Uniswap V3."""
    from_token = req.from_token.upper()
    to_token = req.to_token.upper()

    if from_token not in _TOKEN_MAP or to_token not in _TOKEN_MAP:
        raise HTTPException(400, f"Unsupported tokens. Supported: {list(_TOKEN_MAP.keys())}")
    if from_token == to_token:
        raise HTTPException(400, "from_token and to_token must be different")
    if req.amount <= 0:
        raise HTTPException(400, "Amount must be greater than 0")

    # Check W1 balance of from_token
    if 1 not in accounts:
        raise HTTPException(500, "No wallet configured")

    from_addr = _TOKEN_MAP[from_token]
    from_decimals = _TOKEN_DECIMALS[from_token]

    _, balance, _ = await asyncio.to_thread(_get_token_balance, from_addr, accounts[1])

    if balance < req.amount:
        raise HTTPException(400, f"Insufficient {from_token} balance. Have: {balance:.2f}, Need: {req.amount:.2f}")

    # For stablecoins, quote is ~1:1 with minimal impact
    # Actual slippage will be determined on execution
    estimated_out = req.amount * 0.999  # ~0.1% fee estimate
    price_impact = 0.01  # negligible for stablecoin pools

    return {
        "from_token": from_token,
        "to_token": to_token,
        "amount_in": req.amount,
        "amount_out": round(estimated_out, 2),
        "price_impact": price_impact,
        "route": f"{from_token} → Uniswap V3 (0.01%) → {to_token}",
    }


@app.post("/api/v1/wallet/swap/execute")
async def swap_execute(req: SwapExecuteRequest, token: str = Depends(verify_api_key)):
    """Execute USDC ↔ USDT swap via Uniswap V3, optionally transfer to destination."""
    from_token = req.from_token.upper()
    to_token = req.to_token.upper()

    if from_token not in _TOKEN_MAP or to_token not in _TOKEN_MAP:
        raise HTTPException(400, f"Unsupported tokens. Supported: {list(_TOKEN_MAP.keys())}")
    if from_token == to_token:
        raise HTTPException(400, "from_token and to_token must be different")
    if req.amount <= 0:
        raise HTTPException(400, "Amount must be greater than 0")

    if 1 not in accounts:
        raise HTTPException(500, "No wallet configured")

    acct = accounts[1]
    from_addr = _TOKEN_MAP[from_token]
    to_addr = _TOKEN_MAP[to_token]
    from_decimals = _TOKEN_DECIMALS[from_token]
    to_decimals = _TOKEN_DECIMALS[to_token]

    # Validate balance
    _, balance, _ = await asyncio.to_thread(_get_token_balance, from_addr, acct)
    if balance < req.amount:
        raise HTTPException(400, f"Insufficient {from_token} balance. Have: {balance:.2f}, Need: {req.amount:.2f}")

    # Determine destination address: client-provided, server config, or None
    dest_address = None
    raw_dest = req.destination_address or (
        cfg.bitunix_deposit_address if to_token == "USDT" else None
    )
    if raw_dest:
        from web3 import Web3
        if not Web3.is_address(raw_dest):
            raise HTTPException(400, "Invalid destination address")
        dest_address = Web3.to_checksum_address(raw_dest)

    amount_in_raw = int(req.amount * (10 ** from_decimals))

    try:
        # Step 1: Approve Uniswap router to spend from_token
        logger.info(f"Swap: approving {from_token} for Uniswap router...")
        await asyncio.to_thread(_ensure_approval, from_addr, UNISWAP_V3_ROUTER, amount_in_raw, acct)

        # Step 2: Execute swap
        # If no destination, swap directly to W1. If destination, swap to W1 first then transfer.
        logger.info(f"Swap: executing {req.amount} {from_token} → {to_token}...")
        swap_tx_hash, amount_out_raw = await asyncio.to_thread(
            _execute_uniswap_swap,
            from_addr, to_addr, amount_in_raw,
            acct.address,  # always receive to W1 first
            acct,
        )
        amount_out = amount_out_raw / (10 ** to_decimals)
        logger.info(f"Swap complete: {req.amount} {from_token} → {amount_out:.2f} {to_token}, tx={swap_tx_hash}")

        # Step 3: Transfer to destination if provided
        transfer_tx_hash = None
        if dest_address:
            logger.info(f"Transferring {amount_out:.2f} {to_token} to {dest_address[:10]}...")
            to_contract = w3.eth.contract(
                address=Web3.to_checksum_address(to_addr),
                abi=ERC20_ABI,
            )
            transfer_data = to_contract.encode_abi(
                "transfer",
                [dest_address, amount_out_raw],
            )
            transfer_tx_hash = await asyncio.to_thread(
                _api_send_tx, to_addr, transfer_data, 0, acct
            )
            receipt = await asyncio.to_thread(_api_wait_receipt, transfer_tx_hash)
            if receipt.get("status") != 1:
                # Swap succeeded but transfer failed
                return {
                    "success": False,
                    "message": f"Swap succeeded but transfer to {dest_address[:10]}... failed",
                    "swap_tx_hash": swap_tx_hash,
                    "transfer_tx_hash": transfer_tx_hash,
                    "amount_swapped": req.amount,
                    "amount_received": round(amount_out, 2),
                }
            logger.info(f"Transfer complete: {amount_out:.2f} {to_token} → {dest_address[:10]}..., tx={transfer_tx_hash}")

        return {
            "success": True,
            "message": f"Swapped {req.amount:.2f} {from_token} → {amount_out:.2f} {to_token}"
                       + (f" and sent to {dest_address[:10]}..." if dest_address else ""),
            "swap_tx_hash": swap_tx_hash,
            "transfer_tx_hash": transfer_tx_hash,
            "amount_swapped": req.amount,
            "amount_received": round(amount_out, 2),
        }

    except Exception as e:
        logger.error(f"Swap failed: {e}")
        raise HTTPException(500, f"Swap failed: {str(e)}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PRICES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/v1/prices")
async def get_prices(
    symbols: str = Query("BTC,ETH,SOL,LINK"),
    token: str = Depends(verify_api_key),
):
    """Live prices from Chainlink oracles."""
    requested = [s.strip().upper() for s in symbols.split(",")]
    prices = {}

    for symbol in requested:
        if symbol in ALLOWED_SYMBOLS:
            price = await asyncio.to_thread(_get_chainlink_price, symbol)
            prices[symbol] = {
                "price": price,
                "source": "chainlink",
            }

    return prices


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SIGNALS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/v1/signals")
async def list_signals(
    limit: int = Query(20, ge=1, le=100),
    token: str = Depends(verify_api_key),
):
    """Recent signals from signal store."""
    signals = safe_json_read(SIGNAL_STORE_FILE, [])

    # Most recent first
    signals.sort(key=lambda s: s.get("timestamp_received", 0), reverse=True)
    signals = signals[:limit]

    return {"signals": signals}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIG (read-only, safe fields only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/v1/config")
async def get_config(token: str = Depends(verify_api_key)):
    """Bot configuration (safe fields only — no private keys)."""
    return {
        "exchange_mode": cfg.exchange_mode,
        "min_leverage": cfg.min_leverage,
        "max_leverage": cfg.max_leverage,
        "max_position_usd": cfg.max_position_usd,
        "min_position_usd": cfg.min_position_usd,
        "portfolio_pct": cfg.portfolio_pct,
        "portfolio_fixed_usd": getattr(cfg, "portfolio_fixed_usd", 0),
        "bitunix_portfolio_pct": cfg.bitunix_portfolio_pct,
        "bitunix_portfolio_fixed_usd": getattr(cfg, "bitunix_portfolio_fixed_usd", 0),
        "require_sl": cfg.require_sl,
        "require_tp": cfg.require_tp,
        "dry_run": cfg.dry_run,
        "network": cfg.network,
        "slippage_bps": cfg.slippage_bps,
        "allowed_symbols": list(ALLOWED_SYMBOLS),
    }


class ConfigUpdateRequest(BaseModel):
    portfolio_pct: Optional[float] = None
    portfolio_fixed_usd: Optional[float] = None
    bitunix_portfolio_pct: Optional[float] = None
    bitunix_portfolio_fixed_usd: Optional[float] = None


@app.post("/api/v1/config/update")
async def update_config(req: ConfigUpdateRequest, token: str = Depends(verify_api_key)):
    """Update bot configuration (trade size percentages or fixed USD amounts)."""
    updated = {}
    if req.portfolio_pct is not None:
        if not (0.01 <= req.portfolio_pct <= 1.0):
            raise HTTPException(status_code=400, detail="portfolio_pct must be 0.01-1.0")
        cfg.portfolio_pct = req.portfolio_pct
        updated["portfolio_pct"] = cfg.portfolio_pct
    if req.portfolio_fixed_usd is not None:
        if req.portfolio_fixed_usd < 0:
            raise HTTPException(status_code=400, detail="portfolio_fixed_usd must be >= 0")
        cfg.portfolio_fixed_usd = req.portfolio_fixed_usd
        updated["portfolio_fixed_usd"] = cfg.portfolio_fixed_usd
    if req.bitunix_portfolio_pct is not None:
        if not (0.01 <= req.bitunix_portfolio_pct <= 1.0):
            raise HTTPException(status_code=400, detail="bitunix_portfolio_pct must be 0.01-1.0")
        cfg.bitunix_portfolio_pct = req.bitunix_portfolio_pct
        updated["bitunix_portfolio_pct"] = cfg.bitunix_portfolio_pct
    if req.bitunix_portfolio_fixed_usd is not None:
        if req.bitunix_portfolio_fixed_usd < 0:
            raise HTTPException(status_code=400, detail="bitunix_portfolio_fixed_usd must be >= 0")
        cfg.bitunix_portfolio_fixed_usd = req.bitunix_portfolio_fixed_usd
        updated["bitunix_portfolio_fixed_usd"] = cfg.bitunix_portfolio_fixed_usd
    return {"success": True, "updated": updated}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# API KEY MANAGEMENT (generate new key via CLI)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    import uvicorn

    # Allow generating a key from CLI
    if len(sys.argv) > 1 and sys.argv[1] == "genkey":
        _init_web3_and_accounts()
        key = generate_api_key()
        print(f"\nYour new API key:\n  {key}\n")
        print("Use this in the iOS app Settings > API Key field.")
        sys.exit(0)

    print("Starting GMX Trading Bot REST API...")
    print("Endpoints: http://0.0.0.0:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
