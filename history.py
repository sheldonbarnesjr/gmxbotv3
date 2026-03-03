"""
GMX V2 Trade History — fetch realized PnL from on-chain event logs.

Queries the GMX EventEmitter for OrderExecuted events, then decodes
the associated PositionDecrease events in the same transaction to
extract realized PnL, size, and market data.

Works for TPs, SLs, and manual closes — including those that happened
while the bot was offline.
"""

import logging
import time
from typing import List, Dict, Any, Optional

from web3 import Web3

log = logging.getLogger("GMXBot.history")

# GMX V2 EventEmitter on Arbitrum
_EVENT_EMITTER = "0xC8ee91A54287DB53897056e12D9819156D3822Fb"

# EventLog2 and EventLog1 selectors
_EVENT_LOG2_TOPIC = "0x468a25a7ba624ceea6e540ad6f49171b52495b648417ae91bca21676d8a24dc5"
_EVENT_LOG1_TOPIC = "0x137a44067c8961cd7e1d876f4754a5a3a75989b4552f1843fc69c3b372def160"

# GMX V2 USD precision
_P30 = 10 ** 30

# Arbitrum ~4 blocks/sec
_BLOCKS_PER_SECOND = 4

# Known stablecoin addresses on Arbitrum (lowercase) — fee amounts in these
# tokens map 1:1 to USD after dividing by their decimals.
_STABLECOINS = {
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",  # USDC.e
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
}

# token address (lowercase) → decimals
_TOKEN_DECIMALS = {
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": 6,   # USDC
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8": 6,   # USDC.e
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": 18,  # WETH
    "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f": 8,   # WBTC
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": 6,   # USDT
}


def _get_event_log1_abi():
    """Build EventLog1 ABI using the eventData components from open.py."""
    from open import _EVENT_LOG2_ABI
    eventdata_def = _EVENT_LOG2_ABI[0]["inputs"][5]
    return [{
        "anonymous": False,
        "inputs": [
            {"indexed": False, "name": "msgSender", "type": "address"},
            {"indexed": False, "name": "eventName", "type": "string"},
            {"indexed": True, "name": "eventNameHash", "type": "string"},
            {"indexed": True, "name": "topic1", "type": "bytes32"},
            {
                "components": eventdata_def["components"],
                "indexed": False,
                "name": "eventData",
                "type": "tuple",
            },
        ],
        "name": "EventLog1",
        "type": "event",
    }]


