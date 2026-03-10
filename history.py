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

# GMX V2 market address → index token decimals (for price conversion)
# sizeDeltaInTokens is in index token's smallest unit
_MARKET_INDEX_DECIMALS = {
    "0x47c031236e19d024b42f8ae6780e44a573170703": 8,   # BTC/USD
    "0x70d95587d40a2caf56bd97485ab3eec10bee6336": 18,  # ETH/USD
    "0x09400d9db990d5ed3f35d7be61dfaeb900af03c9": 9,   # SOL/USD
    "0x7f1fa204bb700853d36994da19f830b6ad18455c": 18,  # LINK/USD
}

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


def _get_event_log2_abi():
    """Return EventLog2 ABI for decoding OrderExecuted events."""
    from open import _EVENT_LOG2_ABI
    return _EVENT_LOG2_ABI


def _build_order_type_map(receipt, emitter_addr_lower, el2_contract, order_executed_topic_hex):
    """Build orderKey → order info map from OrderExecuted EventLog2 events in a receipt.

    Scans all logs in a transaction receipt for OrderExecuted events and
    extracts the orderType and triggerPrice from each one's eventData uintItems.

    Returns:
        Dict mapping orderKey (hex str without 0x) → {"order_type": int, "trigger_price": float}.
    """
    order_type_map = {}
    el2_topic_hex = _EVENT_LOG2_TOPIC[2:]  # strip 0x

    for rlog in receipt["logs"]:
        try:
            topics = [t.hex() if isinstance(t, bytes) else t.replace("0x", "") for t in rlog["topics"]]
            if rlog["address"].lower() != emitter_addr_lower:
                continue
            if len(topics) < 4 or topics[0] != el2_topic_hex:
                continue
            if topics[1] != order_executed_topic_hex:
                continue

            order_key_hex = topics[2]

            decoded = el2_contract.events.EventLog2().process_log(rlog)
            ed = decoded["args"]["eventData"]
            order_type = None
            for item in ed["uintItems"]["items"]:
                if item["key"] == "orderType":
                    order_type = int(item["value"])
                    break
            order_type_map[order_key_hex] = {
                "order_type": order_type,
                "trigger_price": 0,
            }
        except Exception:
            continue

    return order_type_map


def _extract_order_key_from_decrease(event_data):
    """Extract orderKey from a PositionDecrease event's bytes32Items.

    Returns orderKey as hex string (without 0x prefix), or None if not found.
    """
    try:
        for item in event_data["bytes32Items"]["items"]:
            if item["key"] == "orderKey":
                val = item["value"]
                if isinstance(val, bytes):
                    return val.hex()
                return str(val).replace("0x", "")
    except Exception:
        pass
    return None


def _fetch_trigger_prices(w3, account, from_block, current_block):
    """Fetch trigger prices from OrderCreated events for this wallet.

    Returns:
        Tuple of:
        - dict mapping orderKey (hex without 0x) → trigger_price (float USD)
        - list of all order info dicts with keys: order_key, trigger_price,
          market, is_long, order_type, block_number
    """
    emitter_addr = Web3.to_checksum_address(_EVENT_EMITTER)
    wallet_topic = "0x" + "0" * 24 + account.lower().replace("0x", "")
    order_created_topic = "0x" + Web3.keccak(text="OrderCreated").hex()

    try:
        created_logs = w3.eth.get_logs({
            "address": emitter_addr,
            "fromBlock": from_block,
            "toBlock": current_block,
            "topics": [
                _EVENT_LOG2_TOPIC,
                order_created_topic,
                None,
                wallet_topic,
            ],
        })
    except Exception as e:
        log.debug(f"_fetch_trigger_prices: get_logs failed: {e}")
        return {}, []

    if not created_logs:
        return {}, []

    el2_abi = _get_event_log2_abi()
    emitter2 = w3.eth.contract(address=emitter_addr, abi=el2_abi)

    trigger_map = {}
    all_orders = []
    for rlog in created_logs:
        try:
            topics = [t.hex() if isinstance(t, bytes) else t.replace("0x", "") for t in rlog["topics"]]
            order_key_hex = topics[2]

            decoded = emitter2.events.EventLog2().process_log(rlog)
            ed = decoded["args"]["eventData"]

            # Get market address to determine correct precision
            market_addr = None
            for item in ed["addressItems"]["items"]:
                if item["key"] == "market":
                    market_addr = item["value"].lower()
                    break

            trigger_price_raw = 0
            order_type = None
            for item in ed["uintItems"]["items"]:
                k = item["key"]
                if k == "triggerPrice":
                    trigger_price_raw = item["value"]
                elif k == "orderType":
                    order_type = int(item["value"])

            is_long = None
            for item in ed["boolItems"]["items"]:
                if item["key"] == "isLong":
                    is_long = item["value"]

            if trigger_price_raw > 0:
                # Price precision = 10^(30 - index_token_decimals)
                idx_dec = _MARKET_INDEX_DECIMALS.get(market_addr, 18)
                precision = 10 ** (30 - idx_dec)
                price = trigger_price_raw / precision
                trigger_map[order_key_hex] = price
                # order_type 5 = LimitDecrease (TP), 6 = StopLossDecrease (SL)
                all_orders.append({
                    "order_key": order_key_hex,
                    "trigger_price": price,
                    "market": market_addr,
                    "is_long": is_long,
                    "order_type": order_type,
                    "block_number": rlog["blockNumber"],
                })
        except Exception:
            continue

    log.debug(f"_fetch_trigger_prices: found {len(trigger_map)} trigger prices, {len(all_orders)} orders")
    return trigger_map, all_orders


