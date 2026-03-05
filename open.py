#!/usr/bin/env python3
"""
open.py — GMX V2 full signal executor.

Parses a trading signal, opens a MarketIncrease position, then places
LimitDecrease (take-profit) and StopLossDecrease orders on-chain.

Signal format (env var SIGNAL or stdin):
    BTC LONG
    Entry: 45000-46000
    TP1: 48000 (50% close)
    TP2: 50000 (30% close)
    TP3: 52000 (20% close)
    SL: 43000
    Leverage: 10x

Usage:
    # Via env vars (simple)
    python3 open.py

    # Via signal text
    echo "BTC LONG ..." | python3 open.py --signal

    # Just show positions
    python3 open.py --positions
"""

import os
import sys
import re
import time
import json
import logging
import functools
import urllib.request
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

from web3 import Web3
from eth_account import Account

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("gmx-v2-open")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Retry utility for on-chain operations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def retry_on_chain(max_retries: int = 3, base_delay: float = 2.0, label: str = ""):
    """Decorator that retries on-chain calls with exponential backoff.

    Retries on network/RPC errors (ConnectionError, TimeoutError, ValueError
    from nonce issues, etc.). Does NOT retry on reverted transactions
    (RuntimeError from receipt.status != 1) since those indicate logic errors.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except RuntimeError:
                    raise  # Reverted tx — don't retry, it will keep reverting
                except (ConnectionError, TimeoutError, OSError) as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = base_delay * (2 ** (attempt - 1))
                        fn_label = label or func.__name__
                        log.warning(
                            f"{fn_label}: attempt {attempt}/{max_retries} failed "
                            f"({type(e).__name__}: {e}), retrying in {delay:.0f}s..."
                        )
                        time.sleep(delay)
                    else:
                        log.error(f"{fn_label}: all {max_retries} attempts failed")
                except Exception as e:
                    # Catch Web3 RPC errors (often wrapped as ValueError or generic Exception)
                    err_str = str(e).lower()
                    is_rpc_error = any(kw in err_str for kw in [
                        "connection", "timeout", "nonce too low", "replacement",
                        "already known", "rate limit", "502", "503", "429",
                    ])
                    if is_rpc_error and attempt < max_retries:
                        last_exc = e
                        delay = base_delay * (2 ** (attempt - 1))
                        fn_label = label or func.__name__
                        log.warning(
                            f"{fn_label}: attempt {attempt}/{max_retries} RPC error "
                            f"({e}), retrying in {delay:.0f}s..."
                        )
                        time.sleep(delay)
                    else:
                        raise
            raise last_exc
        return wrapper
    return decorator


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Minimal ABIs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ERC20_ABI = [
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
]

EXCHANGE_ROUTER_ABI = [
    {
        "name": "multicall", "type": "function", "stateMutability": "payable",
        "inputs": [{"name": "data", "type": "bytes[]"}],
        "outputs": [{"type": "bytes[]"}],
    },
    {
        "name": "sendWnt", "type": "function", "stateMutability": "payable",
        "inputs": [{"name": "receiver", "type": "address"},
                   {"name": "amount", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "sendTokens", "type": "function", "stateMutability": "nonpayable",
        "inputs": [{"name": "token", "type": "address"},
                   {"name": "receiver", "type": "address"},
                   {"name": "amount", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "cancelOrder", "type": "function", "stateMutability": "payable",
        "inputs": [{"name": "key", "type": "bytes32"}],
        "outputs": [],
    },
    {
        "name": "createOrder", "type": "function", "stateMutability": "payable",
        "inputs": [
            {
                "name": "params", "type": "tuple",
                "components": [
                    {
                        "name": "addresses", "type": "tuple",
                        "components": [
                            {"name": "receiver", "type": "address"},
                            {"name": "cancellationReceiver", "type": "address"},
                            {"name": "callbackContract", "type": "address"},
                            {"name": "uiFeeReceiver", "type": "address"},
                            {"name": "market", "type": "address"},
                            {"name": "initialCollateralToken", "type": "address"},
                            {"name": "swapPath", "type": "address[]"},
                        ],
                    },
                    {
                        "name": "numbers", "type": "tuple",
                        "components": [
                            {"name": "sizeDeltaUsd", "type": "uint256"},
                            {"name": "initialCollateralDeltaAmount", "type": "uint256"},
                            {"name": "triggerPrice", "type": "uint256"},
                            {"name": "acceptablePrice", "type": "uint256"},
                            {"name": "executionFee", "type": "uint256"},
                            {"name": "callbackGasLimit", "type": "uint256"},
                            {"name": "minOutputAmount", "type": "uint256"},
                            {"name": "validFromTime", "type": "uint256"},
                        ],
                    },
                    {"name": "orderType", "type": "uint8"},
                    {"name": "decreasePositionSwapType", "type": "uint8"},
                    {"name": "isLong", "type": "bool"},
                    {"name": "shouldUnwrapNativeToken", "type": "bool"},
                    {"name": "autoCancel", "type": "bool"},
                    {"name": "referralCode", "type": "bytes32"},
                    {"name": "dataList", "type": "bytes32[]"},
                ],
            }
        ],
        "outputs": [{"type": "bytes32"}],
    },
]

READER_ABI = [
    {
        "name": "getAccountOrders", "type": "function", "stateMutability": "view",
        "inputs": [
            {"name": "dataStore", "type": "address"},
            {"name": "account", "type": "address"},
            {"name": "start", "type": "uint256"},
            {"name": "end", "type": "uint256"},
        ],
        "outputs": [
            {
                "name": "", "type": "tuple[]",
                "components": [
                    {"name": "orderKey", "type": "bytes32"},
                    {
                        "name": "order", "type": "tuple",
                        "components": [
                            {
                                "name": "addresses", "type": "tuple",
                                "components": [
                                    {"name": "account", "type": "address"},
                                    {"name": "receiver", "type": "address"},
                                    {"name": "cancellationReceiver", "type": "address"},
                                    {"name": "callbackContract", "type": "address"},
                                    {"name": "uiFeeReceiver", "type": "address"},
                                    {"name": "market", "type": "address"},
                                    {"name": "initialCollateralToken", "type": "address"},
                                    {"name": "swapPath", "type": "address[]"},
                                ],
                            },
                            {
                                "name": "numbers", "type": "tuple",
                                "components": [
                                    {"name": "orderType", "type": "uint8"},
                                    {"name": "decreasePositionSwapType", "type": "uint8"},
                                    {"name": "sizeDeltaUsd", "type": "uint256"},
                                    {"name": "initialCollateralDeltaAmount", "type": "uint256"},
                                    {"name": "triggerPrice", "type": "uint256"},
                                    {"name": "acceptablePrice", "type": "uint256"},
                                    {"name": "executionFee", "type": "uint256"},
                                    {"name": "callbackGasLimit", "type": "uint256"},
                                    {"name": "minOutputAmount", "type": "uint256"},
                                    {"name": "updatedAtTime", "type": "uint256"},
                                    {"name": "validFromTime", "type": "uint256"},
                                    {"name": "srcChainId", "type": "uint256"},
                                ],
                            },
                            {
                                "name": "flags", "type": "tuple",
                                "components": [
                                    {"name": "isLong", "type": "bool"},
                                    {"name": "shouldUnwrapNativeToken", "type": "bool"},
                                    {"name": "isFrozen", "type": "bool"},
                                    {"name": "autoCancel", "type": "bool"},
                                ],
                            },
                            {"name": "_dataList", "type": "bytes32[]"},
                        ],
                    },
                ],
            },
        ],
    },
    {
        "name": "getAccountPositions", "type": "function", "stateMutability": "view",
        "inputs": [
            {"name": "dataStore", "type": "address"},
            {"name": "account", "type": "address"},
            {"name": "start", "type": "uint256"},
            {"name": "end", "type": "uint256"},
        ],
        "outputs": [
            {
                "name": "", "type": "tuple[]",
                "components": [
                    {"name": "addresses", "type": "tuple", "components": [
                        {"name": "account", "type": "address"},
                        {"name": "market", "type": "address"},
                        {"name": "collateralToken", "type": "address"},
                    ]},
                    {"name": "numbers", "type": "tuple", "components": [
                        {"name": "sizeInUsd", "type": "uint256"},
                        {"name": "sizeInTokens", "type": "uint256"},
                        {"name": "collateralAmount", "type": "uint256"},
                        {"name": "borrowingFactor", "type": "uint256"},
                        {"name": "fundingFeeAmountPerSize", "type": "uint256"},
                        {"name": "longTokenClaimableFundingAmountPerSize", "type": "uint256"},
                        {"name": "shortTokenClaimableFundingAmountPerSize", "type": "uint256"},
                        {"name": "increasedAtTime", "type": "uint256"},
                        {"name": "decreasedAtTime", "type": "uint256"},
                    ]},
                    {"name": "flags", "type": "tuple", "components": [
                        {"name": "isLong", "type": "bool"},
                    ]},
                ],
            },
        ],
    },
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GMX_V2_READER = "0xf60becbba223EEA9495Da3f606753867eC10d139"
GMX_V2_DATASTORE = "0xFD70de6b91282D8017aA4E741e9Ae325CAb992d8"
GMX_V2_EVENT_EMITTER = "0xC8ee91A54287DB53897056e12D9819156D3822Fb"

# GMX V2 OrderType enum
ORDER_TYPE_MARKET_SWAP = 0
ORDER_TYPE_LIMIT_SWAP = 1
ORDER_TYPE_MARKET_INCREASE = 2
ORDER_TYPE_LIMIT_INCREASE = 3
ORDER_TYPE_MARKET_DECREASE = 4
ORDER_TYPE_LIMIT_DECREASE = 5       # Take Profit
ORDER_TYPE_STOP_LOSS_DECREASE = 6   # Stop Loss
ORDER_TYPE_LIQUIDATION = 7

DECREASE_SWAP_TYPE_NO_SWAP = 0

ZERO_ADDR = "0x0000000000000000000000000000000000000000"
MAX_UINT256 = (1 << 256) - 1

# Known index token decimals by symbol
INDEX_TOKEN_DECIMALS = {
    "BTC": 8,   # WBTC
    "ETH": 18,
    "SOL": 9,
    "LINK": 18,
    "ARB": 18,
    "DOGE": 8,
    "AVAX": 18,
}

# CoinGecko symbol mapping
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "LINK": "chainlink",
    "ARB": "arbitrum",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Signal Dataclass
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dataclass
class TakeProfit:
    price: float
    close_pct: float  # 0.0–1.0

@dataclass
class Signal:
    symbol: str
    side: str              # "LONG" or "SHORT"
    entry_low: float
    entry_high: float
    take_profits: List[TakeProfit]
    stop_loss: float
    leverage: float
    raw_text: str = ""
    trade_type: str = "scalp"  # "swing" or "scalp"
    swing_keyword_match: bool = False  # True only if classified via explicit keyword

    @property
    def is_long(self) -> bool:
        return self.side == "LONG"

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Signal Classifier — Swing vs Scalp
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Keywords configurable via env — comma-separated
_SWING_KEYWORDS_RAW = os.getenv(
    "SWING_KEYWORDS",
    "swing,long term,long-term,hold,htf,weekly,daily,position trade,macro,mid term,mid-term,spot"
)
_SCALP_KEYWORDS_RAW = os.getenv(
    "SCALP_KEYWORDS",
    "scalp,intraday,day trade,quick,ltf,15m,5m,1h,sniper,short term,short-term"
)
SWING_KEYWORDS = [k.strip().lower() for k in _SWING_KEYWORDS_RAW.split(",") if k.strip()]
SCALP_KEYWORDS = [k.strip().lower() for k in _SCALP_KEYWORDS_RAW.split(",") if k.strip()]

# Heuristic threshold — no keywords, leverage decides
SCALP_MIN_LEVERAGE = float(os.getenv("SCALP_MIN_LEVERAGE", "10"))      # ≥ 10x → scalp


def classify_signal(signal: 'Signal') -> str:
    """Classify a signal as 'swing' or 'scalp' based on keywords and leverage.

    Priority:
      1. Explicit keywords in the raw text (swing/scalp/long term/etc.)
      2. Leverage heuristic: < 10x → swing, >= 10x → scalp

    Also sets signal.swing_keyword_match = True when classification is
    based on an explicit swing keyword (not heuristic).
    """
    txt = signal.raw_text.lower()

    # Check for swing keywords first
    for kw in SWING_KEYWORDS:
        if kw in txt:
            signal.swing_keyword_match = True
            return "swing"

    # Check for scalp keywords
    for kw in SCALP_KEYWORDS:
        if kw in txt:
            signal.swing_keyword_match = False
            return "scalp"

    # No keywords matched — classify by leverage alone
    # < 10x → swing (heuristic, NOT keyword match)
    # >= 10x → scalp
    signal.swing_keyword_match = False
    if signal.leverage >= SCALP_MIN_LEVERAGE:
        return "scalp"

    return "swing"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TP Percentage Override from .env — Per TP-Count Configuration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Each TP count (2-8) has its own set of percentages:
#   TP_2_1, TP_2_2                         → when signal has 2 TPs
#   TP_3_1, TP_3_2, TP_3_3                 → when signal has 3 TPs
#   TP_4_1, TP_4_2, TP_4_3, TP_4_4         → when signal has 4 TPs
#   ...
#   TP_8_1, TP_8_2, ..., TP_8_8            → when signal has 8 TPs
#
# Values are 0-100 (percentages). Must sum to 100.
# Set all to 0 for a given count to use the signal's own distribution.

def _load_env_tp_dist(n_tps: int, prefix: str = "TP") -> List[float]:
    """Load {prefix}_{n}_{1..n} from .env for a specific TP count.
    Returns list of floats in 0-1 scale, or empty list if not configured.

    prefix="TP"       → reads TP_4_1, TP_4_2, ...  (scalp)
    prefix="SWING_TP" → reads SWING_TP_4_1, SWING_TP_4_2, ...  (swing)
    """
    if n_tps < 2 or n_tps > 8:
        return []

    pcts = []
    for i in range(1, n_tps + 1):
        val = os.getenv(f"{prefix}_{n_tps}_{i}", "0")
        try:
            pcts.append(float(val) / 100.0)
        except (ValueError, TypeError):
            pcts.append(0.0)
    return pcts


def apply_env_tp_pcts(take_profits: List[TakeProfit], trade_type: str,
                      swing_keyword_match: bool = False) -> List[TakeProfit]:
    """Apply env-configured TP percentage splits (TP_3_1, TP_4_1, etc.).

    Reads TP_{n}_{i} from .env for the given TP count.
    Falls back to equal distribution if not configured.
    """
    n_tps = len(take_profits)
    if n_tps < 2:
        return take_profits

    env_pcts = _load_env_tp_dist(n_tps)
    if env_pcts and len(env_pcts) == n_tps and sum(env_pcts) > 0:
        for i, tp in enumerate(take_profits):
            tp.close_pct = env_pcts[i]
    else:
        # Fallback: equal distribution
        each = 1.0 / n_tps
        for tp in take_profits:
            tp.close_pct = each

    # Ensure exact 1.0 total (absorb rounding into last TP)
    actual_total = sum(tp.close_pct for tp in take_profits)
    if take_profits and abs(actual_total - 1.0) > 0.001:
        take_profits[-1].close_pct += 1.0 - actual_total

    return take_profits


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Signal Parser
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def parse_signal(text: str) -> Signal:
    """Parse a trading signal from text.

    Supports formats like:
        BTC LONG
        Entry: 45000-46000
        TP1: 48000 (50% close)
        TP2: 50000 (30% close)
        TP3: 52000 (20% close)
        SL: 43000
        Leverage: 10x
    """
    txt = text.strip()
    if not txt:
        raise ValueError("Empty signal text")

    # ── Symbol ──
    sym_match = re.search(r'\b([A-Z]{2,10})\b', txt)
    if not sym_match:
        raise ValueError("Could not find symbol in signal")
    symbol = sym_match.group(1)
    # Skip common non-symbol words
    skip = {"LONG", "SHORT", "BUY", "SELL", "ENTRY", "SL", "TP", "STOP", "TAKE",
            "PROFIT", "LEVERAGE", "LEV", "RISK", "NORMAL", "HIGH", "LOW", "USD",
            "USDT", "CLOSE", "TRAILING", "ENABLED", "VIP", "GROUP", "SCALP",
            "SWING", "TARGET", "GAIN", "LOSS", "RR", "SSL", "VRVP", "EMA",
            "BEARISH", "BULLISH", "DIV", "DIVS", "SET"}
    for m in re.finditer(r'\b([A-Z]{2,10})\b', txt):
        candidate = m.group(1)
        if candidate not in skip and candidate in COINGECKO_IDS:
            symbol = candidate
            break

    # ── Side ──
    side_match = re.search(r'\b(LONG|SHORT|BUY|SELL)\b', txt, re.IGNORECASE)
    if not side_match:
        raise ValueError("Could not find side (LONG/SHORT/BUY/SELL)")
    raw_side = side_match.group(1).upper()
    side = "LONG" if raw_side in ("LONG", "BUY") else "SHORT"

    # ── Entry ──
    entry_match = re.search(
        r'(?:ENTRY|ENTER|ENTRY\s*ZONE)\s*[:=@\-]?\s*\$?([\d,]+(?:\.\d+)?)\s*[a-zA-Z]*\s*[-–]\s*\$?([\d,]+(?:\.\d+)?)',
        txt, re.IGNORECASE
    )
    if entry_match:
        entry_low = float(entry_match.group(1).replace(",", ""))
        entry_high = float(entry_match.group(2).replace(",", ""))
        if entry_low > entry_high:
            entry_low, entry_high = entry_high, entry_low
    else:
        # Single entry price
        entry_single = re.search(
            r'(?:ENTRY|ENTER)\s*[:=@\-]?\s*\$?([\d,]+(?:\.\d+)?)',
            txt, re.IGNORECASE
        )
        if entry_single:
            entry_low = entry_high = float(entry_single.group(1).replace(",", ""))
        else:
            # No explicit entry — will use current market price at execution
            entry_low = entry_high = 0.0

    # ── Take Profits ──
    # Try both TP patterns and use whichever finds more matches.
    # This prevents a single accidental "TP" match (e.g. a summary line)
    # from suppressing the richer "Target 1:", "Target 2:", ... matches.

    # Pattern 1: TP1: 48000 (50% close)  or  TP 1: 48000 50%
    # The separator [:=@\-$] is REQUIRED to prevent backtracking where
    # the TP number (e.g. "1") gets captured as the price ($1).
    tp_pattern = re.compile(
        r'\bTP\s*(\d+)?\s*[:=@\-$]\s*\$?([\d,]+(?:\.\d+)?)\s*'
        r'(?:\(?\s*(\d+)\s*%\s*(?:close)?\s*\)?)?',
        re.IGNORECASE
    )
    tp_from_tp = []
    for m in tp_pattern.finditer(txt):
        price = float(m.group(2).replace(",", ""))
        pct_str = m.group(3)
        pct = float(pct_str) / 100.0 if pct_str else None
        tp_from_tp.append(TakeProfit(price=price, close_pct=pct or 0))

    # Pattern 2: TARGET 1: 48000, TAKE PROFIT: 48000
    target_pattern = re.compile(
        r'(?:TARGET|TAKE\s*PROFIT)\s*\d*\s*[:=@\-]?\s*\$?([\d,]+(?:\.\d+)?)\s*'
        r'(?:\(?\s*(\d+)\s*%\s*(?:close)?\s*\)?)?',
        re.IGNORECASE
    )
    tp_from_target = []
    for m in target_pattern.finditer(txt):
        price = float(m.group(1).replace(",", ""))
        pct_str = m.group(2)
        pct = float(pct_str) / 100.0 if pct_str else None
        tp_from_target.append(TakeProfit(price=price, close_pct=pct or 0))

    # Merge both pattern results and deduplicate by price.
    # If both patterns found results, combine them (handles mixed-format signals).
    # Dedup by price to avoid doubles when both patterns match the same line.
    if tp_from_tp and tp_from_target:
        seen_prices = set()
        take_profits = []
        for tp in tp_from_tp + tp_from_target:
            if tp.price not in seen_prices:
                seen_prices.add(tp.price)
                take_profits.append(tp)
    else:
        take_profits = tp_from_tp or tp_from_target

    # Sanity check: filter out TP prices that are absurdly far from entry.
    # This catches regex backtracking bugs (e.g. TP number parsed as price: $1, $2, $3)
    # and other parsing artifacts. A real TP should be within 10%-1000% of entry.
    if entry_low > 0 and take_profits:
        sane_tps = []
        for tp in take_profits:
            ratio = tp.price / entry_low
            if 0.10 <= ratio <= 10.0:
                sane_tps.append(tp)
            else:
                log.warning(
                    f"Filtered absurd TP price ${tp.price:,.2f} "
                    f"(entry ~${entry_low:,.0f}, ratio={ratio:.4f})"
                )
        if sane_tps:
            take_profits = sane_tps
        elif take_profits:
            log.error(
                f"ALL {len(take_profits)} TPs failed sanity check — "
                f"keeping originals to avoid empty TP list"
            )

    # Cap at 8 TPs max — drop extras (keep the closest targets)
    MAX_TPS = 8
    if len(take_profits) > MAX_TPS:
        log.warning(
            f"Signal has {len(take_profits)} TPs — trimming to {MAX_TPS}"
        )
        take_profits = take_profits[:MAX_TPS]

    # Assign close percentages if not specified in the signal.
    # Each TP's close_pct is a fraction of the ORIGINAL position size.
    # All close_pct values must sum to 1.0 (100%) so the full position
    # is closed across all TPs.
    #
    # The last TP always absorbs any rounding remainder so the total
    # is exactly 1.0 — no dust position left behind.
    DEFAULT_TP_DISTRIBUTIONS = {
        2:  [0.33, 0.67],
        3:  [0.20, 0.50, 0.30],
        4:  [0.15, 0.30, 0.30, 0.25],
        5:  [0.10, 0.20, 0.30, 0.25, 0.15],
        6:  [0.08, 0.15, 0.22, 0.22, 0.18, 0.15],
        7:  [0.06, 0.12, 0.18, 0.20, 0.18, 0.14, 0.12],
        8:  [0.05, 0.10, 0.15, 0.18, 0.17, 0.14, 0.11, 0.10],
    }

    if take_profits:
        all_unspecified = all(tp.close_pct == 0 for tp in take_profits)
        specified = sum(tp.close_pct for tp in take_profits)
        unspecified = [tp for tp in take_profits if tp.close_pct == 0]

        if all_unspecified and len(take_profits) in DEFAULT_TP_DISTRIBUTIONS:
            # All TPs missing % → use smart default distribution
            dist = DEFAULT_TP_DISTRIBUTIONS[len(take_profits)]
            for tp, pct in zip(take_profits, dist):
                tp.close_pct = pct
        elif all_unspecified:
            # More TPs than we have a preset for — use ascending weights
            n = len(take_profits)
            weights = list(range(1, n + 1))
            total_w = sum(weights)
            for tp, w in zip(take_profits, weights):
                tp.close_pct = w / total_w
        elif unspecified:
            # Some explicit, some missing → split remaining evenly
            remaining = max(0, 1.0 - specified)
            each = remaining / len(unspecified) if unspecified else 0
            for tp in unspecified:
                tp.close_pct = each

        # Clamp: if explicit TPs already sum >1.0, normalize all down
        total_pct = sum(tp.close_pct for tp in take_profits)
        if total_pct > 1.0 + 0.001:
            log.warning(
                f"TP percentages sum to {total_pct:.2f} (>1.0) — normalizing"
            )
            for tp in take_profits:
                tp.close_pct /= total_pct

        # Ensure total sums to exactly 1.0 — push any rounding
        # remainder onto the last TP so the position fully closes.
        total_pct = sum(tp.close_pct for tp in take_profits)
        if take_profits and abs(total_pct - 1.0) > 0.001:
            take_profits[-1].close_pct += 1.0 - total_pct

    # ── Stop Loss ──
    sl_match = re.search(
        r'(?:\bSL\b|STOP\s*LOSS|STOP(?=\s*[:=@\-$]))\s*[:=@\-]?\s*\$?(\d[\d,]*(?:\.\d+)?)',
        txt, re.IGNORECASE
    )
    if not sl_match:
        raise ValueError("Could not find stop loss price")
    stop_loss = float(sl_match.group(1).replace(",", ""))

    # ── Leverage ──
    # Handle range formats: "5x-10x", "5x-15x" → use midpoint
    # Handle single value: "10x" → use exact value
    # Both numbers must be small (≤3 digits) to avoid matching entry price ranges
    lev_range_match = re.search(r'\b(\d{1,3})[xX]\s*[-–]\s*(\d{1,3})[xX]', txt, re.IGNORECASE)
    if lev_range_match:
        lev_low = float(lev_range_match.group(1))
        lev_high = float(lev_range_match.group(2))
        leverage = (lev_low + lev_high) / 2  # use midpoint of range
    else:
        lev_match = re.search(r'\b(\d{1,3})\s*[xX]\b', txt)
        if not lev_match:
            lev_match = re.search(
                r'(?:LEV|LEVERAGE)\s*[:=@\-]?\s*(\d+)', txt, re.IGNORECASE
            )
        leverage = float(lev_match.group(1)) if lev_match else 2.0

    signal = Signal(
        symbol=symbol,
        side=side,
        entry_low=entry_low,
        entry_high=entry_high,
        take_profits=take_profits,
        stop_loss=stop_loss,
        leverage=leverage,
        raw_text=txt,
    )

    # Classify as swing or scalp
    signal.trade_type = classify_signal(signal)

    # Apply TP allocations: 1% at each early TP, rest on last
    signal.take_profits = apply_env_tp_pcts(
        signal.take_profits, signal.trade_type, signal.swing_keyword_match,
    )

    return signal


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def must_addr(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise SystemExit(f"Missing env var: {name}")
    return Web3.to_checksum_address(v)


def to_wei_decimal(amount: float, decimals: int) -> int:
    return round(amount * (10 ** decimals))


def from_wei_decimal(amount: int, decimals: int) -> float:
    return amount / (10 ** decimals)


def get_price_precision(symbol: str) -> int:
    """GMX V2 price precision = 10^(30 - index_token_decimals)."""
    token_dec = INDEX_TOKEN_DECIMALS.get(symbol, 18)
    return 10 ** (30 - token_dec)


def scale_price(price_usd: float, symbol: str) -> int:
    """Convert a USD price to GMX V2 on-chain format."""
    return int(price_usd * get_price_precision(symbol))


def wait_receipt(w3: Web3, tx_hash: str, timeout=180) -> dict:
    from web3.exceptions import TransactionNotFound
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = w3.eth.get_transaction_receipt(tx_hash)
            if r is not None:
                return r
        except TransactionNotFound:
            pass
        time.sleep(1.0)
    raise TimeoutError(f"Timed out waiting for receipt: {tx_hash}")


def get_fees(w3: Web3) -> dict:
    latest = w3.eth.get_block("latest")
    if "baseFeePerGas" in latest and latest["baseFeePerGas"] is not None:
        base = latest["baseFeePerGas"]
        priority = w3.to_wei(0.01, "gwei")
        max_fee = base * 2 + priority
        return {"maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority}
    return {"gasPrice": w3.eth.gas_price}


def build_tx(w3: Web3, from_addr: str, to_addr: str, data, value=0) -> dict:
    fees = get_fees(w3)
    nonce = w3.eth.get_transaction_count(from_addr, "pending")
    tx = {
        "from": from_addr,
        "to": to_addr,
        "nonce": nonce,
        "value": value,
        "chainId": w3.eth.chain_id,
    }
    tx.update(fees)
    est = w3.eth.estimate_gas({**tx, "data": data})
    gas = int(est * 1.25)
    tx["gas"] = gas
    tx["data"] = data
    return tx


def sign_send(w3: Web3, acct: Account, tx: dict, dry_run: bool) -> str:
    if dry_run:
        log.info(f"[DRY_RUN] Would send tx to {tx['to']} "
                 f"value={tx['value']} gas={tx.get('gas')} nonce={tx.get('nonce')}")
        return f"dry_run_{int(time.time())}"
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    log.info(f"Transaction sent: {tx_hash}")
    return tx_hash


def _get_order_keys_from_datastore(w3: Web3, wallet_addr: str) -> list:
    """Get active order keys directly from the DataStore EnumerableSet.

    This queries the same on-chain data that the Reader's getAccountOrders
    uses internally, so keys are guaranteed 1:1 with raw orders — no event
    scanning, no gas-estimate validation, no offset alignment needed.

    GMX V2 stores account order keys in:
      DataStore.getBytes32ValuesAt(
          keccak256(abi.encode(keccak256(abi.encode("ACCOUNT_ORDER_LIST")), account)),
          start, end
      )

    Returns list of bytes objects (each 32 bytes), in the same order as
    getAccountOrders returns order data.
    """
    datastore_addr = os.getenv("GMX_V2_DATASTORE", GMX_V2_DATASTORE)
    datastore = Web3.to_checksum_address(datastore_addr)
    wallet = Web3.to_checksum_address(wallet_addr)

    # Step 1: Compute the EnumerableSet key for this account's order list
    # Solidity: keccak256(abi.encode("ACCOUNT_ORDER_LIST"))
    aol_str = b"ACCOUNT_ORDER_LIST"
    aol_encoded = (
        (0x20).to_bytes(32, "big")           # offset to string data
        + len(aol_str).to_bytes(32, "big")   # string length
        + aol_str.ljust(32, b"\x00")         # string data padded to 32 bytes
    )
    ACCOUNT_ORDER_LIST = Web3.keccak(aol_encoded)

    # Solidity: keccak256(abi.encode(ACCOUNT_ORDER_LIST, account))
    wallet_bytes = bytes.fromhex(wallet[2:])
    set_key = Web3.keccak(
        bytes(ACCOUNT_ORDER_LIST) + b"\x00" * 12 + wallet_bytes
    )

    # Step 2: Query DataStore.getBytes32ValuesAt(setKey, 0, 50)
    fn_sig = Web3.keccak(text="getBytes32ValuesAt(bytes32,uint256,uint256)")[:4]
    call_data = (
        fn_sig
        + bytes(set_key)
        + (0).to_bytes(32, "big")
        + (50).to_bytes(32, "big")
    )

    try:
        result_hex = w3.eth.call({"to": datastore, "data": call_data})
    except Exception as e:
        log.warning(f"_get_order_keys_from_datastore: DataStore query failed: {e}")
        return []

    # Step 3: Decode bytes32[] return value
    data = bytes(result_hex)
    if len(data) < 64:
        return []

    arr_offset = int.from_bytes(data[0:32], "big")
    count = int.from_bytes(data[arr_offset:arr_offset + 32], "big")

    keys = []
    for i in range(count):
        start = arr_offset + 32 + i * 32
        if start + 32 > len(data):
            break
        keys.append(data[start:start + 32])

    log.debug(f"_get_order_keys_from_datastore: {len(keys)} active keys from DataStore")
    return keys


def _get_order_keys_from_events(w3: Web3, wallet_addr: str, lookback_blocks: int = 500000) -> list:
    """Get active order keys via EventEmitter OrderCreated logs (FALLBACK).

    The GMX V2 EventEmitter emits OrderCreated with:
      topic[2] = bytes32 order key
      topic[3] = account address (padded to 32 bytes)

    Validates each key with cancelOrder gas estimate to confirm it's still active.
    Returns list of bytes objects (each 32 bytes).
    """
    ORDER_CREATED_TOPIC = "0x468a25a7ba624ceea6e540ad6f49171b52495b648417ae91bca21676d8a24dc5"
    EXCHANGE_ROUTER = os.getenv("GMX_V2_EXCHANGE_ROUTER", "").strip()

    wallet = Web3.to_checksum_address(wallet_addr)
    # topic[3] = account padded to 32 bytes (0x + 24 zeros + 40-char address)
    wallet_topic = "0x" + "0" * 24 + wallet_addr.lower().replace("0x", "", 1)

    CANCEL_ABI = [{
        "name": "cancelOrder", "type": "function", "stateMutability": "payable",
        "inputs": [{"name": "key", "type": "bytes32"}],
        "outputs": [],
    }]
    exc = w3.eth.contract(
        address=Web3.to_checksum_address(EXCHANGE_ROUTER), abi=CANCEL_ABI
    )

    current = w3.eth.block_number
    try:
        logs = w3.eth.get_logs({
            "address": GMX_V2_EVENT_EMITTER,
            "fromBlock": max(0, current - lookback_blocks),
            "toBlock": current,
            "topics": [ORDER_CREATED_TOPIC, None, None, wallet_topic],
        })
    except Exception as e:
        log.warning(f"_get_order_keys_from_events: get_logs failed: {e}")
        return []

    # Deduplicate — topic[2] is the order key; keep latest block seen per key
    seen_keys: dict = {}
    for l in logs:
        topics = l.get("topics", [])
        if len(topics) >= 3:
            key_bytes = bytes(topics[2])
            blk = l["blockNumber"]
            if key_bytes not in seen_keys or blk > seen_keys[key_bytes]:
                seen_keys[key_bytes] = blk

    log.debug(f"_get_order_keys_from_events: {len(seen_keys)} unique keys from {len(logs)} logs")

    # Validate: only keep keys where cancelOrder gas estimate succeeds (order still active)
    active_keys = []
    for key_bytes in seen_keys:
        try:
            exc.functions.cancelOrder(key_bytes).estimate_gas({"from": wallet})
            active_keys.append(key_bytes)
        except Exception:
            pass  # order already executed/cancelled

    log.debug(f"_get_order_keys_from_events: {len(active_keys)} active (cancellable) keys")
    return active_keys


# ── GMX V2 EventLog2 ABI for PositionDecrease event parsing ──────────────
# The EventEmitter emits EventLog2 for PositionDecrease events.
# We decode the nested EventLogData struct to extract execution price.

_EVENT_LOG2_ABI = [{
    "anonymous": False,
    "inputs": [
        {"indexed": False, "name": "msgSender", "type": "address"},
        {"indexed": False, "name": "eventName", "type": "string"},
        {"indexed": True, "name": "eventNameHash", "type": "string"},
        {"indexed": True, "name": "topic1", "type": "bytes32"},
        {"indexed": True, "name": "topic2", "type": "bytes32"},
        {
            "components": [
                {"name": "addressItems", "type": "tuple", "components": [
                    {"name": "items", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "address"}]},
                    {"name": "arrayItems", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "address[]"}]}
                ]},
                {"name": "uintItems", "type": "tuple", "components": [
                    {"name": "items", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "uint256"}]},
                    {"name": "arrayItems", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "uint256[]"}]}
                ]},
                {"name": "intItems", "type": "tuple", "components": [
                    {"name": "items", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "int256"}]},
                    {"name": "arrayItems", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "int256[]"}]}
                ]},
                {"name": "boolItems", "type": "tuple", "components": [
                    {"name": "items", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "bool"}]},
                    {"name": "arrayItems", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "bool[]"}]}
                ]},
                {"name": "bytes32Items", "type": "tuple", "components": [
                    {"name": "items", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "bytes32"}]},
                    {"name": "arrayItems", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "bytes32[]"}]}
                ]},
                {"name": "bytesItems", "type": "tuple", "components": [
                    {"name": "items", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "bytes"}]},
                    {"name": "arrayItems", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "bytes[]"}]}
                ]},
                {"name": "stringItems", "type": "tuple", "components": [
                    {"name": "items", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "string"}]},
                    {"name": "arrayItems", "type": "tuple[]", "components": [
                        {"name": "key", "type": "string"}, {"name": "value", "type": "string[]"}]}
                ]},
            ],
            "indexed": False,
            "name": "eventData",
            "type": "tuple"
        }
    ],
    "name": "EventLog2",
    "type": "event"
}]

# Topic hashes
# EventLog2 selector — keccak256 of the full EventLog2 event signature with tuple types.
# This is the SAME hash used as ORDER_CREATED_TOPIC in _get_order_keys_from_events
# because both OrderCreated and PositionDecrease are emitted via EventLog2.
_EVENT_LOG2_TOPIC = "0x468a25a7ba624ceea6e540ad6f49171b52495b648417ae91bca21676d8a24dc5"
_POSITION_DECREASE_TOPIC = "0x84b670ed7b7ee8ccb350963a7dea39493daff6e7a43ab021a0e4ac2d652d359e"

# GMX V2 uses 1e30 precision for USD prices in events
_GMX_PRICE_PRECISION = 10 ** 30


def fetch_execution_price(
    w3: Web3,
    wallet_addr: str,
    market_addr: str,
    is_long: bool,
    lookback_blocks: int = 3000,
) -> Optional[float]:
    """Fetch the actual execution price from GMX V2 PositionDecrease events.

    Queries the EventEmitter for recent PositionDecrease EventLog2 events
    matching the wallet + market, then extracts 'executionPrice' from the
    decoded event data.

    Args:
        w3: Web3 instance
        wallet_addr: The wallet address that owned the position
        market_addr: The GMX market address for the position
        is_long: Whether the position was LONG (used to pick the right event
                 if multiple decrease events exist)
        lookback_blocks: Number of blocks to search back (default 300 ≈ 5 min on Arb)

    Returns:
        The execution price as a float (USD), or None if not found.
    """
    emitter_addr = Web3.to_checksum_address(GMX_V2_EVENT_EMITTER)

    # Pad addresses to 32 bytes for topic matching
    wallet_topic = "0x" + "0" * 24 + wallet_addr.lower().replace("0x", "")
    market_topic = "0x" + "0" * 24 + market_addr.lower().replace("0x", "")

    current = w3.eth.block_number

    try:
        raw_logs = w3.eth.get_logs({
            "address": emitter_addr,
            "fromBlock": max(0, current - lookback_blocks),
            "toBlock": current,
            "topics": [
                _EVENT_LOG2_TOPIC,            # EventLog2 selector
                _POSITION_DECREASE_TOPIC,     # keccak256("PositionDecrease")
                wallet_topic,                 # topic1 = account
                market_topic,                 # topic2 = market
            ],
        })
    except Exception as e:
        log.warning(f"fetch_execution_price: get_logs failed: {e}")
        return None

    if not raw_logs:
        log.debug(f"fetch_execution_price: no PositionDecrease events found "
                  f"in last {lookback_blocks} blocks for {wallet_addr[:10]}…")
        return None

    # Decode using web3 contract events API
    emitter = w3.eth.contract(address=emitter_addr, abi=_EVENT_LOG2_ABI)

    best_price = None
    best_block = 0

    for raw_log in reversed(raw_logs):  # most recent first
        try:
            decoded = emitter.events.EventLog2().process_log(raw_log)
            args = decoded["args"]

            # Extract uint items — executionPrice is stored here
            uint_items = args["eventData"][1]  # uintItems is index 1
            items = uint_items[0]  # .items (not .arrayItems)

            exec_price_raw = None
            size_delta_usd = None
            is_long_event = None

            for key, value in items:
                if key == "executionPrice":
                    exec_price_raw = value
                elif key == "sizeDeltaUsd":
                    size_delta_usd = value

            # Check bool items for isLong to match our position side
            bool_items = args["eventData"][3]  # boolItems is index 3
            for key, value in bool_items[0]:  # .items
                if key == "isLong":
                    is_long_event = value

            # Only use events matching our position direction
            if is_long_event is not None and is_long_event != is_long:
                continue

            if exec_price_raw and exec_price_raw > 0:
                blk = raw_log.get("blockNumber", 0)
                if blk >= best_block:
                    best_price = exec_price_raw / _GMX_PRICE_PRECISION
                    best_block = blk

        except Exception as e:
            log.debug(f"fetch_execution_price: failed to decode log: {e}")
            continue

    if best_price:
        log.info(f"fetch_execution_price: found execution price ${best_price:,.2f} "
                 f"at block {best_block}")
    else:
        log.debug("fetch_execution_price: could not extract executionPrice from events")

    return best_price


def _parse_orders_raw(w3: Web3, wallet_addr: str) -> list:
    """Parse order metadata from getAccountOrders raw bytes.

    getAccountOrders returns OrderInfo[] where:
      struct OrderInfo { bytes32 orderKey; Order.Props order; }

    Order.Props contains sub-structs: Addresses, Numbers, Flags.
    The ABI encoding flattens these into a complex byte layout.

    Confirmed struct word offsets per element (relative to element start es):
      word[0]  = pointer / struct header
      word[1]  = orderType  (uint8, stored as uint256)
      word[3]  = sizeDeltaUsd
      word[5]  = triggerPrice
      word[13] = isLong (bool at es+416)
      addresses sub-tuple at es+448:
        +0   = account
        +32  = receiver
        +64  = cancellationReceiver
        +96  = callbackContract
        +128 = market  (addresses[4] from es+448 base)
        +160 = initialCollateralToken

    Instead of relying solely on fixed offsets for the market address,
    we also search the element bytes for known market addresses as a
    fallback to handle any ABI encoding variations.
    """
    reader_addr    = os.getenv("GMX_V2_READER", GMX_V2_READER)
    datastore_addr = os.getenv("GMX_V2_DATASTORE", GMX_V2_DATASTORE)
    wallet         = Web3.to_checksum_address(wallet_addr)
    datastore      = Web3.to_checksum_address(datastore_addr)

    # ABI-encode the call manually
    fn_sig = Web3.keccak(text="getAccountOrders(address,address,uint256,uint256)")[:4]
    def enc_addr(a):
        return b"\x00" * 12 + bytes.fromhex(Web3.to_checksum_address(a)[2:])
    def enc_uint(n):
        return n.to_bytes(32, "big")

    calldata = (fn_sig
                + enc_addr(datastore)
                + enc_addr(wallet)
                + enc_uint(0)
                + enc_uint(50))

    try:
        result_hex = w3.eth.call({"to": Web3.to_checksum_address(reader_addr), "data": calldata})
    except Exception as e:
        log.warning(f"_parse_orders_raw: eth_call failed: {e}")
        return []

    data = bytes(result_hex)
    if len(data) < 64:
        return []

    # Top-level: offset to array, then array length
    arr_rel_offset = int.from_bytes(data[0:32], "big")
    count          = int.from_bytes(data[arr_rel_offset:arr_rel_offset + 32], "big")

    MARKET_SYM = {
        "0x47c031236e19d024b42f8ae6780e44a573170703": "BTC",
        "0x70d95587d40a2caf56bd97485ab3eec10bee6336": "ETH",
        "0x09400d9db990d5ed3f35d7be61dfaeb900af03c9": "SOL",
        "0x7f1fa204bb700853d36994da19f830b6ad18455c": "LINK",
        "0xc25cef6061cf5de5eb761b50e4743c1f5d7e5407": "ARB",
        "0x6853ea96ff216fab11d2d930ce3c508556a4bdc4": "DOGE",
        "0x7bbbf946883a5701350007320f525c5379b8178a": "AVAX",
    }
    # Pre-compute lowercased bytes for each known market to enable scanning
    MARKET_BYTES = {}
    for addr_hex in MARKET_SYM:
        MARKET_BYTES[bytes.fromhex(addr_hex[2:])] = addr_hex

    PRICE_PRECISION = {
        "BTC":  10 ** (30 - 8),
        "ETH":  10 ** (30 - 18),
        "SOL":  10 ** (30 - 9),
        "LINK": 10 ** (30 - 18),
        "ARB":  10 ** (30 - 18),
        "DOGE": 10 ** (30 - 8),
        "AVAX": 10 ** (30 - 18),
    }

    orders = []
    for i in range(count):
        elem_ptr_pos = arr_rel_offset + 32 + i * 32
        if elem_ptr_pos + 32 > len(data):
            break
        elem_rel_ptr = int.from_bytes(data[elem_ptr_pos:elem_ptr_pos + 32], "big")
        es = arr_rel_offset + 32 + elem_rel_ptr  # element start

        if es + 448 + 7 * 32 > len(data):
            break

        order_type  = int.from_bytes(data[es + 32:  es + 64],  "big")
        size_raw    = int.from_bytes(data[es + 96:  es + 128], "big")
        trigger_raw = int.from_bytes(data[es + 160: es + 192], "big")
        is_long_val = int.from_bytes(data[es + 416: es + 448], "big")

        # Find market address at known offsets first, then scan as fallback.
        # The market address is in the Addresses sub-struct. Due to ABI
        # encoding with dynamic arrays (swapPath[]), the exact offset can
        # vary, so we check multiple likely positions.
        market = None

        # Try specific offsets where the market address is expected:
        #   - es+448 + 128 = addresses[4] (original estimate)
        #   - es+448 + 0/32/64/96/128/160 = addresses[0..5]
        # We extract the 20-byte address from each 32-byte word and check
        # against known markets.
        CANDIDATE_OFFSETS = [
            es + 448 + 128,   # addresses[4] = market (primary)
            es + 448 + 160,   # addresses[5]
            es + 448 + 96,    # addresses[3]
            es + 448 + 64,    # addresses[2]
            es + 448 + 0,     # addresses[0]
            es + 448 + 32,    # addresses[1]
            es + 448 + 192,   # addresses[6]
            es + 480 + 128,   # shifted +32
            es + 512 + 128,   # shifted +64
        ]
        for off in CANDIDATE_OFFSETS:
            if off + 32 > len(data):
                continue
            addr_bytes = data[off + 12: off + 32]  # last 20 bytes of 32-byte word
            if addr_bytes in MARKET_BYTES:
                market = MARKET_BYTES[addr_bytes]
                break

        # Fallback: scan entire element for known market bytes
        if not market:
            elem_end = min(es + 800, len(data))
            elem_bytes = data[es:elem_end]
            # Collect ALL matches and pick the one that appears last
            # (market is deeper in the struct than account/receiver)
            last_match = None
            last_idx = -1
            for mkt_bytes, mkt_addr in MARKET_BYTES.items():
                idx = elem_bytes.find(mkt_bytes)
                if idx >= 0 and idx > last_idx:
                    last_match = mkt_addr
                    last_idx = idx
            if last_match:
                market = last_match

        sym  = MARKET_SYM.get(market.lower() if market else "", "???")
        prec = PRICE_PRECISION.get(sym, 10 ** 22)  # default to BTC precision as safest fallback
        trigger_price = trigger_raw / prec if trigger_raw > 0 else 0.0

        ORDER_TYPE_NAMES = {2: "MarketInc", 3: "LimitInc", 4: "MarketDec",
                            5: "TP", 6: "SL"}
        log.info(f"  order[{i}]: {sym} {ORDER_TYPE_NAMES.get(order_type, order_type)} "
                 f"trigger=${trigger_price:,.2f} size=${size_raw / (10**30):,.2f} "
                 f"market={market[:10] if market else '???'}...")

        orders.append({
            "market":        Web3.to_checksum_address(market) if market else "???",
            "symbol":        sym,
            "order_type":    order_type,
            "is_long":       bool(is_long_val),
            "size_usd":      size_raw / (10 ** 30),
            "trigger_price": trigger_price,
            "key_hex":       None,  # filled in by fetch_open_orders
        })

    log.info(f"_parse_orders_raw: decoded {len(orders)} orders")
    return orders


def fetch_open_orders(w3: Web3, wallet_addr: str) -> list:
    """Fetch all open orders for the wallet and return a list of dicts with decoded fields.

    Uses two sources from the same DataStore to ensure 1:1 key-to-order mapping:
      - _parse_orders_raw()              → order metadata (type, size, trigger, market)
      - _get_order_keys_from_datastore() → bytes32 order keys from EnumerableSet

    Both read from the DataStore's account order list, so keys and orders
    are in the same sequence — no offset alignment or event scanning needed.

    Falls back to the older event-based key detection if DataStore query fails.

    Each dict contains:
      market        — checksummed market address
      symbol        — str (BTC/ETH/SOL/…)
      order_type    — int (2=MarketIncrease, 3=LimitIncrease, 4=MarketDecrease,
                           5=LimitDecrease, 6=StopLossDecrease)
      is_long       — bool
      size_usd      — float (sizeDeltaUsd / 10^30)
      trigger_price — float in USD (0 for market orders)
      key_hex       — hex string of the order key (or None if not found)
    """
    raw_orders  = _parse_orders_raw(w3, wallet_addr)

    # Primary: get keys directly from DataStore (same source as Reader)
    active_keys = _get_order_keys_from_datastore(w3, wallet_addr)

    if active_keys and len(active_keys) == len(raw_orders):
        # Perfect 1:1 mapping — DataStore keys match raw orders
        return [
            {**o, "key_hex": active_keys[i].hex()}
            for i, o in enumerate(raw_orders)
        ]

    # Fallback: DataStore query failed or count mismatch — use event-based keys
    if not active_keys:
        log.warning("fetch_open_orders: DataStore key query returned empty, falling back to events")
        active_keys = _get_order_keys_from_events(w3, wallet_addr)
    else:
        log.warning(
            f"fetch_open_orders: DataStore returned {len(active_keys)} keys "
            f"for {len(raw_orders)} raw orders — falling back to events"
        )
        active_keys = _get_order_keys_from_events(w3, wallet_addr)

    # Legacy offset alignment for event-based fallback
    offset = max(0, len(raw_orders) - len(active_keys))
    if offset > 0:
        log.info(
            f"fetch_open_orders: {len(raw_orders)} raw orders, {len(active_keys)} active keys — "
            f"skipping {offset} stale order(s) at start"
        )

    result = []
    for i, o in enumerate(raw_orders):
        key_idx = i - offset
        if key_idx >= 0 and key_idx < len(active_keys):
            result.append({**o, "key_hex": active_keys[key_idx].hex()})
        else:
            log.warning(
                f"fetch_open_orders: order #{i} has no key_hex — "
                f"market={o.get('market', '?')[:10]}, "
                f"type={o.get('order_type_name', o.get('order_type', '?'))}, "
                f"trigger=${o.get('trigger_price', 0):,.2f}. "
                f"This order cannot be cancelled by the bot."
            )
            result.append({**o, "key_hex": None})

    return result


@retry_on_chain(max_retries=2, label="cancel_all_orders")
def cancel_all_orders(
    w3: Web3,
    acct: Account,
    exchange,
    dry_run: bool,
) -> int:
    """Cancel ALL open orders for this wallet across every market.

    Uses EventEmitter logs to find active order keys (topic[2]) — this bypasses
    the broken getAccountOrders ABI which does not return bytes32 keys.

    Cancels LimitIncrease (3), LimitDecrease (5), and StopLossDecrease (6).
    MarketIncrease (2) and MarketDecrease (4) cannot be cancelled once submitted.

    Returns the number of orders cancelled.
    """
    wallet = Web3.to_checksum_address(acct.address)

    active_keys = _get_order_keys_from_events(w3, acct.address)
    if not active_keys:
        log.info("cancel_all_orders: no active orders found.")
        return 0

    cancelled = 0
    for key_bytes in active_keys:
        key_hex = key_bytes.hex()
        log.info(f"Cancelling order key=0x{key_hex[:16]}...")

        if dry_run:
            log.info(f"  [DRY_RUN] Would cancel order 0x{key_hex[:16]}")
            cancelled += 1
            continue

        try:
            data = exchange.encode_abi("cancelOrder", [key_bytes])
            tx = build_tx(w3, wallet, exchange.address, data, value=0)
            txh = sign_send(w3, acct, tx, dry_run=False)
            receipt = wait_receipt(w3, txh)
            if receipt.get("status") == 1:
                log.info(f"  Cancelled: {txh}")
                cancelled += 1
            else:
                log.warning(f"  Cancel tx reverted: {txh}")
        except Exception as e:
            log.warning(f"  Failed to cancel order 0x{key_hex[:16]}: {e}")

    log.info(f"cancel_all_orders: cancelled={cancelled} of {len(active_keys)} active orders.")
    return cancelled


@retry_on_chain(max_retries=2, label="cancel_orders_for_market")
def cancel_orders_for_market(
    w3: Web3,
    acct: Account,
    exchange,
    market_addr: str,
    dry_run: bool,
) -> int:
    """Fetch all open orders for this wallet and cancel any that belong to market_addr.

    Uses EventEmitter logs for keys + raw byte decode for market metadata.
    Cancels LimitIncrease (3), LimitDecrease (5), and StopLossDecrease (6) orders.
    MarketIncrease (2) and MarketDecrease (4) cannot be cancelled once submitted.

    Returns the number of orders cancelled.
    """
    wallet        = Web3.to_checksum_address(acct.address)
    target_lower  = market_addr.lower()

    # Get active order keys + metadata merged by index
    orders = fetch_open_orders(w3, acct.address)

    # Resolve target symbol for logging
    _SYM_LOOKUP = {
        "0x47c031236e19d024b42f8ae6780e44a573170703": "BTC",
        "0x70d95587d40a2caf56bd97485ab3eec10bee6336": "ETH",
        "0x09400d9db990d5ed3f35d7be61dfaeb900af03c9": "SOL",
        "0x7f1fa204bb700853d36994da19f830b6ad18455c": "LINK",
    }
    target_sym = _SYM_LOOKUP.get(target_lower, target_lower[:10])
    log.info(f"cancel_orders_for_market: target={target_sym} ({target_lower[:10]}...), "
             f"found {len(orders)} total orders")
    for o in orders:
        log.info(f"  order: {o['symbol']} type={o['order_type']} "
                 f"market={o['market'][:10]}... trigger=${o['trigger_price']:,.2f}")

    CANCELLABLE = {
        ORDER_TYPE_LIMIT_INCREASE,
        ORDER_TYPE_LIMIT_DECREASE,
        ORDER_TYPE_STOP_LOSS_DECREASE,
    }

    cancelled = 0
    for o in orders:
        if o["market"].lower() != target_lower:
            continue
        if o["order_type"] not in CANCELLABLE:
            continue
        if not o["key_hex"]:
            log.warning(f"  No key for order type={o['order_type']} market={market_addr[:10]}...")
            continue

        key_bytes = bytes.fromhex(o["key_hex"])
        log.info(f"Cancelling stale order type={o['order_type']} key=0x{o['key_hex'][:16]}...")

        if dry_run:
            log.info(f"  [DRY_RUN] Would cancel order 0x{o['key_hex'][:16]}")
            cancelled += 1
            continue

        try:
            data = exchange.encode_abi("cancelOrder", [key_bytes])
            tx = build_tx(w3, wallet, exchange.address, data, value=0)
            txh = sign_send(w3, acct, tx, dry_run=False)
            receipt = wait_receipt(w3, txh)
            if receipt.get("status") == 1:
                log.info(f"  Cancelled: {txh}")
                cancelled += 1
            else:
                log.warning(f"  Cancel tx reverted: {txh}")
        except Exception as e:
            log.warning(f"  Failed to cancel order 0x{o['key_hex'][:16]}: {e}")

    if cancelled:
        log.info(f"Cancelled {cancelled} order(s) for {target_sym}")
    else:
        log.info(f"No orders to cancel for {target_sym}")

    return cancelled


# ── Chainlink on-chain price feeds (Arbitrum) ──
# These are the same Chainlink feeds that underpin GMX V2's oracle.
# Using on-chain reads avoids CoinGecko rate limits and price discrepancies.
CHAINLINK_FEEDS = {
    "BTC":  "0x6ce185860a4963106506C203335A2910413708e9",
    "ETH":  "0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612",
    "SOL":  "0x24ceA4b8ce57cdA5058b924B9B9987992450590c",
    "LINK": "0x86E53CF1B870786351Da77A57575e79CB55812CB",
}
CHAINLINK_ABI = [
    {"name": "latestRoundData", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [{"name": "roundId", "type": "uint80"},
                 {"name": "answer", "type": "int256"},
                 {"name": "startedAt", "type": "uint256"},
                 {"name": "updatedAt", "type": "uint256"},
                 {"name": "answeredInRound", "type": "uint80"}]},
    {"name": "getRoundData", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "_roundId", "type": "uint80"}],
     "outputs": [{"name": "roundId", "type": "uint80"},
                 {"name": "answer", "type": "int256"},
                 {"name": "startedAt", "type": "uint256"},
                 {"name": "updatedAt", "type": "uint256"},
                 {"name": "answeredInRound", "type": "uint80"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
]

# Cache decimals per feed so we don't make an extra RPC call every time
_chainlink_decimals_cache: Dict[str, int] = {}


def fetch_current_price(symbol: str, w3=None) -> float:
    """Fetch current price from Chainlink on-chain feeds (primary).
    Falls back to CoinGecko only if Chainlink is unavailable."""

    symbol_upper = symbol.upper()

    # ── Primary: Chainlink on-chain price feed ──
    feed_addr = CHAINLINK_FEEDS.get(symbol_upper)
    if feed_addr and w3:
        try:
            feed = w3.eth.contract(
                address=Web3.to_checksum_address(feed_addr), abi=CHAINLINK_ABI
            )
            result = feed.functions.latestRoundData().call()

            if symbol_upper not in _chainlink_decimals_cache:
                _chainlink_decimals_cache[symbol_upper] = feed.functions.decimals().call()
            decimals = _chainlink_decimals_cache[symbol_upper]

            price = result[1] / (10 ** decimals)
            if price > 0:
                log.debug(f"Chainlink price for {symbol}: ${price:,.2f}")
                return float(price)
        except Exception as e:
            log.warning(f"Chainlink price feed failed for {symbol}: {e}")

    # ── Fallback: CoinGecko (only if Chainlink unavailable) ──
    coin_id = COINGECKO_IDS.get(symbol_upper)
    if not coin_id:
        raise ValueError(f"Unknown symbol '{symbol}'. Supported: "
                         f"{', '.join(COINGECKO_IDS.keys())}")
    try:
        url = (f"https://api.coingecko.com/api/v3/simple/price"
               f"?ids={coin_id}&vs_currencies=usd")
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        price = data[coin_id]["usd"]
        log.info(f"CoinGecko fallback price for {symbol}: ${price:,.2f}")
        return float(price)
    except Exception as e:
        log.warning(f"CoinGecko fallback also failed for {symbol}: {e}")

    raise RuntimeError(f"Could not fetch price for {symbol} from Chainlink or CoinGecko")


def fetch_price_touched_in_window(
    symbol: str,
    target_price: float,
    is_long: bool,
    w3=None,
    window_seconds: int = 600,
    tolerance_pct: float = 0.003,
) -> bool:
    """Check if Chainlink price touched a target within the last `window_seconds`.

    Walks backward through Chainlink round data to see if price ever reached
    the target level. Used for TP hit verification when current price has bounced.

    Args:
        symbol: Token symbol (BTC, ETH, SOL, LINK)
        target_price: The TP price to check
        is_long: True for LONG (price must go UP to hit TP), False for SHORT
        w3: Web3 instance
        window_seconds: How far back to look (default 10 minutes)
        tolerance_pct: Price tolerance (default 0.3%)

    Returns:
        True if any historical round's price reached the target.
    """
    feed_addr = CHAINLINK_FEEDS.get(symbol.upper())
    if not feed_addr or not w3:
        return False

    try:
        feed = w3.eth.contract(
            address=Web3.to_checksum_address(feed_addr), abi=CHAINLINK_ABI
        )

        if symbol.upper() not in _chainlink_decimals_cache:
            _chainlink_decimals_cache[symbol.upper()] = feed.functions.decimals().call()
        decimals = _chainlink_decimals_cache[symbol.upper()]

        latest = feed.functions.latestRoundData().call()
        current_round_id = latest[0]
        cutoff_time = int(time.time()) - window_seconds

        tol = target_price * tolerance_pct

        # Walk backward through rounds (max 50 to limit RPC calls)
        for i in range(50):
            round_id = current_round_id - i
            if round_id <= 0:
                break
            try:
                data = feed.functions.getRoundData(round_id).call()
                price = data[1] / (10 ** decimals)
                updated_at = data[3]

                if updated_at < cutoff_time:
                    break  # outside our window

                if is_long:
                    if price >= target_price - tol:
                        log.info(
                            f"Historical price confirmation: {symbol} ${price:,.2f} "
                            f"reached TP ${target_price:,.2f} at round {round_id}"
                        )
                        return True
                else:
                    if price <= target_price + tol:
                        log.info(
                            f"Historical price confirmation: {symbol} ${price:,.2f} "
                            f"reached TP ${target_price:,.2f} at round {round_id}"
                        )
                        return True
            except Exception:
                continue

        return False
    except Exception as e:
        log.warning(f"Historical price check failed for {symbol}: {e}")
        return False


def fetch_positions(w3: Web3, wallet: str) -> list:
    """Fetch and display all open GMX V2 positions for the wallet."""
    reader_addr = os.getenv("GMX_V2_READER", GMX_V2_READER)
    datastore_addr = os.getenv("GMX_V2_DATASTORE", GMX_V2_DATASTORE)
    reader = w3.eth.contract(
        address=Web3.to_checksum_address(reader_addr), abi=READER_ABI,
    )
    datastore = Web3.to_checksum_address(datastore_addr)
    positions = reader.functions.getAccountPositions(
        datastore, wallet, 0, 100
    ).call()
    if not positions:
        log.info("No open positions found.")
        return []
    log.info(f"Found {len(positions)} open position(s):")
    for i, pos in enumerate(positions):
        addresses, numbers, flags = pos[0], pos[1], pos[2]
        market, collateral_token, is_long = addresses[1], addresses[2], flags[0]
        size_usd = numbers[0] / (10 ** 30)
        size_tokens_raw, collateral_raw = numbers[1], numbers[2]
        try:
            col_contract = w3.eth.contract(address=collateral_token, abi=ERC20_ABI)
            col_dec = col_contract.functions.decimals().call()
            col_sym = col_contract.functions.symbol().call()
        except Exception:
            col_dec, col_sym = 6, "?"
        collateral_amount = collateral_raw / (10 ** col_dec)
        leverage = size_usd / collateral_amount if collateral_amount > 0 else 0
        # Look up token decimals from market address for accurate entry price
        _MARKET_DECIMALS = {
            "0x47c031236e19d024b42f8ae6780e44a573170703": 8,   # BTC
            "0x70d95587d40a2caf56bd97485ab3eec10bee6336": 18,  # ETH
            "0x09400d9db990d5ed3f35d7be61dfaeb900af03c9": 9,   # SOL
            "0x7f1fa204bb700853d36994da19f830b6ad18455c": 18,  # LINK
            "0xc25cef6061cf5de5eb761b50e4743c1f5d7e5407": 18,  # ARB
            "0x6853ea96ff216fab11d2d930ce3c508556a4bdc4": 8,   # DOGE
            "0x7bbbf946883a5701350007320f525c5379b8178a": 18,  # AVAX
        }
        if size_tokens_raw > 0:
            token_dec = _MARKET_DECIMALS.get(market.lower(), 18)
            entry_price = size_usd / (size_tokens_raw / (10 ** token_dec))
        else:
            entry_price = 0
        side = "LONG" if is_long else "SHORT"
        log.info(
            f"  Position #{i+1}: {market} {side} "
            f"${size_usd:,.2f} {collateral_amount:,.2f} {col_sym} "
            f"{leverage:.1f}x entry=${entry_price:,.2f}"
        )
    return positions


def ensure_allowance(w3, acct, token, owner, spender, required_base,
                     dry_run, approve_max):
    allowance = token.functions.allowance(owner, spender).call()
    decimals = token.functions.decimals().call()
    try:
        sym = token.functions.symbol().call()
    except Exception:
        sym = token.address
    log.info(
        f"Allowance check: token={sym} "
        f"allowance={from_wei_decimal(allowance, decimals)} "
        f"required={from_wei_decimal(required_base, decimals)} "
        f"spender={spender}"
    )
    if allowance >= required_base:
        return
    approve_amount = MAX_UINT256 if approve_max else required_base * 2
    data = token.encode_abi("approve", [spender, approve_amount])
    tx = build_tx(w3, owner, token.address, data, value=0)
    log.info("Sending approve...")
    txh = sign_send(w3, acct, tx, dry_run)
    if dry_run:
        return
    r = wait_receipt(w3, txh)
    if r.get("status") != 1:
        raise RuntimeError(f"Approve failed: {txh}")
    log.info(f"Approve confirmed: tx_hash={txh}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Order builders
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _build_order_params(
    wallet: str,
    market: str,
    collateral_token: str,
    size_delta_usd: int,
    initial_collateral_delta: int,
    trigger_price: int,
    acceptable_price: int,
    execution_fee: int,
    order_type: int,
    is_long: bool,
    auto_cancel: bool = False,
) -> tuple:
    """Build the createOrder params tuple."""
    zero = Web3.to_checksum_address(ZERO_ADDR)
    wallet = Web3.to_checksum_address(wallet)
    market = Web3.to_checksum_address(market)
    collateral_token = Web3.to_checksum_address(collateral_token)
    return (
        # addresses
        (wallet, wallet, zero, zero, market, collateral_token, []),
        # numbers
        (
            size_delta_usd,
            initial_collateral_delta,
            trigger_price,
            acceptable_price,
            execution_fee,
            0,  # callbackGasLimit
            0,  # minOutputAmount
            0,  # validFromTime
        ),
        order_type,
        DECREASE_SWAP_TYPE_NO_SWAP,
        is_long,
        False,  # shouldUnwrapNativeToken
        auto_cancel,
        b"\x00" * 32,  # referralCode
        [],  # dataList
    )


@retry_on_chain(max_retries=3, label="create_market_increase_order")
def create_market_increase_order(
    w3: Web3,
    acct: Account,
    exchange,
    wallet: str,
    market: str,
    collateral_token: str,
    order_vault: str,
    size_usd: float,
    collateral_usd: float,
    entry_price: float,
    symbol: str,
    is_long: bool,
    slippage_bps: int,
    execution_fee: int,
    dry_run: bool,
) -> str:
    """Create a MarketIncrease order (open position)."""
    # Ensure all addresses are checksummed
    wallet = Web3.to_checksum_address(wallet)
    market = Web3.to_checksum_address(market)
    collateral_token = Web3.to_checksum_address(collateral_token)
    order_vault = Web3.to_checksum_address(order_vault)

    token = w3.eth.contract(
        address=collateral_token, abi=ERC20_ABI
    )
    dec = token.functions.decimals().call()
    collateral_amount_base = to_wei_decimal(collateral_usd, dec)

    # Approve collateral to Router
    router = Web3.to_checksum_address(
        os.getenv("GMX_V2_ROUTER", "0x7452c558d45f8afC8c83dAe62C3f8A5BE19c71f6")
    )
    approve_max = os.getenv("APPROVE_MAX", "true").lower() == "true"
    ensure_allowance(w3, acct, token, wallet, router,
                     collateral_amount_base, dry_run, approve_max)

    # Acceptable price with slippage
    slip = slippage_bps / 10_000.0
    if is_long:
        acceptable = entry_price * (1 + slip)
    else:
        acceptable = entry_price * (1 - slip)

    size_delta_usd = int(size_usd * (10 ** 30))
    acceptable_price_scaled = scale_price(acceptable, symbol)

    params = _build_order_params(
        wallet=wallet,
        market=market,
        collateral_token=collateral_token,
        size_delta_usd=size_delta_usd,
        initial_collateral_delta=0,
        trigger_price=0,
        acceptable_price=acceptable_price_scaled,
        execution_fee=execution_fee,
        order_type=ORDER_TYPE_MARKET_INCREASE,
        is_long=is_long,
    )

    # Multicall: sendWnt + sendTokens + createOrder
    data1 = exchange.encode_abi("sendWnt", [order_vault, execution_fee])
    data2 = exchange.encode_abi(
        "sendTokens", [collateral_token, order_vault, collateral_amount_base]
    )
    data3 = exchange.encode_abi("createOrder", [params])
    call_data = exchange.encode_abi("multicall", [[data1, data2, data3]])

    tx = build_tx(w3, wallet, exchange.address, call_data, value=execution_fee)
    txh = sign_send(w3, acct, tx, dry_run)

    if not dry_run and not txh.startswith("dry_run"):
        receipt = wait_receipt(w3, txh)
        if receipt.get("status") != 1:
            raise RuntimeError(f"MarketIncrease tx reverted: {txh}")

    log.info(f"MarketIncrease order created: {txh}")
    return txh


@retry_on_chain(max_retries=3, label="create_limit_increase_order")
def create_limit_increase_order(
    w3: Web3,
    acct: Account,
    exchange,
    wallet: str,
    market: str,
    collateral_token: str,
    order_vault: str,
    size_usd: float,
    collateral_usd: float,
    trigger_price: float,
    symbol: str,
    is_long: bool,
    slippage_bps: int,
    execution_fee: int,
    dry_run: bool,
) -> str:
    """Create a LimitIncrease order (open position at a specific price).

    GMX V2 LimitIncrease trigger logic:
      LONG:  triggers when oracle price <= triggerPrice  (buy the dip)
      SHORT: triggers when oracle price >= triggerPrice  (sell the rally)

    acceptablePrice = worst entry price you'll accept after slippage.
      LONG:  acceptablePrice = triggerPrice * (1 + slippage)   (buying, accept higher)
      SHORT: acceptablePrice = triggerPrice * (1 - slippage)   (selling, accept lower)
    """
    # Ensure all addresses are checksummed
    wallet = Web3.to_checksum_address(wallet)
    market = Web3.to_checksum_address(market)
    collateral_token = Web3.to_checksum_address(collateral_token)
    order_vault = Web3.to_checksum_address(order_vault)

    token = w3.eth.contract(address=collateral_token, abi=ERC20_ABI)
    dec = token.functions.decimals().call()
    collateral_amount_base = to_wei_decimal(collateral_usd, dec)

    # Approve collateral to Router
    router = Web3.to_checksum_address(
        os.getenv("GMX_V2_ROUTER", "0x7452c558d45f8afC8c83dAe62C3f8A5BE19c71f6")
    )
    approve_max = os.getenv("APPROVE_MAX", "true").lower() == "true"
    ensure_allowance(w3, acct, token, wallet, router,
                     collateral_amount_base, dry_run, approve_max)

    # Acceptable price with slippage
    slip = slippage_bps / 10_000.0
    if is_long:
        acceptable = trigger_price * (1 + slip)
    else:
        acceptable = trigger_price * (1 - slip)

    size_delta_usd = int(size_usd * (10 ** 30))
    trigger_price_scaled = scale_price(trigger_price, symbol)
    acceptable_price_scaled = scale_price(acceptable, symbol)

    params = _build_order_params(
        wallet=wallet,
        market=market,
        collateral_token=collateral_token,
        size_delta_usd=size_delta_usd,
        initial_collateral_delta=0,
        trigger_price=trigger_price_scaled,
        acceptable_price=acceptable_price_scaled,
        execution_fee=execution_fee,
        order_type=ORDER_TYPE_LIMIT_INCREASE,
        is_long=is_long,
    )

    # Multicall: sendWnt + sendTokens + createOrder
    data1 = exchange.encode_abi("sendWnt", [order_vault, execution_fee])
    data2 = exchange.encode_abi(
        "sendTokens", [collateral_token, order_vault, collateral_amount_base]
    )
    data3 = exchange.encode_abi("createOrder", [params])
    call_data = exchange.encode_abi("multicall", [[data1, data2, data3]])

    tx = build_tx(w3, wallet, exchange.address, call_data, value=execution_fee)
    txh = sign_send(w3, acct, tx, dry_run)

    if not dry_run and not txh.startswith("dry_run"):
        receipt = wait_receipt(w3, txh)
        if receipt.get("status") != 1:
            raise RuntimeError(f"LimitIncrease tx reverted: {txh}")

    log.info(f"LimitIncrease order created @ ${trigger_price:,.2f}: {txh}")
    return txh


@retry_on_chain(max_retries=3, label="create_tp_order")
def create_tp_order(
    w3: Web3,
    acct: Account,
    exchange,
    wallet: str,
    market: str,
    collateral_token: str,
    order_vault: str,
    tp: TakeProfit,
    total_size_usd: float,
    collateral_usd: float,
    symbol: str,
    is_long: bool,
    slippage_bps: int,
    execution_fee: int,
    dry_run: bool,
) -> str:
    """Create a LimitDecrease (take-profit) order.

    GMX V2 LimitDecrease trigger logic:
      LONG:  triggers when oracle price >= triggerPrice  (price rose to target)
      SHORT: triggers when oracle price <= triggerPrice  (price fell to target)

    acceptablePrice = worst price you'll accept after slippage.
      LONG TP:  acceptablePrice = triggerPrice * (1 - slippage)   (selling, accept lower)
      SHORT TP: acceptablePrice = triggerPrice * (1 + slippage)   (buying, accept higher)

    collateral_usd: total position collateral. Used to withdraw proportional
      collateral on partial close so leverage stays constant.
    """
    # Ensure all addresses are checksummed
    wallet = Web3.to_checksum_address(wallet)
    market = Web3.to_checksum_address(market)
    collateral_token = Web3.to_checksum_address(collateral_token)
    order_vault = Web3.to_checksum_address(order_vault)

    slip = slippage_bps / 10_000.0

    # Size for this TP level
    close_size_usd = total_size_usd * tp.close_pct
    size_delta_usd = int(close_size_usd * (10 ** 30))

    # Withdraw proportional collateral so leverage stays constant on the
    # remaining position.  Without this, GMX adds profit to collateral and
    # leverage drops (e.g. 10x → 1x).
    token = w3.eth.contract(address=collateral_token, abi=ERC20_ABI)
    try:
        col_decimals = token.functions.decimals().call()
    except Exception:
        col_decimals = 6  # USDC default
    close_collateral = collateral_usd * tp.close_pct
    collateral_delta = int(close_collateral * (10 ** col_decimals))

    trigger_price_scaled = scale_price(tp.price, symbol)

    if is_long:
        acceptable = tp.price * (1 - slip)
    else:
        acceptable = tp.price * (1 + slip)
    acceptable_price_scaled = scale_price(acceptable, symbol)

    params = _build_order_params(
        wallet=wallet,
        market=market,
        collateral_token=collateral_token,
        size_delta_usd=size_delta_usd,
        initial_collateral_delta=collateral_delta,
        trigger_price=trigger_price_scaled,
        acceptable_price=acceptable_price_scaled,
        execution_fee=execution_fee,
        order_type=ORDER_TYPE_LIMIT_DECREASE,
        is_long=is_long,
        auto_cancel=True,  # cancel TP if position is closed by SL
    )

    # Multicall: sendWnt + createOrder (no collateral deposit for decrease)
    data1 = exchange.encode_abi("sendWnt", [order_vault, execution_fee])
    data2 = exchange.encode_abi("createOrder", [params])
    call_data = exchange.encode_abi("multicall", [[data1, data2]])

    tx = build_tx(w3, wallet, exchange.address, call_data, value=execution_fee)
    txh = sign_send(w3, acct, tx, dry_run)

    if not dry_run and not txh.startswith("dry_run"):
        receipt = wait_receipt(w3, txh)
        if receipt.get("status") != 1:
            raise RuntimeError(f"TP order tx reverted: {txh}")

    log.info(f"  TP @ ${tp.price:,.2f} ({tp.close_pct:.0%} close) → {txh}")
    return txh


@retry_on_chain(max_retries=3, label="create_sl_order")
def create_sl_order(
    w3: Web3,
    acct: Account,
    exchange,
    wallet: str,
    market: str,
    collateral_token: str,
    order_vault: str,
    sl_price: float,
    size_usd: float,
    symbol: str,
    is_long: bool,
    slippage_bps: int,
    execution_fee: int,
    dry_run: bool,
) -> str:
    """Create a StopLossDecrease order.

    GMX V2 StopLossDecrease trigger logic:
      LONG:  triggers when oracle price <= triggerPrice  (price fell to SL)
      SHORT: triggers when oracle price >= triggerPrice  (price rose to SL)

    acceptablePrice = worst price you'll accept after slippage.
      LONG SL:  acceptablePrice = triggerPrice * (1 - slippage)   (selling, accept lower)
      SHORT SL: acceptablePrice = triggerPrice * (1 + slippage)   (buying, accept higher)

    We set acceptablePrice with generous slippage (5x normal) to avoid
    the SL order being cancelled during volatile conditions.
    """
    # Ensure all addresses are checksummed
    wallet = Web3.to_checksum_address(wallet)
    market = Web3.to_checksum_address(market)
    collateral_token = Web3.to_checksum_address(collateral_token)
    order_vault = Web3.to_checksum_address(order_vault)

    # Use wider slippage for SL to ensure execution
    sl_slip = max(slippage_bps, 300) / 10_000.0  # at least 3% for SL to ensure execution

    size_delta_usd = int(size_usd * (10 ** 30))
    trigger_price_scaled = scale_price(sl_price, symbol)

    if is_long:
        acceptable = sl_price * (1 - sl_slip)
    else:
        acceptable = sl_price * (1 + sl_slip)
    acceptable_price_scaled = scale_price(acceptable, symbol)

    params = _build_order_params(
        wallet=wallet,
        market=market,
        collateral_token=collateral_token,
        size_delta_usd=size_delta_usd,
        initial_collateral_delta=0,
        trigger_price=trigger_price_scaled,
        acceptable_price=acceptable_price_scaled,
        execution_fee=execution_fee,
        order_type=ORDER_TYPE_STOP_LOSS_DECREASE,
        is_long=is_long,
        auto_cancel=False,  # SL must NOT auto-cancel — it is the safety net
    )

    # Multicall: sendWnt + createOrder
    data1 = exchange.encode_abi("sendWnt", [order_vault, execution_fee])
    data2 = exchange.encode_abi("createOrder", [params])
    call_data = exchange.encode_abi("multicall", [[data1, data2]])

    tx = build_tx(w3, wallet, exchange.address, call_data, value=execution_fee)
    txh = sign_send(w3, acct, tx, dry_run)

    if not dry_run and not txh.startswith("dry_run"):
        receipt = wait_receipt(w3, txh)
        if receipt.get("status") != 1:
            raise RuntimeError(f"SL order tx reverted: {txh}")

    log.info(f"  SL @ ${sl_price:,.2f} (100% close) → {txh}")
    return txh


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Full execution flow
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def execute_signal(
    w3: Web3,
    acct: Account,
    signal: Signal,
    exchange_router: str,
    order_vault: str,
    market: str,
    collateral_token: str,
    size_usd: float,
    collateral_usd: float,
    execution_fee: int,
    slippage_bps: int,
    dry_run: bool,
) -> Dict[str, Any]:
    """Execute a full signal: open position + place TP/SL orders.

    collateral_usd is passed from the caller (25% of combined portfolio).
    size_usd = collateral_usd * leverage.

    Returns dict with tx hashes for each order.
    """
    wallet = Web3.to_checksum_address(acct.address)
    market = Web3.to_checksum_address(market)
    collateral_token = Web3.to_checksum_address(collateral_token)
    order_vault = Web3.to_checksum_address(order_vault)
    exchange = w3.eth.contract(
        address=Web3.to_checksum_address(exchange_router),
        abi=EXCHANGE_ROUTER_ABI,
    )

    log.info(f"Collateral: ${collateral_usd:.2f}, Size: ${size_usd:.2f}, Leverage: {signal.leverage}x")

    # Fetch current price for entry validation
    current_price = fetch_current_price(signal.symbol, w3=w3)
    entry_price = signal.entry_mid

    # Always use MarketIncrease (immediate fill at current price).
    # This avoids limit-order edge cases where unfilled orders get
    # misinterpreted as closed positions by the monitoring loop.
    use_limit = False
    entry_price = current_price
    log.info(f"Current price ${current_price:,.2f} (signal entry: "
             f"[${signal.entry_low:,.2f} - ${signal.entry_high:,.2f}]) → MARKET order")

    results = {"open": None, "tp": [], "sl": None, "order_type": "limit" if use_limit else "market"}

    # ── Pre-step: Cancel any stale open orders for this market ──
    # This prevents duplicate TP/SL orders if the previous position was closed
    # by on-chain keepers (SL/TP hit) without the bot knowing.
    log.info(f"\n{'='*60}")
    log.info("PRE-STEP: Cancelling any stale orders for this market")
    log.info(f"{'='*60}")
    try:
        cancel_orders_for_market(w3, acct, exchange, market, dry_run)
    except Exception as e:
        log.warning(f"Order cancellation failed (continuing anyway): {e}")

    # ── Step 1: Open position ──
    order_type_str = "LIMIT" if use_limit else "MARKET"
    log.info(f"{'='*60}")
    log.info(f"STEP 1: Opening {signal.side} position ({order_type_str})")
    log.info(f"  Symbol:     {signal.symbol}")
    log.info(f"  Size:       ${size_usd:.2f}")
    log.info(f"  Collateral: ${collateral_usd:.2f}")
    log.info(f"  Leverage:   {signal.leverage}x")
    log.info(f"  Entry:      ${entry_price:,.2f}")
    log.info(f"  Order type: {order_type_str}")
    log.info(f"{'='*60}")

    if use_limit:
        results["open"] = create_limit_increase_order(
            w3=w3, acct=acct, exchange=exchange, wallet=wallet,
            market=market, collateral_token=collateral_token,
            order_vault=order_vault, size_usd=size_usd,
            collateral_usd=collateral_usd, trigger_price=entry_price,
            symbol=signal.symbol, is_long=signal.is_long,
            slippage_bps=slippage_bps, execution_fee=execution_fee,
            dry_run=dry_run,
        )
    else:
        results["open"] = create_market_increase_order(
            w3=w3, acct=acct, exchange=exchange, wallet=wallet,
            market=market, collateral_token=collateral_token,
            order_vault=order_vault, size_usd=size_usd,
            collateral_usd=collateral_usd, entry_price=entry_price,
            symbol=signal.symbol, is_long=signal.is_long,
            slippage_bps=slippage_bps, execution_fee=execution_fee,
            dry_run=dry_run,
        )

    # Wait for position to be opened by keepers before placing TP/SL
    # For limit orders, we still place TP/SL immediately (they auto-cancel if position doesn't fill)
    if not dry_run:
        wait_time = 10 if not use_limit else 2
        log.info(f"Waiting {wait_time}s for keeper to execute open order...")
        time.sleep(wait_time)

    # ── Step 2: Place Take Profit orders (LimitDecrease) ──
    if signal.take_profits:
        log.info(f"\n{'='*60}")
        log.info(f"STEP 2: Placing {len(signal.take_profits)} Take Profit order(s)")
        log.info(f"{'='*60}")

        for i, tp in enumerate(signal.take_profits):
            log.info(f"\n  TP{i+1}: ${tp.price:,.2f} "
                     f"({tp.close_pct:.0%} = ${size_usd * tp.close_pct:.2f})")
            try:
                txh = create_tp_order(
                    w3=w3, acct=acct, exchange=exchange, wallet=wallet,
                    market=market, collateral_token=collateral_token,
                    order_vault=order_vault, tp=tp, total_size_usd=size_usd,
                    collateral_usd=collateral_usd,
                    symbol=signal.symbol, is_long=signal.is_long,
                    slippage_bps=slippage_bps, execution_fee=execution_fee,
                    dry_run=dry_run,
                )
                results["tp"].append({"price": tp.price, "pct": tp.close_pct,
                                      "tx": txh})
                if not dry_run:
                    time.sleep(2)  # small delay between orders
            except Exception as e:
                log.error(f"  TP{i+1} failed: {e}")
                results["tp"].append({"price": tp.price, "pct": tp.close_pct,
                                      "tx": None, "error": str(e)})

    # ── Step 3: Place Stop Loss order (StopLossDecrease) ──
    log.info(f"\n{'='*60}")
    log.info("STEP 3: Placing Stop Loss order")
    log.info(f"  SL: ${signal.stop_loss:,.2f} (100% close)")
    log.info(f"{'='*60}")

    try:
        results["sl"] = create_sl_order(
            w3=w3, acct=acct, exchange=exchange, wallet=wallet,
            market=market, collateral_token=collateral_token,
            order_vault=order_vault, sl_price=signal.stop_loss,
            size_usd=size_usd, symbol=signal.symbol,
            is_long=signal.is_long, slippage_bps=slippage_bps,
            execution_fee=execution_fee, dry_run=dry_run,
        )
    except Exception as e:
        log.error(f"  SL failed: {e}")
        results["sl"] = None

    # ── Summary ──
    log.info(f"\n{'='*60}")
    log.info("EXECUTION SUMMARY")
    log.info(f"{'='*60}")
    log.info(f"  Open:  {results['open']}")
    for i, tp_r in enumerate(results["tp"]):
        status = tp_r["tx"] or f"FAILED: {tp_r.get('error', '?')}"
        log.info(f"  TP{i+1}:  {status}")
    log.info(f"  SL:    {results['sl']}")

    # Count total execution fees spent
    num_orders = 1 + len(signal.take_profits) + (1 if signal.stop_loss else 0)  # open + TPs + SL
    total_fee_eth = num_orders * execution_fee / (10 ** 18)
    log.info(f"  Total execution fees: {total_fee_eth:.4f} ETH "
             f"({num_orders} orders)")
    log.info(f"{'='*60}")

    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    RPC_URL = os.getenv("ARBITRUM_RPC_URL") or os.getenv("RPC_URL")
    if not RPC_URL:
        raise SystemExit("Missing ARBITRUM_RPC_URL (or RPC_URL)")

    PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()
    if not PRIVATE_KEY:
        raise SystemExit("Missing PRIVATE_KEY")

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    acct = Account.from_key(PRIVATE_KEY)

    # ── Show positions ──
    if "--positions" in sys.argv:
        fetch_positions(w3, acct.address)
        raise SystemExit(0)

    # ── Get signal text ──
    # Priority: --signal (stdin), SIGNAL env var, then fallback to env vars
    signal_text = None

    if "--signal" in sys.argv:
        signal_text = sys.stdin.read()
    elif os.getenv("SIGNAL"):
        signal_text = os.getenv("SIGNAL")

    if signal_text:
        # ── Parse signal mode ──
        signal = parse_signal(signal_text)
        log.info("Parsed signal:")
        log.info(f"  Symbol:   {signal.symbol} {signal.side}")
        log.info(f"  Entry:    ${signal.entry_low:,.2f} - ${signal.entry_high:,.2f}")
        for i, tp in enumerate(signal.take_profits):
            log.info(f"  TP{i+1}:     ${tp.price:,.2f} ({tp.close_pct:.0%})")
        log.info(f"  SL:       ${signal.stop_loss:,.2f}")
        log.info(f"  Leverage: {signal.leverage}x")
    else:
        # ── Env-var mode (backward compatible) ──
        SYMBOL = os.getenv("SYMBOL", "BTC").strip().upper()
        SIDE = os.getenv("SIDE", "LONG").strip().upper()
        IS_LONG = SIDE == "LONG"

        SIZE_USD = float(os.getenv("SIZE_USD", "10"))
        LEVERAGE = float(os.getenv("LEVERAGE", "2"))

        entry_env = os.getenv("ENTRY_PRICE", "").strip()
        if entry_env:
            ENTRY = float(entry_env)
        else:
            ENTRY = fetch_current_price(SYMBOL)

        # Parse TP/SL from env vars if present
        tps = []
        for i in range(1, 6):
            tp_env = os.getenv(f"TP{i}", "").strip()
            tp_pct_env = os.getenv(f"TP{i}_PCT", "").strip()
            if tp_env:
                pct = float(tp_pct_env) / 100.0 if tp_pct_env else 0
                tps.append(TakeProfit(price=float(tp_env), close_pct=pct))

        # Distribute unspecified percentages
        if tps:
            specified = sum(tp.close_pct for tp in tps)
            unspecified = [tp for tp in tps if tp.close_pct == 0]
            if unspecified:
                remaining = max(0, 1.0 - specified)
                each = remaining / len(unspecified)
                for tp in unspecified:
                    tp.close_pct = each

        sl_env = os.getenv("SL", "").strip()
        sl_price = float(sl_env) if sl_env else 0

        # If no TP/SL from env, run simple open-only (backward compat)
        if not tps and not sl_price:
            # Legacy mode: just open the position
            log.info("No TP/SL specified. Running simple MarketIncrease only.")
            EXCHANGE_ROUTER = must_addr("GMX_V2_EXCHANGE_ROUTER")
            ORDER_VAULT = must_addr("GMX_V2_ORDER_VAULT")
            MARKET = must_addr("GMX_V2_MARKET")
            COLLATERAL_TOKEN = must_addr("GMX_V2_COLLATERAL_TOKEN")
            SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "30"))
            EXECUTION_FEE_WEI = int(os.getenv(
                "GMX_V2_EXECUTION_FEE_WEI", str(Web3.to_wei(0.0002, "ether"))
            ))
            DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

            exchange = w3.eth.contract(
                address=Web3.to_checksum_address(EXCHANGE_ROUTER),
                abi=EXCHANGE_ROUTER_ABI,
            )

            COLLATERAL_USD = SIZE_USD / LEVERAGE
            txh = create_market_increase_order(
                w3=w3, acct=acct, exchange=exchange, wallet=acct.address,
                market=MARKET, collateral_token=COLLATERAL_TOKEN,
                order_vault=ORDER_VAULT, size_usd=SIZE_USD,
                collateral_usd=COLLATERAL_USD, entry_price=ENTRY,
                symbol=SYMBOL, is_long=IS_LONG,
                slippage_bps=SLIPPAGE_BPS, execution_fee=EXECUTION_FEE_WEI,
                dry_run=DRY_RUN,
            )
            print(f"Tx hash: {txh}")
            if not DRY_RUN:
                log.info("Waiting 5s for keeper execution...")
                time.sleep(5)
                fetch_positions(w3, acct.address)
            raise SystemExit(0)

        # Build signal from env vars
        signal = Signal(
            symbol=SYMBOL,
            side=SIDE,
            entry_low=ENTRY,
            entry_high=ENTRY,
            take_profits=tps,
            stop_loss=sl_price,
            leverage=LEVERAGE,
        )

    # ── Execute the signal ──
    EXCHANGE_ROUTER = must_addr("GMX_V2_EXCHANGE_ROUTER")
    ORDER_VAULT = must_addr("GMX_V2_ORDER_VAULT")
    MARKET = must_addr("GMX_V2_MARKET")
    COLLATERAL_TOKEN = must_addr("GMX_V2_COLLATERAL_TOKEN")
    SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "30"))
    EXECUTION_FEE_WEI = int(os.getenv(
        "GMX_V2_EXECUTION_FEE_WEI", str(Web3.to_wei(0.0002, "ether"))
    ))
    DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

    SIZE_USD = float(os.getenv("SIZE_USD", "10"))
    MAX_POSITION = float(os.getenv("MAX_POSITION_USD", "100"))
    MIN_POSITION = float(os.getenv("MIN_POSITION_USD", "20"))

    # Validate position size
    if SIZE_USD > MAX_POSITION:
        log.warning(f"SIZE_USD ${SIZE_USD} exceeds MAX_POSITION_USD ${MAX_POSITION}, "
                    f"capping to ${MAX_POSITION}")
        SIZE_USD = MAX_POSITION
    if SIZE_USD < MIN_POSITION:
        raise SystemExit(f"SIZE_USD ${SIZE_USD} below MIN_POSITION_USD ${MIN_POSITION}")

    # Validate leverage
    MAX_LEV = float(os.getenv("MAX_LEVERAGE", "10"))
    if signal.leverage > MAX_LEV:
        log.warning(f"Signal leverage {signal.leverage}x exceeds MAX_LEVERAGE {MAX_LEV}x, "
                    f"capping to {MAX_LEV}x")
        signal.leverage = MAX_LEV

    # Validate TP/SL makes sense — actually remove invalid TPs
    valid_tps = []
    if signal.is_long:
        for tp in signal.take_profits:
            if tp.price <= signal.entry_low:
                log.warning(f"TP ${tp.price:,.2f} is below entry — removing")
            else:
                valid_tps.append(tp)
        if signal.stop_loss >= signal.entry_low:
            log.warning(f"SL ${signal.stop_loss:,.2f} is above entry range — risky!")
    else:
        for tp in signal.take_profits:
            if tp.price >= signal.entry_high:
                log.warning(f"TP ${tp.price:,.2f} is above entry — removing")
            else:
                valid_tps.append(tp)
        if signal.stop_loss <= signal.entry_high:
            log.warning(f"SL ${signal.stop_loss:,.2f} is below entry range — risky!")
    if len(valid_tps) < len(signal.take_profits):
        log.warning(f"Removed {len(signal.take_profits) - len(valid_tps)} invalid TP(s)")
        signal.take_profits = valid_tps
        # Renormalize TP percentages to sum to 1.0
        total_pct = sum(tp.close_pct for tp in signal.take_profits)
        if signal.take_profits and total_pct > 0 and abs(total_pct - 1.0) > 0.001:
            for tp in signal.take_profits:
                tp.close_pct /= total_pct
            log.info(f"Renormalized {len(signal.take_profits)} TP(s) to sum to 100%")

    # Check ETH balance covers all execution fees
    num_orders = 1 + len(signal.take_profits) + (1 if signal.stop_loss else 0)
    total_fee = num_orders * EXECUTION_FEE_WEI
    if not DRY_RUN:
        eth_bal = w3.eth.get_balance(acct.address)
        if eth_bal < total_fee:
            raise SystemExit(
                "Insufficient ETH for execution fees. "
                f"Need {total_fee / 10**18:.4f} ETH for {num_orders} orders, "
                f"have {eth_bal / 10**18:.6f} ETH"
            )
        log.info(f"ETH balance: {eth_bal / 10**18:.6f} ETH "
                 f"(need {total_fee / 10**18:.4f} for {num_orders} orders)")

    COLLATERAL_USD = SIZE_USD / signal.leverage if signal.leverage else SIZE_USD
    results = execute_signal(
        w3=w3, acct=acct, signal=signal,
        exchange_router=EXCHANGE_ROUTER, order_vault=ORDER_VAULT,
        market=MARKET, collateral_token=COLLATERAL_TOKEN,
        size_usd=SIZE_USD, collateral_usd=COLLATERAL_USD,
        execution_fee=EXECUTION_FEE_WEI,
        slippage_bps=SLIPPAGE_BPS, dry_run=DRY_RUN,
    )

    if not DRY_RUN:
        log.info("\nWaiting 5s then showing final positions...")
        time.sleep(5)
        fetch_positions(w3, acct.address)
