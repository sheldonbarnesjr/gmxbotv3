#!/usr/bin/env python3
"""
GMX v2 Position Closer
Standalone script to close GMX v2 positions for testing decrease order logic
Based on the proven open.py implementation pattern
"""

import os
import sys
import time
import json
import logging
import functools
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

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
log = logging.getLogger("gmx-v2-close")


def retry_on_chain(max_retries: int = 3, base_delay: float = 2.0, label: str = ""):
    """Decorator that retries on-chain calls with exponential backoff."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except RuntimeError:
                    raise  # Reverted tx — don't retry
                except (ConnectionError, TimeoutError, OSError) as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = base_delay * (2 ** (attempt - 1))
                        log.warning(f"{label or func.__name__}: attempt {attempt}/{max_retries} failed ({e}), retrying in {delay:.0f}s...")
                        time.sleep(delay)
                except Exception as e:
                    err_str = str(e).lower()
                    is_rpc = any(kw in err_str for kw in ["connection", "timeout", "rate limit", "502", "503", "429"])
                    if is_rpc and attempt < max_retries:
                        last_exc = e
                        delay = base_delay * (2 ** (attempt - 1))
                        log.warning(f"{label or func.__name__}: attempt {attempt}/{max_retries} RPC error ({e}), retrying in {delay:.0f}s...")
                        time.sleep(delay)
                    else:
                        raise
            raise last_exc
        return wrapper
    return decorator


# -----------------------------
# ABIs (Same as open.py)
# -----------------------------

ERC20_ABI = [
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view", "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}]},
]

EXCHANGE_ROUTER_ABI = [
    {
        "name": "multicall",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "data", "type": "bytes[]"}],
        "outputs": [{"type": "bytes[]"}],
    },
    {
        "name": "sendWnt",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "receiver", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "createOrder",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {
                        "name": "addresses",
                        "type": "tuple",
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
                        "name": "numbers",
                        "type": "tuple",
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
        "name": "getAccountPositions",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "dataStore", "type": "address"},
            {"name": "account", "type": "address"},
            {"name": "start", "type": "uint256"},
            {"name": "end", "type": "uint256"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {
                        "name": "addresses",
                        "type": "tuple",
                        "components": [
                            {"name": "account", "type": "address"},
                            {"name": "market", "type": "address"},
                            {"name": "collateralToken", "type": "address"},
                        ],
                    },
                    {
                        "name": "numbers",
                        "type": "tuple",
                        "components": [
                            {"name": "sizeInUsd", "type": "uint256"},
                            {"name": "sizeInTokens", "type": "uint256"},
                            {"name": "collateralAmount", "type": "uint256"},
                            {"name": "borrowingFactor", "type": "uint256"},
                            {"name": "fundingFeeAmountPerSize", "type": "uint256"},
                            {"name": "longTokenClaimableFundingAmountPerSize", "type": "uint256"},
                            {"name": "shortTokenClaimableFundingAmountPerSize", "type": "uint256"},
                            {"name": "increasedAtTime", "type": "uint256"},
                            {"name": "decreasedAtTime", "type": "uint256"},
                        ],
                    },
                    {
                        "name": "flags",
                        "type": "tuple",
                        "components": [
                            {"name": "isLong", "type": "bool"},
                        ],
                    },
                ],
            },
        ],
    }
]

# Default GMX v2 addresses (Arbitrum)
GMX_V2_READER = "0xf60becbba223EEA9495Da3f606753867eC10d139"
GMX_V2_DATASTORE = "0xFD70de6b91282D8017aA4E741e9Ae325CAb992d8"
GMX_V2_READER_PNL = "0x22199a49A999c351eF7927602CFB187ec3cae489"
GMX_V2_REFERRAL_STORAGE = "0xe6fab3F0c7199b0d34d7FbE83394fc0e0D06e99d"

# Token decimal info per market (for building MarketPrices structs)
# {market_addr_lower: (index_token_decimals, long_token_decimals, short_token_decimals)}
MARKET_TOKEN_DECIMALS = {
    "0x47c031236e19d024b42f8ae6780e44a573170703": (8, 8, 6),    # BTC
    "0x70d95587d40a2caf56bd97485ab3eec10bee6336": (18, 18, 6),   # ETH
    "0x09400d9db990d5ed3f35d7be61dfaeb900af03c9": (9, 9, 6),     # SOL
    "0x7f1fa204bb700853d36994da19f830b6ad18455c": (18, 18, 6),   # LINK
    "0xc25cef6061cf5de5eb761b50e4743c1f5d7e5407": (18, 18, 6),   # ARB
    "0x6853ea96ff216fab11d2d930ce3c508556a4bdc4": (8, 8, 6),     # DOGE
    "0x7bbbf946883a5701350007320f525c5379b8178a": (18, 18, 6),   # AVAX
}

# Market symbols for better display (all keys stored lowercase for safe lookup)
MARKET_SYMBOLS = {
    "0x47c031236e19d024b42f8ae6780e44a573170703": "BTC",
    "0x70d95587d40a2caf56bd97485ab3eec10bee6336": "ETH",
    "0x09400d9db990d5ed3f35d7be61dfaeb900af03c9": "SOL",
    "0x7f1fa204bb700853d36994da19f830b6ad18455c": "LINK",
    "0xc25cef6061cf5de5eb761b50e4743c1f5d7e5407": "ARB",
    "0x6853ea96ff216fab11d2d930ce3c508556a4bdc4": "DOGE",
    "0x7bbbf946883a5701350007320f525c5379b8178a": "AVAX",
}

def market_to_symbol(market_addr: str) -> str:
    """Case-insensitive market address → symbol lookup."""
    return MARKET_SYMBOLS.get(market_addr.lower(), market_addr[:10] + "...")

# CoinGecko for current prices
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "LINK": "chainlink",
    "ARB": "arbitrum",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
}

# Helper functions (same pattern as open.py)
MAX_UINT256 = (1 << 256) - 1

def must_addr(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise SystemExit(f"Missing env var: {name}")
    return Web3.to_checksum_address(v)

def to_wei_decimal(amount: float, decimals: int) -> int:
    return round(amount * (10 ** decimals))

def from_wei_decimal(amount: int, decimals: int) -> float:
    return amount / (10 ** decimals)

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

def build_tx(w3: Web3, from_addr: str, to_addr: str, data, value=0, dry_run=False) -> dict:
    fees = get_fees(w3) if not dry_run else {"gasPrice": 20_000_000_000}
    nonce = w3.eth.get_transaction_count(from_addr, "pending") if not dry_run else 1
    tx = {
        "from": from_addr,
        "to": to_addr,
        "nonce": nonce,
        "value": value,
        "chainId": w3.eth.chain_id if not dry_run else 42161,
    }
    tx.update(fees)
    
    if dry_run:
        gas = int(os.getenv("GAS_LIMIT", "1000000"))
    else:
        est = w3.eth.estimate_gas({**tx, "data": data})
        gas = min(int(est * 1.25), int(os.getenv("GAS_LIMIT", "1000000")))
    
    tx["gas"] = gas
    tx["data"] = data
    return tx

def sign_send(w3: Web3, acct: Account, tx: dict, dry_run: bool) -> str:
    if dry_run:
        log.info(f"[DRY_RUN] Would send decrease tx to {tx['to']} value={tx['value']} gas={tx.get('gas')}")
        return f"dry_run_close_{int(time.time())}"
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    log.info(f"Close transaction sent: {tx_hash}")
    return tx_hash

# Chainlink on-chain price feeds (Arbitrum) — same feeds GMX V2 uses
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
_chainlink_decimals_cache_close: dict = {}


def fetch_current_price(symbol_pair: str, w3=None) -> float:
    """Fetch current price from Chainlink on-chain feeds (primary).
    Falls back to CoinGecko only if Chainlink unavailable."""

    # ── Primary: Chainlink on-chain ──
    feed_addr = CHAINLINK_FEEDS.get(symbol_pair.upper())
    if feed_addr and w3:
        try:
            feed = w3.eth.contract(
                address=Web3.to_checksum_address(feed_addr), abi=CHAINLINK_ABI
            )
            result = feed.functions.latestRoundData().call()
            if symbol_pair.upper() not in _chainlink_decimals_cache_close:
                _chainlink_decimals_cache_close[symbol_pair.upper()] = feed.functions.decimals().call()
            decimals = _chainlink_decimals_cache_close[symbol_pair.upper()]
            price = result[1] / (10 ** decimals)
            if price > 0:
                return float(price)
        except Exception as e:
            log.warning(f"Chainlink price failed for {symbol_pair}: {e}")

    # ── Fallback: CoinGecko ──
    coin_id = COINGECKO_IDS.get(symbol_pair.upper())
    if not coin_id:
        return 0.0
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        return float(data[coin_id]["usd"])
    except Exception:
        return 0.0

# ── Reader getPositionInfo ABI (for accurate PnL with fees) ──
# The return type is a deeply nested struct. We define the full ABI
# so web3.py can decode it properly.
_POSITION_INFO_ABI = {
    "name": "getPositionInfo",
    "type": "function",
    "stateMutability": "view",
    "inputs": [
        {"name": "dataStore", "type": "address"},
        {"name": "referralStorage", "type": "address"},
        {"name": "positionKey", "type": "bytes32"},
        {"name": "prices", "type": "tuple", "components": [
            {"name": "indexTokenPrice", "type": "tuple", "components": [
                {"name": "min", "type": "uint256"},
                {"name": "max", "type": "uint256"},
            ]},
            {"name": "longTokenPrice", "type": "tuple", "components": [
                {"name": "min", "type": "uint256"},
                {"name": "max", "type": "uint256"},
            ]},
            {"name": "shortTokenPrice", "type": "tuple", "components": [
                {"name": "min", "type": "uint256"},
                {"name": "max", "type": "uint256"},
            ]},
        ]},
        {"name": "sizeDeltaUsd", "type": "uint256"},
        {"name": "uiFeeReceiver", "type": "address"},
        {"name": "usePositionSizeAsSizeDeltaUsd", "type": "bool"},
    ],
    "outputs": [
        {"name": "", "type": "tuple", "components": [
            {"name": "positionKey", "type": "bytes32"},
            {"name": "position", "type": "tuple", "components": [
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
            ]},
            {"name": "fees", "type": "tuple", "components": [
                {"name": "referral", "type": "tuple", "components": [
                    {"name": "referralCode", "type": "bytes32"},
                    {"name": "affiliate", "type": "address"},
                    {"name": "trader", "type": "address"},
                    {"name": "totalRebateFactor", "type": "uint256"},
                    {"name": "affiliateRewardFactor", "type": "uint256"},
                    {"name": "traderDiscountFactor", "type": "uint256"},
                    {"name": "totalRebateAmount", "type": "uint256"},
                    {"name": "traderDiscountAmount", "type": "uint256"},
                    {"name": "affiliateRewardAmount", "type": "uint256"},
                ]},
                {"name": "funding", "type": "tuple", "components": [
                    {"name": "fundingFeeAmount", "type": "uint256"},
                    {"name": "claimableLongTokenAmount", "type": "uint256"},
                    {"name": "claimableShortTokenAmount", "type": "uint256"},
                    {"name": "latestFundingFeeAmountPerSize", "type": "uint256"},
                    {"name": "latestLongTokenClaimableFundingAmountPerSize", "type": "uint256"},
                    {"name": "latestShortTokenClaimableFundingAmountPerSize", "type": "uint256"},
                ]},
                {"name": "borrowing", "type": "tuple", "components": [
                    {"name": "borrowingFeeUsd", "type": "uint256"},
                    {"name": "borrowingFeeAmount", "type": "uint256"},
                    {"name": "borrowingFeeReceiverFactor", "type": "uint256"},
                    {"name": "borrowingFeeAmountForFeeReceiver", "type": "uint256"},
                ]},
                {"name": "ui", "type": "tuple", "components": [
                    {"name": "uiFeeReceiver", "type": "address"},
                    {"name": "uiFeeReceiverFactor", "type": "uint256"},
                    {"name": "uiFeeAmount", "type": "uint256"},
                ]},
                {"name": "collateralTokenPrice", "type": "tuple", "components": [
                    {"name": "min", "type": "uint256"},
                    {"name": "max", "type": "uint256"},
                ]},
                {"name": "positionFeeFactor", "type": "uint256"},
                {"name": "protocolFeeAmount", "type": "uint256"},
                {"name": "positionFeeReceiverFactor", "type": "uint256"},
                {"name": "feeReceiverAmount", "type": "uint256"},
                {"name": "feeAmountForPool", "type": "uint256"},
                {"name": "positionFeeAmountForPool", "type": "uint256"},
                {"name": "positionFeeAmount", "type": "uint256"},
                {"name": "totalCostAmountExcludingFunding", "type": "uint256"},
                {"name": "totalCostAmount", "type": "uint256"},
                {"name": "totalDiscountAmount", "type": "uint256"},
            ]},
            {"name": "executionPriceResult", "type": "tuple", "components": [
                {"name": "priceImpactUsd", "type": "int256"},
                {"name": "priceImpactDiffUsd", "type": "uint256"},
                {"name": "executionPrice", "type": "uint256"},
            ]},
            {"name": "basePnlUsd", "type": "int256"},
            {"name": "uncappedBasePnlUsd", "type": "int256"},
            {"name": "pnlAfterPriceImpactUsd", "type": "int256"},
        ]},
    ],
}

_READER_GET_MARKET_ABI = {
    "name": "getMarket",
    "type": "function",
    "stateMutability": "view",
    "inputs": [
        {"name": "dataStore", "type": "address"},
        {"name": "key", "type": "address"},
    ],
    "outputs": [
        {"name": "", "type": "tuple", "components": [
            {"name": "marketToken", "type": "address"},
            {"name": "indexToken", "type": "address"},
            {"name": "longToken", "type": "address"},
            {"name": "shortToken", "type": "address"},
        ]},
    ],
}

READER_PNL_ABI = [_POSITION_INFO_ABI, _READER_GET_MARKET_ABI]

# Cache market token info (marketAddr -> (indexToken, longToken, shortToken))
_market_tokens_cache: dict = {}


def _get_market_tokens(w3, market_addr: str) -> tuple:
    """Get (indexToken, longToken, shortToken) for a market from the Reader."""
    key = market_addr.lower()
    if key in _market_tokens_cache:
        return _market_tokens_cache[key]

    reader_addr = os.getenv("GMX_V2_READER_PNL", GMX_V2_READER_PNL)
    datastore = os.getenv("GMX_V2_DATASTORE", GMX_V2_DATASTORE)
    reader = w3.eth.contract(
        address=Web3.to_checksum_address(reader_addr),
        abi=READER_PNL_ABI,
    )
    result = reader.functions.getMarket(
        Web3.to_checksum_address(datastore),
        Web3.to_checksum_address(market_addr),
    ).call()
    # result = (marketToken, indexToken, longToken, shortToken)
    tokens = (result[1], result[2], result[3])
    _market_tokens_cache[key] = tokens
    return tokens


def _build_market_prices(index_price_usd: float, market_addr: str) -> tuple:
    """Build the MarketPrices tuple for getPositionInfo.

    Prices in GMX format: price_usd * 10^(30 - tokenDecimals).
    For query (not trade), min = max = current price.
    """
    key = market_addr.lower()
    decimals = MARKET_TOKEN_DECIMALS.get(key)
    if not decimals:
        raise ValueError(f"Unknown market {market_addr}")

    idx_dec, long_dec, short_dec = decimals

    # Index/long token price (e.g., BTC at $95k with 8 dec → 95000 * 10^22)
    index_price = int(index_price_usd * (10 ** (30 - idx_dec)))
    long_price = int(index_price_usd * (10 ** (30 - long_dec)))
    # Short token (USDC) price = $1
    short_price = int(1 * (10 ** (30 - short_dec)))

    return (
        (index_price, index_price),  # indexTokenPrice (min, max)
        (long_price, long_price),    # longTokenPrice (min, max)
        (short_price, short_price),  # shortTokenPrice (min, max)
    )


def _compute_position_key(w3, account: str, market: str, collateral_token: str, is_long: bool) -> bytes:
    """Compute GMX V2 position key: keccak256(abi.encode(account, market, collateralToken, isLong))."""
    from eth_abi import encode
    encoded = encode(
        ['address', 'address', 'address', 'bool'],
        [
            Web3.to_checksum_address(account),
            Web3.to_checksum_address(market),
            Web3.to_checksum_address(collateral_token),
            is_long,
        ]
    )
    return w3.keccak(encoded)


def fetch_position_pnl(
    w3,
    account: str,
    market: str,
    collateral_token: str,
    is_long: bool,
    current_price_usd: float,
) -> dict:
    """Fetch accurate PnL from the GMX V2 Reader contract.

    Returns dict with:
        base_pnl_usd: raw PnL from price movement
        borrowing_fee_usd: accumulated borrowing fees
        funding_fee_usd: accumulated funding fees (in collateral token units * price)
        closing_fee_usd: position fee to close
        net_pnl_usd: PnL after all fees
        success: True if the on-chain call worked

    Falls back to empty dict with success=False on any error.
    """
    try:
        reader_addr = os.getenv("GMX_V2_READER_PNL", GMX_V2_READER_PNL)
        datastore = os.getenv("GMX_V2_DATASTORE", GMX_V2_DATASTORE)
        referral_storage = os.getenv("GMX_V2_REFERRAL_STORAGE", GMX_V2_REFERRAL_STORAGE)

        reader = w3.eth.contract(
            address=Web3.to_checksum_address(reader_addr),
            abi=READER_PNL_ABI,
        )

        # Compute position key
        pos_key = _compute_position_key(w3, account, market, collateral_token, is_long)

        # Build market prices
        prices = _build_market_prices(current_price_usd, market)

        # Call getPositionInfo with usePositionSizeAsSizeDeltaUsd=True
        # to get PnL as if closing the entire position
        result = reader.functions.getPositionInfo(
            Web3.to_checksum_address(datastore),
            Web3.to_checksum_address(referral_storage),
            pos_key,
            prices,
            0,  # sizeDeltaUsd (0 since we use the flag below)
            "0x0000000000000000000000000000000000000000",  # uiFeeReceiver
            True,  # usePositionSizeAsSizeDeltaUsd
        ).call()

        # Parse the PositionInfo struct
        # result = (positionKey, position, fees, executionPriceResult, basePnlUsd, uncappedBasePnlUsd, pnlAfterPriceImpactUsd)
        fees = result[2]
        base_pnl_usd = result[4] / (10 ** 30)  # int256 scaled by 10^30

        # fees.borrowing.borrowingFeeUsd (fees[2][0]) — scaled by 10^30
        borrowing_fee_usd = fees[2][0] / (10 ** 30)

        # fees.funding.fundingFeeAmount (fees[1][0]) — in collateral token units
        # NOTE: This assumes USDC (short token, 6 dec) as collateral for all positions.
        # If a position uses a non-stablecoin collateral (WBTC/WETH), this will be wrong.
        collateral_decimals = MARKET_TOKEN_DECIMALS.get(market.lower(), (18, 18, 6))[2]
        funding_fee_amount = fees[1][0] / (10 ** collateral_decimals)
        funding_fee_usd = funding_fee_amount

        # fees.positionFeeAmount (fees[4+6] = index 11 in the flat tuple? No...)
        # The totalCostAmountExcludingFunding includes borrowing + position fee
        # totalCostAmount includes everything
        # Position fee = totalCostAmountExcludingFunding - borrowing fee amount
        total_cost_excl_funding = fees[12] / (10 ** collateral_decimals)  # totalCostAmountExcludingFunding
        total_cost = fees[13] / (10 ** collateral_decimals)  # totalCostAmount

        # Closing/position fee = total cost excl funding - borrowing fee amount
        borrowing_fee_in_token = fees[2][1] / (10 ** collateral_decimals)
        closing_fee_usd = max(0, total_cost_excl_funding - borrowing_fee_in_token)

        # Net PnL = basePnL - all fees
        net_pnl_usd = base_pnl_usd - borrowing_fee_usd - funding_fee_usd - closing_fee_usd

        return {
            "base_pnl_usd": base_pnl_usd,
            "borrowing_fee_usd": borrowing_fee_usd,
            "funding_fee_usd": funding_fee_usd,
            "closing_fee_usd": closing_fee_usd,
            "net_pnl_usd": net_pnl_usd,
            "success": True,
        }
    except Exception as e:
        log.debug(f"fetch_position_pnl failed for {market}: {e}")
        return {"success": False}


@dataclass
class GMXPosition:
    """Represents a GMX v2 position"""
    market: str
    symbol: str
    collateral_token: str
    is_long: bool
    size_usd: float
    size_tokens: float
    collateral_amount: float
    entry_price: float
    current_price: float
    leverage: float
    unrealized_pnl: float
    pnl_percentage: float
    # On-chain PnL breakdown (from Reader.getPositionInfo)
    base_pnl_usd: float = 0.0         # raw PnL from price movement
    borrowing_fee_usd: float = 0.0     # accumulated borrowing fees
    funding_fee_usd: float = 0.0       # accumulated funding fees (can be negative = earned)
    closing_fee_usd: float = 0.0       # fee to close the position
    net_pnl_usd: float = 0.0          # PnL after all fees (what you'd actually receive)
    pnl_source: str = "local"          # "local" or "onchain"

def fetch_positions(w3: Web3, wallet: str) -> List[GMXPosition]:
    """Fetch all open GMX v2 positions (enhanced from open.py)"""
    reader_addr = os.getenv("GMX_V2_READER", GMX_V2_READER)
    datastore_addr = os.getenv("GMX_V2_DATASTORE", GMX_V2_DATASTORE)
    
    reader = w3.eth.contract(
        address=Web3.to_checksum_address(reader_addr),
        abi=READER_ABI,
    )
    datastore = Web3.to_checksum_address(datastore_addr)
    
    positions = reader.functions.getAccountPositions(datastore, wallet, 0, 100).call()
    
    if not positions:
        return []
    
    parsed_positions = []
    
    for pos in positions:
        addresses = pos[0]
        numbers = pos[1]
        flags = pos[2]
        
        market = addresses[1]
        collateral_token = addresses[2]
        is_long = flags[0]
        
        size_usd = numbers[0] / (10 ** 30)
        size_tokens_raw = numbers[1]
        collateral_raw = numbers[2]
        
        # Get collateral token info
        try:
            col_contract = w3.eth.contract(address=collateral_token, abi=ERC20_ABI)
            col_dec = col_contract.functions.decimals().call()
            col_sym = col_contract.functions.symbol().call()
        except Exception:
            col_dec = 6
            col_sym = "USDC"
        
        collateral_amount = collateral_raw / (10 ** col_dec)
        leverage = size_usd / collateral_amount if collateral_amount > 0 else 0
        
        # Get symbol first so we can use it for token-decimal detection
        symbol = market_to_symbol(market)
        sym_key = symbol.upper().split("/")[0]
        INDEX_DECIMALS_MAP = {"BTC": 8, "ETH": 18, "SOL": 9, "LINK": 18, "ARB": 18, "DOGE": 8, "AVAX": 18}

        # Calculate entry price using known token decimals, fall back to heuristic
        if size_tokens_raw > 0:
            if sym_key in INDEX_DECIMALS_MAP:
                idx_dec = INDEX_DECIMALS_MAP[sym_key]
                entry_price = size_usd / (size_tokens_raw / (10 ** idx_dec))
                if not (0.001 < entry_price < 10_000_000):
                    entry_price = 0  # sanity check
            else:
                # Heuristic: try 8-decimal first (BTC), then 18-decimal (ETH)
                entry_8  = size_usd / (size_tokens_raw / (10 ** 8))
                entry_18 = size_usd / (size_tokens_raw / (10 ** 18))
                if 1 < entry_8 < 1_000_000:
                    entry_price = entry_8
                    idx_dec = 8
                elif 1 < entry_18 < 1_000_000:
                    entry_price = entry_18
                    idx_dec = 18
                else:
                    entry_price = entry_8
                    idx_dec = 8
        else:
            entry_price = 0
            idx_dec = 18

        size_tokens = size_tokens_raw / (10 ** idx_dec) if size_tokens_raw > 0 else 0

        # Get current price from Chainlink; fall back to entry_price
        current_price = fetch_current_price(symbol, w3=w3)
        if current_price == 0:
            current_price = entry_price  # fallback

        # Calculate P&L
        if is_long:
            price_diff = current_price - entry_price
        else:
            price_diff = entry_price - current_price

        if entry_price > 0:
            unrealized_pnl = (price_diff / entry_price) * collateral_amount * leverage
            pnl_percentage = (unrealized_pnl / collateral_amount) * 100
        else:
            unrealized_pnl = 0
            pnl_percentage = 0
        
        gpos = GMXPosition(
            market=market,
            symbol=symbol,
            collateral_token=collateral_token,
            is_long=is_long,
            size_usd=size_usd,
            size_tokens=size_tokens,
            collateral_amount=collateral_amount,
            entry_price=entry_price,
            current_price=current_price,
            leverage=leverage,
            unrealized_pnl=unrealized_pnl,
            pnl_percentage=pnl_percentage,
        )

        # Enrich with on-chain PnL from GMX Reader contract
        try:
            pnl_data = fetch_position_pnl(
                w3, wallet, market, collateral_token, is_long, current_price
            )
            if pnl_data.get("success"):
                gpos.base_pnl_usd = pnl_data["base_pnl_usd"]
                gpos.borrowing_fee_usd = pnl_data["borrowing_fee_usd"]
                gpos.funding_fee_usd = pnl_data["funding_fee_usd"]
                gpos.closing_fee_usd = pnl_data["closing_fee_usd"]
                gpos.net_pnl_usd = pnl_data["net_pnl_usd"]
                gpos.unrealized_pnl = pnl_data["net_pnl_usd"]
                if collateral_amount > 0:
                    gpos.pnl_percentage = (pnl_data["net_pnl_usd"] / collateral_amount) * 100
                gpos.pnl_source = "onchain"
        except Exception as e:
            log.debug(f"On-chain PnL enrichment failed for {symbol}: {e}")

        parsed_positions.append(gpos)
    
    return parsed_positions

def display_positions(positions: List[GMXPosition]):
    """Display positions in a nice table format"""
    if not positions:
        print("No open positions found.")
        return
    
    print(f"\n{'='*90}")
    print(f"  OPEN GMX v2 POSITIONS")
    print(f"{'='*90}")
    
    for i, pos in enumerate(positions):
        side = "LONG" if pos.is_long else "SHORT"
        pnl_symbol = "+" if pos.unrealized_pnl >= 0 else ""
        pnl_color = "🟢" if pos.unrealized_pnl >= 0 else "🔴"
        
        print(f"\n  [{i+1}] {pos.symbol} {side}")
        print(f"      Market:     {pos.market}")
        print(f"      Size:       ${pos.size_usd:,.2f}")
        print(f"      Collateral: ${pos.collateral_amount:,.2f}")
        print(f"      Leverage:   {pos.leverage:.1f}x")
        print(f"      Entry:      ${pos.entry_price:,.2f}")
        print(f"      Current:    ${pos.current_price:,.2f}")
        print(f"      P&L:        {pnl_color} ${pnl_symbol}{pos.unrealized_pnl:.2f} ({pnl_symbol}{pos.pnl_percentage:.1f}%)")
    
    print(f"\n{'='*90}")

@retry_on_chain(max_retries=3, label="create_close_order")
def create_close_order(
    w3: Web3,
    acct: Account,
    position: GMXPosition,
    percentage: float = 1.0,
    dry_run: bool = True,
    debug: bool = False
) -> str:
    """Create GMX v2 decrease order to close position"""
    percentage = max(0.01, min(percentage, 1.0))

    if debug:
        log.debug(f"🔧 Creating close order for {position.symbol} {'LONG' if position.is_long else 'SHORT'}")
        log.debug(f"   Position size: ${position.size_usd:.2f}")
        log.debug(f"   Collateral: ${position.collateral_amount:.2f}")
        log.debug(f"   Close percentage: {percentage:.0%}")
    
    try:
        exchange_router_addr = must_addr("GMX_V2_EXCHANGE_ROUTER")
        order_vault_addr = must_addr("GMX_V2_ORDER_VAULT")
        execution_fee = int(os.getenv("GMX_V2_EXECUTION_FEE_WEI", str(Web3.to_wei(0.0002, "ether"))))
        slippage_bps = int(os.getenv("SLIPPAGE_BPS", "30"))
        
        if debug:
            log.debug(f"🔧 GMX Addresses:")
            log.debug(f"   Exchange Router: {exchange_router_addr}")
            log.debug(f"   Order Vault: {order_vault_addr}")
            log.debug(f"   Execution Fee: {execution_fee / 10**18:.4f} ETH")
            log.debug(f"   Slippage: {slippage_bps / 100:.1f}%")
        
        # Calculate close amounts
        close_size_usd = position.size_usd * percentage
        size_delta_usd = int(close_size_usd * (10 ** 30))
        
        if debug:
            log.debug(f"🔧 Close Calculations:")
            log.debug(f"   Close size USD: ${close_size_usd:.2f}")
            log.debug(f"   Size delta (scaled): {size_delta_usd}")
        
        # Get collateral token decimals
        try:
            col_token = w3.eth.contract(address=position.collateral_token, abi=ERC20_ABI)
            col_decimals = col_token.functions.decimals().call() if not dry_run else 6
            col_symbol = col_token.functions.symbol().call() if not dry_run else "USDC"
        except Exception as e:
            col_decimals = 6  # USDC default
            col_symbol = "USDC"
            if debug:
                log.debug(f"⚠️ Failed to get collateral info, using defaults: {e}")
        
        close_collateral = position.collateral_amount * percentage
        # For full close (100%), set collateral delta to 0 — the protocol
        # automatically returns all remaining collateral + PnL.
        # For partial close, specify proportional collateral withdrawal.
        if percentage >= 1.0:
            collateral_delta = 0
        else:
            collateral_delta = int(close_collateral * (10 ** col_decimals))
        
        if debug:
            log.debug(f"🔧 Collateral Info:")
            log.debug(f"   Token: {position.collateral_token}")
            log.debug(f"   Symbol: {col_symbol}")
            log.debug(f"   Decimals: {col_decimals}")
            log.debug(f"   Close amount: {close_collateral:.4f} {col_symbol}")
            log.debug(f"   Collateral delta (scaled): {collateral_delta}")
        
        # Calculate acceptable price with slippage
        slip = slippage_bps / 10_000.0

        if position.is_long:
            # LONG close = selling, want minimum acceptable price
            acceptable_price = position.current_price * (1 - slip)
            direction = "selling (LONG close)"
        else:
            # SHORT close = buying back, want maximum acceptable price
            acceptable_price = position.current_price * (1 + slip)
            direction = "buying (SHORT close)"

        # GMX V2 price precision = 10^(30 - index_token_decimals)
        # BTC = 8 decimals → precision = 10^22
        # SOL = 9 decimals → precision = 10^21
        # ETH/others = 18 decimals → precision = 10^12
        # Detect from symbol name first, then fall back to price heuristic
        sym_upper = position.symbol.upper().split("/")[0]
        INDEX_DECIMALS_MAP = {"BTC": 8, "ETH": 18, "SOL": 9, "LINK": 18, "ARB": 18, "DOGE": 8, "AVAX": 18}
        if sym_upper in INDEX_DECIMALS_MAP:
            index_token_decimals = INDEX_DECIMALS_MAP[sym_upper]
        elif position.entry_price > 10_000:
            index_token_decimals = 8   # BTC-like
        elif position.entry_price > 100:
            index_token_decimals = 9   # SOL-like
        else:
            index_token_decimals = 18  # ETH-like

        price_precision = 10 ** (30 - index_token_decimals)
        acceptable_price_scaled = int(acceptable_price * price_precision)
        
        if debug:
            log.debug(f"🔧 Price Calculations:")
            log.debug(f"   Current price: ${position.current_price:.2f}")
            log.debug(f"   Direction: {direction}")
            log.debug(f"   Slippage: {slip*100:.1f}%")
            log.debug(f"   Acceptable price: ${acceptable_price:.2f}")
            log.debug(f"   Index token decimals: {index_token_decimals}")
            log.debug(f"   Price precision: 10^{30 - index_token_decimals}")
            log.debug(f"   Acceptable (scaled): {acceptable_price_scaled}")
        
        # Order parameters
        ORDER_TYPE_MARKET_DECREASE = 4
        DECREASE_SWAP_TYPE_NO_SWAP = 0
        ZERO_ADDR = Web3.to_checksum_address("0x0000000000000000000000000000000000000000")
        
        # Create decrease order parameters
        if debug:
            log.debug(f"🔧 Order Parameters:")
            log.debug(f"   Order Type: {ORDER_TYPE_MARKET_DECREASE} (MARKET_DECREASE)")
            log.debug(f"   Is Long: {position.is_long}")
            log.debug(f"   Market: {position.market}")
            log.debug(f"   Collateral Token: {position.collateral_token}")
        
        wallet = Web3.to_checksum_address(acct.address)
        market_cs = Web3.to_checksum_address(position.market)
        collateral_cs = Web3.to_checksum_address(position.collateral_token)

        params = (
            # addresses
            (wallet, wallet, ZERO_ADDR, ZERO_ADDR,
             market_cs, collateral_cs, []),
            # numbers
            (size_delta_usd,
             collateral_delta,  # initialCollateralDeltaAmount (key for decrease)
             0,  # triggerPrice (0 for market order)
             acceptable_price_scaled,
             execution_fee,
             0,  # callbackGasLimit
             0,  # minOutputAmount
             0), # validFromTime
            ORDER_TYPE_MARKET_DECREASE,
            DECREASE_SWAP_TYPE_NO_SWAP,
            position.is_long,
            False,  # shouldUnwrapNativeToken
            False,  # autoCancel
            b"\x00" * 32,  # referralCode
            [],  # dataList
        )
        
        if debug:
            log.debug(f"🔧 Full Order Params Built Successfully")
        
        # Create exchange router contract
        exchange = w3.eth.contract(address=exchange_router_addr, abi=EXCHANGE_ROUTER_ABI)
        
        # Multicall for decrease order (only 2 calls - no token transfer needed)
        data1 = exchange.encode_abi("sendWnt", [order_vault_addr, execution_fee])
        data2 = exchange.encode_abi("createOrder", [params])
        
        if debug:
            log.debug(f"🔧 Multicall Data:")
            log.debug(f"   Call 1 (sendWnt): {len(data1)} bytes")
            log.debug(f"   Call 2 (createOrder): {len(data2)} bytes")
        
        call_data = exchange.encode_abi("multicall", [[data1, data2]])
        
        if debug:
            log.debug(f"🔧 Transaction Building:")
            log.debug(f"   To: {exchange_router_addr}")
            log.debug(f"   Value: {execution_fee / 10**18:.6f} ETH")
            log.debug(f"   Calldata: {len(call_data)} bytes")
        
        # Build and send transaction
        tx = build_tx(w3, acct.address, exchange_router_addr, call_data, value=execution_fee, dry_run=dry_run)
        
        if debug:
            log.debug(f"🔧 Built Transaction:")
            log.debug(f"   Gas: {tx.get('gas', 'not set')}")
            log.debug(f"   Nonce: {tx.get('nonce', 'not set')}")
            if 'gasPrice' in tx:
                log.debug(f"   Gas Price: {tx['gasPrice'] / 10**9:.1f} gwei")
            if 'maxFeePerGas' in tx:
                log.debug(f"   Max Fee: {tx['maxFeePerGas'] / 10**9:.1f} gwei")
        
        tx_hash = sign_send(w3, acct, tx, dry_run)
        
        if not dry_run and not tx_hash.startswith("dry_run"):
            receipt = wait_receipt(w3, tx_hash)
            if receipt.get("status") != 1:
                raise RuntimeError(f"Close order transaction failed: {tx_hash}")
        
        close_size = position.size_usd * percentage
        close_pnl = position.unrealized_pnl * percentage
        
        log.info(f"GMX v2 close order created:")
        log.info(f"  Symbol: {position.symbol} {'LONG' if position.is_long else 'SHORT'}")
        log.info(f"  Size: ${close_size:.2f} ({percentage:.0%})")
        log.info(f"  Expected P&L: ${close_pnl:+.2f}")
        log.info(f"  Acceptable Price: ${acceptable_price:.2f}")
        log.info(f"  TX: {tx_hash}")
        
        return tx_hash
        
    except Exception as e:
        log.error(f"Error creating close order: {e}")
        raise

def main():
    """Main close.py execution"""
    
    # Parse command line arguments
    if len(sys.argv) > 1 and sys.argv[1] in ["-h", "--help", "help"]:
        print("""