def _fetch_position_increase_timestamps(
    w3: Web3,
    account: str,
    from_block: int,
    current_block: int,
) -> Dict[str, int]:
    """Fetch PositionIncrease timestamps to find when positions were opened.

    Scans EventLog1 for PositionIncrease events matching the account.
    Uses topic1 (account padded to bytes32) to filter server-side.

    Returns:
        Dict mapping (market_address_lower, is_long) → earliest timestamp.
    """
    emitter_addr = Web3.to_checksum_address(_EVENT_EMITTER)
    el1_abi = _get_event_log1_abi()
    emitter1 = w3.eth.contract(address=emitter_addr, abi=el1_abi)
    account_lower = account.lower()

    # PositionIncrease eventNameHash (indexed string → keccak)
    increase_topic = "0x" + Web3.keccak(text="PositionIncrease").hex()
    # topic1 = account address padded to bytes32
    account_topic = "0x" + "0" * 24 + account_lower.replace("0x", "")

    try:
        logs = w3.eth.get_logs({
            "address": emitter_addr,
            "fromBlock": from_block,
            "toBlock": current_block,
            "topics": [
                _EVENT_LOG1_TOPIC,
                increase_topic,
                account_topic,
            ],
        })
    except Exception as e:
        log.debug(f"_fetch_position_increase_timestamps failed: {e}")
        return {}

    result = {}  # (market_lower, is_long) → list of timestamps (sorted)
    for rlog in logs:
        try:
            decoded = emitter1.events.EventLog1().process_log(rlog)
            if decoded["args"].get("eventName") != "PositionIncrease":
                continue
            ed = decoded["args"]["eventData"]

            market_addr = None
            for item in ed["addressItems"]["items"]:
                if item["key"] == "market":
                    market_addr = item["value"].lower()

            is_long = None
            for item in ed["boolItems"]["items"]:
                if item["key"] == "isLong":
                    is_long = item["value"]

            block_num = rlog["blockNumber"]
            blk = w3.eth.get_block(block_num)
            ts = blk["timestamp"]

            key = (market_addr, is_long)
            if key not in result:
                result[key] = []
            result[key].append(ts)

        except Exception:
            continue

    # Sort each list of timestamps
    for key in result:
        result[key].sort()

    log.debug(f"_fetch_position_increase_timestamps: found {sum(len(v) for v in result.values())} increases across {len(result)} positions")
    return result


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
        return [], []

    if not exec_logs:
        return [], []

    # Fetch trigger prices from OrderCreated events
    trigger_price_map, all_created_orders = _fetch_trigger_prices(w3, account, from_block, current_block)

    # Fetch position open timestamps from PositionIncrease events
    open_ts_map = _fetch_position_increase_timestamps(w3, account, from_block, current_block)

    # Build EventLog1 decoder
    el1_abi = _get_event_log1_abi()
    emitter1 = w3.eth.contract(address=emitter_addr, abi=el1_abi)

    # Build EventLog2 decoder for OrderExecuted (to extract order types)
    el2_abi = _get_event_log2_abi()
    emitter2 = w3.eth.contract(address=emitter_addr, abi=el2_abi)
    order_executed_topic_hex = Web3.keccak(text="OrderExecuted").hex()

    results = []
    processed_receipts = {}  # tx_hash → receipt (avoid re-fetching batched txs)
    account_lower = account.lower()
    emitter_lower = emitter_addr.lower()

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

        # Build orderKey → {order_type, trigger_price} map from OrderExecuted events
        order_info_map = _build_order_type_map(
            receipt, emitter_lower, emitter2, order_executed_topic_hex
        )

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
                order_type_val = None
                size_delta_in_tokens = 0
                collateral_delta_raw = 0
                for item in ed["uintItems"]["items"]:
                    k = item["key"]
                    if k == "sizeDeltaUsd":
                        size_delta = item["value"] / _P30
                    elif k == "sizeDeltaInTokens":
                        size_delta_in_tokens = item["value"]
                    elif k == "collateralDeltaAmount":
                        collateral_delta_raw = item["value"]
                    elif k == "borrowingFeeAmount":
                        borrowing_fee_raw = item["value"]
                    elif k == "positionFeeAmount":
                        position_fee_raw = item["value"]
                    elif k == "executionPrice":
                        execution_price_raw = item["value"]
                    elif k == "orderType":
                        order_type_val = int(item["value"])

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

                # Convert collateral delta to USD
                collateral_delta_usd = 0.0
                if collateral_delta_raw and collateral_token:
                    coll_dec = _TOKEN_DECIMALS.get(collateral_token, 6)
                    coll_tokens = collateral_delta_raw / (10 ** coll_dec)
                    if collateral_token in _STABLECOINS:
                        collateral_delta_usd = coll_tokens
                    elif execution_price_raw > 0:
                        collateral_delta_usd = coll_tokens * (execution_price_raw / _P30)

                net_pnl = base_pnl + price_impact - fees_usd

                # Get block timestamp
                blk = w3.eth.get_block(block_num)
                ts = blk["timestamp"]

                # Filter by since_timestamp
                if since_timestamp and ts < since_timestamp:
                    continue

                # Use tx_hash:logIndex as unique ID to handle multi-event txs
                log_idx = rlog.get("logIndex", 0)

                # Compute USD execution price: sizeDeltaUsd / sizeDeltaInTokens
                idx_dec = _MARKET_INDEX_DECIMALS.get((market_addr or "").lower(), 18)
                if size_delta > 0 and size_delta_in_tokens > 0:
                    tokens_float = size_delta_in_tokens / (10 ** idx_dec)
                    execution_price = size_delta / tokens_float if tokens_float > 0 else 0
                else:
                    execution_price = 0

                # Look up order_type from OrderExecuted, trigger_price from OrderCreated
                order_key = _extract_order_key_from_decrease(ed)
                order_info = order_info_map.get(order_key, {}) if order_key else {}
                if order_info.get("order_type") is not None:
                    order_type_val = order_info["order_type"]
                trigger_price = trigger_price_map.get(order_key, 0) if order_key else 0

                # Look up position open timestamp — find the most recent
                # PositionIncrease that occurred before this decrease event
                opened_at = 0
                increase_times = open_ts_map.get((market_addr, is_long), [])
                for inc_ts in reversed(increase_times):
                    if inc_ts <= ts:
                        opened_at = inc_ts
                        break

                results.append({
                    "market_address": market_addr or "",
                    "is_long": is_long,
                    "size_delta_usd": size_delta,
                    "execution_price": execution_price,
                    "trigger_price": trigger_price,
                    "collateral_delta_usd": collateral_delta_usd,
                    "pnl_usd": base_pnl,
                    "net_pnl_usd": net_pnl,
                    "price_impact_usd": price_impact,
                    "total_fees_usd": fees_usd,
                    "timestamp": ts,
                    "opened_at": opened_at,
                    "tx_hash": tx_hash,
                    "log_index": log_idx,
                    "block_number": block_num,
                    "order_type": order_type_val,  # 5=TP, 6=SL, 4=MarketDecrease
                })

            except Exception as e:
                log.debug(f"Failed to decode event log: {e}")
                continue

    log.info(f"fetch_trade_history: {len(results)} trade(s) for {account[:10]}...")
    return results, all_created_orders


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

    # Fetch trigger prices from OrderCreated events
    trigger_price_map, all_created_orders = _fetch_trigger_prices(w3, account, from_block, current_block)

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

    # EventLog2 decoder for OrderExecuted events (to extract orderType)
    el2_abi = _get_event_log2_abi()
    emitter2 = w3.eth.contract(address=emitter_addr, abi=el2_abi)
    order_executed_topic_hex = Web3.keccak(text="OrderExecuted").hex()

    results = []
    processed_receipts = {}
    account_lower = account.lower()
    emitter_lower = emitter_addr.lower()

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

        # Build orderKey → orderType map from OrderExecuted events in this tx
        order_type_map = _build_order_type_map(
            receipt, emitter_lower, emitter2, order_executed_topic_hex
        )

        for rlog in receipt["logs"]:
            topics = [t.hex() if isinstance(t, bytes) else t.replace("0x", "") for t in rlog["topics"]]
            if not (rlog["address"].lower() == emitter_lower
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
                size_delta_in_tokens = 0
                execution_price_raw = 0
                borrowing_fee_raw = 0
                position_fee_raw = 0
                collateral_delta_raw = 0
                for item in ed["uintItems"]["items"]:
                    k = item["key"]
                    if k == "sizeDeltaUsd":
                        size_delta = item["value"] / _P30
                    elif k == "sizeDeltaInTokens":
                        size_delta_in_tokens = item["value"]
                    elif k == "executionPrice":
                        execution_price_raw = item["value"]
                    elif k == "collateralDeltaAmount":
                        collateral_delta_raw = item["value"]
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

                # Convert collateral delta to USD
                collateral_delta_usd = 0.0
                if collateral_delta_raw and collateral_token:
                    coll_dec = _TOKEN_DECIMALS.get(collateral_token, 6)
                    coll_tokens = collateral_delta_raw / (10 ** coll_dec)
                    if collateral_token in _STABLECOINS:
                        collateral_delta_usd = coll_tokens
                    elif execution_price_raw > 0:
                        collateral_delta_usd = coll_tokens * (execution_price_raw / _P30)

                net_pnl = base_pnl + price_impact - fees_usd

                # Compute USD execution price: sizeDeltaUsd / sizeDeltaInTokens
                # (This is the ENTRY price in GMX V2, not exit)
                idx_dec = _MARKET_INDEX_DECIMALS.get((event_market or "").lower(), 18)
                if size_delta > 0 and size_delta_in_tokens > 0:
                    tokens_float = size_delta_in_tokens / (10 ** idx_dec)
                    execution_price = size_delta / tokens_float if tokens_float > 0 else 0
                else:
                    execution_price = 0

                # Look up order_type from OrderExecuted, trigger_price from OrderCreated
                order_key = _extract_order_key_from_decrease(ed)
                order_info = order_type_map.get(order_key, {}) if order_key else {}
                order_type = order_info.get("order_type") if isinstance(order_info, dict) else order_info
                trigger_price = trigger_price_map.get(order_key, 0) if order_key else 0

                blk = w3.eth.get_block(block_num)

                results.append({
                    "market_address": event_market or "",
                    "is_long": event_is_long,
                    "size_delta_usd": size_delta,
                    "execution_price": execution_price,
                    "trigger_price": trigger_price,
                    "collateral_delta_usd": collateral_delta_usd,
                    "pnl_usd": base_pnl,
                    "net_pnl_usd": net_pnl,
                    "price_impact_usd": price_impact,
                    "total_fees_usd": fees_usd,
                    "timestamp": blk["timestamp"],
                    "tx_hash": tx_hash,
                    "log_index": rlog.get("logIndex", 0),
                    "order_type": order_type,  # 5=TP, 6=SL, 4=MarketDecrease, None=unknown
                })

            except Exception as e:
                log.debug(f"Failed to decode event log: {e}")
                continue

    log.info(
        f"fetch_recent_position_decreases: {len(results)} decrease(s) "
        f"for {account[:10]} market={market_lower[:10]}..."
    )
    return results