def fetch_trade_history(
    w3: Web3,
    account: str,
    since_timestamp: Optional[int] = None,
    lookback_seconds: int = 30 * 86400,
) -> List[Dict[str, Any]]:
    """Fetch historical position decrease events from on-chain logs.

    Finds OrderExecuted events for the account, then decodes the
    PositionDecrease EventLog1 in the same transaction to get PnL.

    Args:
        w3: Web3 instance connected to Arbitrum.
        account: Wallet address.
        since_timestamp: Unix timestamp — only return events after this.
        lookback_seconds: How far back to search (default 30 days).

    Returns:
        List of trade dicts with: market_address, is_long, size_delta_usd,
        pnl_usd, timestamp.
    """
    emitter_addr = Web3.to_checksum_address(_EVENT_EMITTER)
    wallet_topic = "0x" + "0" * 24 + account.lower().replace("0x", "")

    # Calculate block range
    current_block = w3.eth.block_number
    if since_timestamp:
        elapsed = int(time.time()) - since_timestamp
        lookback_blocks = min(elapsed * _BLOCKS_PER_SECOND, lookback_seconds * _BLOCKS_PER_SECOND)
    else:
        lookback_blocks = lookback_seconds * _BLOCKS_PER_SECOND
    from_block = max(0, current_block - int(lookback_blocks))

    # OrderExecuted topic hash
    order_executed_topic = "0x" + Web3.keccak(text="OrderExecuted").hex()

    # Get all OrderExecuted events where this wallet is topic3
    try:
        exec_logs = w3.eth.get_logs({
            "address": emitter_addr,
            "fromBlock": from_block,
            "toBlock": current_block,
            "topics": [
                _EVENT_LOG2_TOPIC,
                order_executed_topic,
                None,
                wallet_topic,
            ],
        })
    except Exception as e:
        log.warning(f"fetch_trade_history: get_logs failed for {account[:10]}: {e}")
        return []

    if not exec_logs:
        return []

    # Build EventLog1 decoder
    el1_abi = _get_event_log1_abi()
    emitter1 = w3.eth.contract(address=emitter_addr, abi=el1_abi)

    results = []
    processed_receipts = {}  # tx_hash → receipt (avoid re-fetching batched txs)
    account_lower = account.lower()

    for exec_log in exec_logs:
        tx_hash = exec_log["transactionHash"].hex()
        block_num = exec_log["blockNumber"]

        # Skip if we already scanned this receipt (keeper batched multiple orders)
        if tx_hash in processed_receipts:
            continue

        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception as e:
            log.debug(f"Failed to fetch receipt for {tx_hash}: {e}")
            continue
        processed_receipts[tx_hash] = True

        # Find PositionDecrease EventLog1 in same transaction
        for rlog in receipt["logs"]:
            topics = [t.hex() if isinstance(t, bytes) else t.replace("0x", "") for t in rlog["topics"]]
            if not (rlog["address"].lower() == emitter_addr.lower()
                    and topics[0] == _EVENT_LOG1_TOPIC[2:]):
                continue

            try:
                decoded = emitter1.events.EventLog1().process_log(rlog)
                event_name = decoded["args"].get("eventName", "")
                if event_name != "PositionDecrease":
                    continue

                ed = decoded["args"]["eventData"]

                # Address items — extract market, account, and collateral token
                market_addr = None
                event_account = None
                collateral_token = None
                for item in ed["addressItems"]["items"]:
                    if item["key"] == "market":
                        market_addr = item["value"].lower()
                    elif item["key"] == "account":
                        event_account = item["value"].lower()
                    elif item["key"] == "collateralToken":
                        collateral_token = item["value"].lower()

                # Skip events that belong to a different wallet
                if event_account and event_account != account_lower:
                    continue

                # Uint items — size + fee amounts (in collateral token units)
                size_delta = 0
                borrowing_fee_raw = 0
                position_fee_raw = 0
                execution_price_raw = 0
                for item in ed["uintItems"]["items"]:
                    k = item["key"]
                    if k == "sizeDeltaUsd":
                        size_delta = item["value"] / _P30
                    elif k == "borrowingFeeAmount":
                        borrowing_fee_raw = item["value"]
                    elif k == "positionFeeAmount":
                        position_fee_raw = item["value"]
                    elif k == "executionPrice":
                        execution_price_raw = item["value"]

                # Int items — PnL and price impact (already USD at 1e30)
                base_pnl = 0
                price_impact = 0
                for item in ed["intItems"]["items"]:
                    k = item["key"]
                    if k == "basePnlUsd":
                        base_pnl = item["value"] / _P30
                    elif k == "priceImpactUsd":
                        price_impact = item["value"] / _P30

                # Bool items
                is_long = None
                for item in ed["boolItems"]["items"]:
                    if item["key"] == "isLong":
                        is_long = item["value"]

                # Convert fee amounts from collateral-token units to USD
                fees_usd = 0.0
                if collateral_token and (borrowing_fee_raw or position_fee_raw):
                    decimals = _TOKEN_DECIMALS.get(collateral_token, 6)
                    fee_tokens = (borrowing_fee_raw + position_fee_raw) / (10 ** decimals)
                    if collateral_token in _STABLECOINS:
                        fees_usd = fee_tokens
                    elif execution_price_raw > 0:
                        # Non-stablecoin collateral (WBTC/WETH): convert via execution price
                        fees_usd = fee_tokens * (execution_price_raw / _P30)

                net_pnl = base_pnl + price_impact - fees_usd

                # Get block timestamp
                blk = w3.eth.get_block(block_num)
                ts = blk["timestamp"]

                # Filter by since_timestamp
                if since_timestamp and ts < since_timestamp:
                    continue

                # Use tx_hash:logIndex as unique ID to handle multi-event txs
                log_idx = rlog.get("logIndex", 0)

                results.append({
                    "market_address": market_addr or "",
                    "is_long": is_long,
                    "size_delta_usd": size_delta,
                    "pnl_usd": base_pnl,
                    "net_pnl_usd": net_pnl,
                    "price_impact_usd": price_impact,
                    "total_fees_usd": fees_usd,
                    "timestamp": ts,
                    "tx_hash": tx_hash,
                    "log_index": log_idx,
                })

            except Exception as e:
                log.debug(f"Failed to decode event log: {e}")
                continue

    log.info(f"fetch_trade_history: {len(results)} trade(s) for {account[:10]}...")
    return results