GMX v2 Position Closer - Test decrease order logic

Usage:
  python3 close.py                    # Show positions and interactive close
  python3 close.py --list            # Just list positions
  python3 close.py --close-first     # Close the first position (quick test)
  python3 close.py --close-all       # Close all positions
  python3 close.py --close-all 50%   # Close 50% of all positions
  python3 close.py --dry-run         # Force dry run mode
  python3 close.py --debug           # Extra debugging output
  
Environment Variables:
  ARBITRUM_RPC_URL         # Arbitrum RPC endpoint
  PRIVATE_KEY              # Your wallet private key  
  GMX_V2_EXCHANGE_ROUTER   # GMX v2 ExchangeRouter address
  GMX_V2_ORDER_VAULT       # GMX v2 OrderVault address
  DRY_RUN=true            # Enable dry run mode (default: false)
  SLIPPAGE_BPS=30         # Slippage in basis points (default: 30 = 0.3%)
        """)
        return
    
    # Configuration
    RPC_URL = os.getenv("ARBITRUM_RPC_URL") or os.getenv("RPC_URL")
    if not RPC_URL:
        raise SystemExit("Missing ARBITRUM_RPC_URL (or RPC_URL)")
    
    PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()
    if not PRIVATE_KEY:
        raise SystemExit("Missing PRIVATE_KEY")
    
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    acct = Account.from_key(PRIVATE_KEY)
    
    DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
    DEBUG = False
    if "--dry-run" in sys.argv:
        DRY_RUN = True
    if "--debug" in sys.argv:
        DEBUG = True
        logging.getLogger().setLevel(logging.DEBUG)
    
    log.info(f"GMX v2 Position Closer")
    log.info(f"Wallet: {acct.address}")
    log.info(f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    
    # Test network connection
    try:
        chain_id = w3.eth.chain_id
        latest_block = w3.eth.get_block('latest')
        log.info(f"Network: Arbitrum (Chain ID: {chain_id})")
        log.info(f"Latest Block: #{latest_block['number']}")
        
        # Check required environment variables
        required_vars = {
            "ARBITRUM_RPC_URL": RPC_URL,
            "GMX_V2_EXCHANGE_ROUTER": os.getenv("GMX_V2_EXCHANGE_ROUTER"),
            "GMX_V2_ORDER_VAULT": os.getenv("GMX_V2_ORDER_VAULT"),
        }
        
        missing_vars = [k for k, v in required_vars.items() if not v]
        if missing_vars:
            log.error(f"❌ Missing environment variables: {missing_vars}")
            log.error("Set them with:")
            log.error("export GMX_V2_EXCHANGE_ROUTER='0x7C68C7866A64FA2160F78EEaE12217FFbf871fa8'")
            log.error("export GMX_V2_ORDER_VAULT='0x31eF83a530Fde1B38EE9A18093A333D8Bbbc40Dd'")
            return
        
        if DEBUG:
            log.debug(f"🔧 Environment Check:")
            log.debug(f"   RPC URL: {RPC_URL}")
            log.debug(f"   Block Time: {latest_block['timestamp']}")
            log.debug(f"   Exchange Router: {required_vars['GMX_V2_EXCHANGE_ROUTER']}")
            log.debug(f"   Order Vault: {required_vars['GMX_V2_ORDER_VAULT']}")
            log.debug(f"   Dry Run: {DRY_RUN}")
            
    except Exception as e:
        log.error(f"❌ Network connection failed: {e}")
        return
    
    # Check ETH balance for gas
    if not DRY_RUN:
        try:
            eth_balance = w3.eth.get_balance(acct.address)
            eth_balance_formatted = eth_balance / 10**18
            execution_fee_eth = int(os.getenv("GMX_V2_EXECUTION_FEE_WEI", str(Web3.to_wei(0.0002, "ether")))) / 10**18
            
            log.info(f"ETH Balance: {eth_balance_formatted:.6f} ETH")
            log.info(f"Execution Fee: {execution_fee_eth:.6f} ETH per order")
            
            if eth_balance_formatted < execution_fee_eth:
                log.error(f"❌ Insufficient ETH for gas! Need at least {execution_fee_eth:.6f} ETH")
                return
        except Exception as e:
            log.warning(f"⚠️ Could not check ETH balance: {e}")
    
    # Fetch positions
    print("\n🔄 Fetching open positions...")
    positions = fetch_positions(w3, acct.address)
    
    if not positions:
        print("✅ No open positions found.")
        return
    
    # Display positions
    display_positions(positions)
    
    total_pnl = sum(pos.unrealized_pnl for pos in positions)
    total_size = sum(pos.size_usd for pos in positions)
    
    print(f"\n📊 Portfolio Summary:")
    print(f"   Total Positions: {len(positions)}")
    print(f"   Total Size: ${total_size:,.2f}")
    print(f"   Total P&L: ${total_pnl:+.2f}")
    
    # Handle command line actions
    if "--list" in sys.argv:
        return
    
    if "--close-first" in sys.argv:
        if not positions:
            print("❌ No positions to close")
            return
        
        pos = positions[0]
        print(f"\n🎯 Testing close on first position: {pos.symbol} {'LONG' if pos.is_long else 'SHORT'}")
        print(f"   Size: ${pos.size_usd:.2f}, P&L: ${pos.unrealized_pnl:+.2f}")
        
        if not DRY_RUN:
            confirm = input("Type 'YES' to close this position: ")
            if confirm != "YES":
                print("❌ Cancelled")
                return
        
        print(f"\n🔄 Creating close order...")
        tx_hash = create_close_order(w3, acct, pos, 1.0, DRY_RUN, DEBUG)
        
        if tx_hash:
            print("✅ Close order submitted successfully!")
            if not DRY_RUN:
                print(f"🔗 TX: {tx_hash}")
                print(f"📊 Monitor at: https://arbiscan.io/tx/{tx_hash}")
        else:
            print("❌ Failed to submit close order")
        return
    
    if "--close-all" in sys.argv:
        # Parse percentage if provided
        percentage = 1.0  # Default 100%
        for arg in sys.argv:
            if "%" in arg:
                percentage = float(arg.replace("%", "")) / 100
                break
        
        print(f"\n⚠️ About to close {percentage:.0%} of ALL positions!")
        if not DRY_RUN:
            confirm = input("Type 'YES' to confirm: ")
            if confirm != "YES":
                print("❌ Cancelled")
                return
        
        success_count = 0
        for pos in positions:
            print(f"\n🔄 Closing {pos.symbol} {'LONG' if pos.is_long else 'SHORT'}...")
            tx_hash = create_close_order(w3, acct, pos, percentage, DRY_RUN, DEBUG)
            if tx_hash:
                success_count += 1
            time.sleep(1)  # Small delay between orders
        
        print(f"\n✅ Close orders created: {success_count}/{len(positions)}")
        return
    
    # Interactive mode
    while True:
        try:
            print(f"\n🎮 Choose action:")
            print(f"   1-{len(positions)}: Close specific position")
            print(f"   'all': Close all positions")
            print(f"   'refresh': Refresh positions")
            print(f"   'quit': Exit")
            
            choice = input("\nEnter choice: ").strip().lower()
            
            if choice == 'quit':
                break
            elif choice == 'refresh':
                print("\n🔄 Refreshing positions...")
                positions = fetch_positions(w3, acct.address)
                display_positions(positions)
                continue
            elif choice == 'all':
                percentage = 1.0
                pct_input = input("Enter percentage to close (default 100%): ").strip()
                if pct_input:
                    percentage = float(pct_input.replace("%", "")) / 100
                
                print(f"\n⚠️ About to close {percentage:.0%} of ALL {len(positions)} positions!")
                if not DRY_RUN:
                    confirm = input("Type 'YES' to confirm: ")
                    if confirm != "YES":
                        print("❌ Cancelled")
                        continue
                
                for pos in positions:
                    print(f"\n🔄 Closing {pos.symbol}...")
                    create_close_order(w3, acct, pos, percentage, DRY_RUN, DEBUG)
                    time.sleep(1)
                
                print("✅ All close orders submitted")
                
            else:
                try:
                    index = int(choice) - 1
                    if 0 <= index < len(positions):
                        pos = positions[index]
                        
                        percentage = 1.0
                        pct_input = input(f"Enter percentage to close (default 100%): ").strip()
                        if pct_input:
                            percentage = float(pct_input.replace("%", "")) / 100
                        
                        print(f"\n🔄 Closing {percentage:.0%} of {pos.symbol} {'LONG' if pos.is_long else 'SHORT'}...")
                        tx_hash = create_close_order(w3, acct, pos, percentage, DRY_RUN, DEBUG)
                        
                        if tx_hash:
                            print("✅ Close order submitted successfully!")
                        else:
                            print("❌ Failed to submit close order")
                    else:
                        print("❌ Invalid position number")
                except ValueError:
                    print("❌ Invalid input")
        
        except KeyboardInterrupt:
            print("\n👋 Goodbye!")
            break
        except Exception as e:
            print(f"❌ Error: {e}")

if __name__ == "__main__":
    main()