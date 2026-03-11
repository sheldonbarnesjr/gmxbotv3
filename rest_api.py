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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Globals initialized at startup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
cfg = None
w3 = None
accounts = {}  # {wallet_id: Account}
bx_client = None  # BitunixClient (if configured)
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_web3_and_accounts()
    # Generate initial API key if none exist
    keys = _load_api_keys()
    if not keys:
        key = generate_api_key()
        print(f"\n{'='*60}")
        print(f"  YOUR API KEY (save this for the iOS app):")
        print(f"  {key}")
        print(f"{'='*60}\n")
    yield


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
    # Calculate free USDC across all wallets
    free_usdc = 0.0
    for wid, acct in accounts.items():
        free_usdc += await asyncio.to_thread(_get_usdc_balance, acct)

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
    if bx_client:
        try:
            from bitunix_executor import get_bitunix_balance, get_bitunix_positions
            bx_bal = await asyncio.to_thread(get_bitunix_balance, bx_client)
            bx_positions = await asyncio.to_thread(get_bitunix_positions, bx_client)
            free_usdc += bx_bal
            for bp in bx_positions:
                bx_margin = float(bp.get("margin", 0))
                bx_pnl = float(bp.get("unrealizedPNL", 0))
                deployed_collateral += bx_margin
                unrealized_pnl += bx_pnl
        except Exception as e:
            logger.warning(f"Bitunix balance fetch failed: {e}")

    total_portfolio = free_usdc + deployed_collateral + unrealized_pnl

    # 24h change from balance snapshots
    snapshots = safe_json_read(BALANCE_SNAPSHOTS_FILE, [])
    change_24h_usd = 0.0
    change_24h_pct = 0.0
    has_24h_data = False

    if snapshots:
        target_ts = time.time() - 86400
        closest = min(snapshots, key=lambda s: abs(s["timestamp"] - target_ts))
        if abs(closest["timestamp"] - target_ts) < 6 * 3600:
            old_total = closest["total_portfolio"]
            if old_total > 0:
                change_24h_usd = total_portfolio - old_total
                change_24h_pct = (change_24h_usd / old_total) * 100
                has_24h_data = True

    return {
        "total_portfolio": round(total_portfolio, 2),
        "free_usdc": round(free_usdc, 2),
        "deployed_collateral": round(deployed_collateral, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "change_24h_usd": round(change_24h_usd, 2),
        "change_24h_pct": round(change_24h_pct, 2),
        "has_24h_data": has_24h_data,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHART
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/v1/dashboard/chart")
async def dashboard_chart(
    period: str = Query("24h"),
    token: str = Depends(verify_api_key),
):
    """Portfolio chart data from balance snapshots + trade history."""
    snapshots = safe_json_read(BALANCE_SNAPSHOTS_FILE, [])

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
    cutoff = time.time() - (period_hours * 3600)
    filtered = [s for s in snapshots if s["timestamp"] >= cutoff]

    points = [{"timestamp": s["timestamp"], "value": s["total_portfolio"]} for s in filtered]

    # If we don't have enough snapshot data, build from trade history
    if len(points) < 3 and period in ("7d", "30d", "all"):
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

    return {
        "period": period,
        "points": points,
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
                "avg_win": 0, "avg_loss": 0, "pnl": 0, "best": 0, "worst": 0}

    wins = [t for t in trades if t.get("pnl_usd", 0) > 0]
    losses = [t for t in trades if t.get("pnl_usd", 0) < 0]
    total_pnl = sum(t.get("pnl_usd", 0) for t in trades)

    return {
        "win_rate": len(wins) / len(trades) * 100,
        "wins": len(wins),
        "losses": len(losses),
        "total": len(trades),
        "avg_win": sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0,
        "avg_loss": sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0,
        "pnl": round(total_pnl, 2),
        "best": max((t["pnl_usd"] for t in trades), default=0),
        "worst": min((t["pnl_usd"] for t in trades), default=0),
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
        "require_sl": cfg.require_sl,
        "require_tp": cfg.require_tp,
        "dry_run": cfg.dry_run,
        "network": cfg.network,
        "slippage_bps": cfg.slippage_bps,
        "allowed_symbols": list(ALLOWED_SYMBOLS),
    }


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
