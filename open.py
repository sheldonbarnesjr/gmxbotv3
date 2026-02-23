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
import urllib.request
from dataclasses import dataclass
from typing import List, Dict, Any

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

    @property
    def is_long(self) -> bool:
        return self.side == "LONG"

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2


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
            "USDT", "CLOSE", "TRAILING", "ENABLED"}
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
        r'(?:ENTRY|ENTER|ENTRY\s*ZONE)\s*[:=@\-]?\s*\$?([\d,]+(?:\.\d+)?)\s*[-–]\s*\$?([\d,]+(?:\.\d+)?)',
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
            raise ValueError("Could not find entry price")

    # ── Take Profits ──
    take_profits = []
    # Match patterns like: TP1: 48000 (50% close)  or  TP 1: 48000 50%
    tp_pattern = re.compile(
        r'TP\s*(\d+)?\s*[:=@\-]?\s*\$?([\d,]+(?:\.\d+)?)\s*'
        r'(?:\(?\s*(\d+)\s*%\s*(?:close)?\s*\)?)?',
        re.IGNORECASE
    )
    for m in tp_pattern.finditer(txt):
        price = float(m.group(2).replace(",", ""))
        pct_str = m.group(3)
        pct = float(pct_str) / 100.0 if pct_str else None
        take_profits.append(TakeProfit(price=price, close_pct=pct or 0))

    # Also match: TARGET 1: 48000, TAKE PROFIT: 48000
    if not take_profits:
        target_pattern = re.compile(
            r'(?:TARGET|TAKE\s*PROFIT)\s*\d?\s*[:=@\-]?\s*\$?([\d,]+(?:\.\d+)?)\s*'
            r'(?:\(?\s*(\d+)\s*%\s*(?:close)?\s*\)?)?',
            re.IGNORECASE
        )
        for m in target_pattern.finditer(txt):
            price = float(m.group(1).replace(",", ""))
            pct_str = m.group(2)
            pct = float(pct_str) / 100.0 if pct_str else None
            take_profits.append(TakeProfit(price=price, close_pct=pct or 0))

    # Assign close percentages if not specified in the signal.
    # Each TP's close_pct is a fraction of the ORIGINAL position size.
    # All close_pct values must sum to 1.0 (100%) so the full position
    # is closed across all TPs.
    #
    # Default distributions (when signal doesn't specify %):
    #   2 TPs: 33%, 67%           — small early lock-in, let the rest ride
    #   3 TPs: 20%, 50%, 30%      — light trim, bulk at TP2, remainder at TP3
    #   4 TPs: 15%, 30%, 30%, 25% — scale out gradually
    #   5 TPs: 10%, 20%, 30%, 25%, 15% — pyramid out, heaviest at TP3
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
        9:  [0.04, 0.08, 0.12, 0.16, 0.16, 0.14, 0.12, 0.10, 0.08],
        10: [0.04, 0.07, 0.10, 0.13, 0.15, 0.14, 0.12, 0.10, 0.08, 0.07],
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

        # Ensure total sums to exactly 1.0 — push any rounding
        # remainder onto the last TP so the position fully closes.
        total_pct = sum(tp.close_pct for tp in take_profits)
        if take_profits and abs(total_pct - 1.0) > 0.001:
            take_profits[-1].close_pct += 1.0 - total_pct

    # ── Stop Loss ──
    sl_match = re.search(
        r'(?:SL|STOP\s*LOSS|STOP)\s*[:=@\-]?\s*\$?([\d,]+(?:\.\d+)?)',
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

    return Signal(
        symbol=symbol,
        side=side,
        entry_low=entry_low,
        entry_high=entry_high,
        take_profits=take_profits,
        stop_loss=stop_loss,
        leverage=leverage,
        raw_text=txt,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def must_addr(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise SystemExit(f"Missing env var: {name}")
    return Web3.to_checksum_address(v)


def to_wei_decimal(amount: float, decimals: int) -> int:
    return int(amount * (10 ** decimals))


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
        max_fee = base + priority * 2
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
    gas = min(int(est * 1.25), int(os.getenv("GAS_LIMIT", "2000000")))
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


def _get_order_keys_from_events(w3: Web3, wallet_addr: str, lookback_blocks: int = 500000) -> list:
    """Get active order keys via EventEmitter OrderCreated logs.

    The GMX V2 EventEmitter emits OrderCreated with:
      topic[2] = bytes32 order key
      topic[3] = account address (padded to 32 bytes)

    Validates each key with cancelOrder gas estimate to confirm it's still active.
    Returns list of bytes objects (each 32 bytes).
    """
    EVENT_EMITTER = "0xC8ee91A54287DB53897056e12D9819156D3822Fb"
    ORDER_CREATED_TOPIC = "0x468a25a7ba624ceea6e540ad6f49171b52495b648417ae91bca21676d8a24dc5"
    EXCHANGE_ROUTER = os.getenv("GMX_V2_EXCHANGE_ROUTER", "").strip()

    wallet = Web3.to_checksum_address(wallet_addr)
    # topic[3] = account padded to 32 bytes (0x + 24 zeros + 40-char address)
    wallet_topic = "0x" + "0" * 24 + wallet_addr.lower().lstrip("0x")

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
            "address": EVENT_EMITTER,
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

    Combines:
      - _parse_orders_raw()  for order metadata (type, size, trigger price, market)
      - _get_order_keys_from_events()  for bytes32 order keys via EventEmitter logs

    Keys and metadata are merged by index position (both return orders in creation order).

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
    active_keys = _get_order_keys_from_events(w3, wallet_addr)

    result = []
    for i, o in enumerate(raw_orders):
        key_bytes = active_keys[i] if i < len(active_keys) else None
        result.append({**o, "key_hex": key_bytes.hex() if key_bytes else None})

    return result


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
    print(f"\n{'='*70}")
    print(f"  OPEN POSITIONS for {wallet[:10]}...{wallet[-6:]}")
    print(f"{'='*70}")
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
        if size_tokens_raw > 0:
            entry_8 = size_usd / (size_tokens_raw / (10 ** 8))
            entry_18 = size_usd / (size_tokens_raw / (10 ** 18))
            entry_price = entry_8 if 1 < entry_8 < 1_000_000 else entry_18
        else:
            entry_price = 0
        side = "LONG" if is_long else "SHORT"
        print(f"\n  Position #{i+1}")
        print(f"    Market:     {market}")
        print(f"    Side:       {side}")
        print(f"    Size:       ${size_usd:,.2f}")
        print(f"    Collateral: {collateral_amount:,.2f} {col_sym}")
        print(f"    Leverage:   {leverage:.1f}x")
        print(f"    Entry:      ${entry_price:,.2f}")
    print(f"\n{'='*70}\n")
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
        initial_collateral_delta=0,
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
    sl_slip = max(slippage_bps, 100) / 10_000.0  # at least 1% for SL

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
    num_orders = 1 + len(signal.take_profits) + 1  # open + TPs + SL
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

    # Validate TP/SL makes sense
    if signal.is_long:
        for tp in signal.take_profits:
            if tp.price <= signal.entry_low:
                log.warning(f"TP ${tp.price:,.2f} is below entry — skipping")
        if signal.stop_loss >= signal.entry_low:
            log.warning(f"SL ${signal.stop_loss:,.2f} is above entry range — risky!")
    else:
        for tp in signal.take_profits:
            if tp.price >= signal.entry_high:
                log.warning(f"TP ${tp.price:,.2f} is above entry — skipping")
        if signal.stop_loss <= signal.entry_high:
            log.warning(f"SL ${signal.stop_loss:,.2f} is below entry range — risky!")

    # Check ETH balance covers all execution fees
    num_orders = 1 + len(signal.take_profits) + (1 if signal.stop_loss else 0)
    total_fee = num_orders * EXECUTION_FEE_WEI
    if not DRY_RUN:
        eth_bal = w3.eth.get_balance(acct.address)
        if eth_bal < total_fee:
            raise SystemExit(
                f"Insufficient ETH for execution fees. "
                f"Need {total_fee / 10**18:.4f} ETH for {num_orders} orders, "
                f"have {eth_bal / 10**18:.6f} ETH"
            )
        log.info(f"ETH balance: {eth_bal / 10**18:.6f} ETH "
                 f"(need {total_fee / 10**18:.4f} for {num_orders} orders)")

    results = execute_signal(
        w3=w3, acct=acct, signal=signal,
        exchange_router=EXCHANGE_ROUTER, order_vault=ORDER_VAULT,
        market=MARKET, collateral_token=COLLATERAL_TOKEN,
        size_usd=SIZE_USD, execution_fee=EXECUTION_FEE_WEI,
        slippage_bps=SLIPPAGE_BPS, dry_run=DRY_RUN,
    )

    if not DRY_RUN:
        log.info("\nWaiting 5s then showing final positions...")
        time.sleep(5)
        fetch_positions(w3, acct.address)