def fetch_recent_position_decreases(
    w3: Web3,
    account: str,
    market: str,
    is_long: bool,
    lookback_seconds: int = 600,
) -> List[Dict[str, Any]]:
    """Fetch recent PositionDecrease events for a specific market+wallet.

    Lightweight version of fetch_trade_history — only scans a short block
    window (default 10 min) and filters to a single market+direction.
    Used by check_tp_hits to verify that a TP order disappearance
    corresponds to an actual keeper execution, not a cancellation.

    Returns list of dicts with: size_delta_usd, execution_price, is_long,
    market_address, timestamp, tx_hash.
    """
    emitter_addr = Web3.to_checksum_address(_EVENT_EMITTER)
    wallet_topic = "0x" + "0" * 24 + account.lower().replace("0x", "")
    market_lower = market.lower()

    current_block = w3.eth.block_number
    lookback_blocks = lookback_seconds * _BLOCKS_PER_SECOND
    from_block = max(0, current_block - int(lookback_blocks))

    order_executed_topic = "0x" + Web3.keccak(text="OrderExecuted").hex()

    try:
        exec_logs = w3.eth.get_logs({
            "address": emitter_addr,
            "fromBlock": from_block,
            "toBlock": current_block,
            "topics": [
                _EVENT_LOG2_TOPIC,
                order_executed_topic,
                None,
                wallet_topic,
            ],
        })
    except Exception as e:
        log.warning(f"fetch_recent_position_decreases: get_logs failed: {e}")
        return []

    if not exec_logs:
        return []

    el1_abi = _get_event_log1_abi()
    emitter1 = w3.eth.contract(address=emitter_addr, abi=el1_abi)

    results = []
    processed_receipts = {}
    account_lower = account.lower()

    for exec_log in exec_logs:
        tx_hash = exec_log["transactionHash"].hex()
        block_num = exec_log["blockNumber"]

        if tx_hash in processed_receipts:
            continue

        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception as e:
            log.debug(f"Failed to fetch receipt for {tx_hash}: {e}")
            continue
        processed_receipts[tx_hash] = True

        for rlog in receipt["logs"]:
            topics = [t.hex() if isinstance(t, bytes) else t.replace("0x", "") for t in rlog["topics"]]
            if not (rlog["address"].lower() == emitter_addr.lower()
                    and topics[0] == _EVENT_LOG1_TOPIC[2:]):
                continue

            try:
                decoded = emitter1.events.EventLog1().process_log(rlog)
                event_name = decoded["args"].get("eventName", "")
                if event_name != "PositionDecrease":
                    continue

                ed = decoded["args"]["eventData"]

                event_market = None
                event_account = None
                collateral_token = None
                for item in ed["addressItems"]["items"]:
                    if item["key"] == "market":
                        event_market = item["value"].lower()
                    elif item["key"] == "account":
                        event_account = item["value"].lower()
                    elif item["key"] == "collateralToken":
                        collateral_token = item["value"].lower()

                if event_account and event_account != account_lower:
                    continue
                if event_market and event_market != market_lower:
                    continue

                event_is_long = None
                for item in ed["boolItems"]["items"]:
                    if item["key"] == "isLong":
                        event_is_long = item["value"]

                if event_is_long is not None and event_is_long != is_long:
                    continue

                size_delta = 0
                execution_price_raw = 0
                borrowing_fee_raw = 0
                position_fee_raw = 0
                for item in ed["uintItems"]["items"]:
                    k = item["key"]
                    if k == "sizeDeltaUsd":
                        size_delta = item["value"] / _P30
                    elif k == "executionPrice":
                        execution_price_raw = item["value"]
                    elif k == "borrowingFeeAmount":
                        borrowing_fee_raw = item["value"]
                    elif k == "positionFeeAmount":
                        position_fee_raw = item["value"]

                # PnL from intItems
                base_pnl = 0
                price_impact = 0
                for item in ed["intItems"]["items"]:
                    k = item["key"]
                    if k == "basePnlUsd":
                        base_pnl = item["value"] / _P30
                    elif k == "priceImpactUsd":
                        price_impact = item["value"] / _P30

                # Convert fees to USD (same logic as fetch_trade_history)
                fees_usd = 0.0
                if collateral_token and (borrowing_fee_raw or position_fee_raw):
                    decimals = _TOKEN_DECIMALS.get(collateral_token, 6)
                    fee_tokens = (borrowing_fee_raw + position_fee_raw) / (10 ** decimals)
                    if collateral_token in _STABLECOINS:
                        fees_usd = fee_tokens
                    elif execution_price_raw > 0:
                        fees_usd = fee_tokens * (execution_price_raw / _P30)

                net_pnl = base_pnl + price_impact - fees_usd
                execution_price = execution_price_raw / _P30 if execution_price_raw else 0

                blk = w3.eth.get_block(block_num)

                results.append({
                    "market_address": event_market or "",
                    "is_long": event_is_long,
                    "size_delta_usd": size_delta,
                    "execution_price": execution_price,
                    "net_pnl_usd": net_pnl,
                    "timestamp": blk["timestamp"],
                    "tx_hash": tx_hash,
                    "log_index": rlog.get("logIndex", 0),
                })

            except Exception as e:
                log.debug(f"Failed to decode event log: {e}")
                continue

    log.info(
        f"fetch_recent_position_decreases: {len(results)} decrease(s) "
        f"for {account[:10]} market={market_lower[:10]}..."
    )
    return results
