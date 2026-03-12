"""
Centralized Trade Rebuilder.

Single source of truth for all trade data (GMX on-chain + Bitunix API).
Every call queries live sources and overwrites JSON files with fresh data.

JSON files managed:
  - onchain_trades.json: raw on-chain PositionDecrease events (merged cache)
  - trade_history.json: complete closed-trade list (GMX + Bitunix TradeRecords)
  - position_state.json: open-position verified_decreases updated from on-chain

Usage:
  from trade_rebuilder import rebuild_all_trades, rebuild_open_positions

  trades = await rebuild_all_trades(w3, wallets, markets, bitunix_client, open_positions)
  updated = await rebuild_open_positions(w3, wallets, positions, markets, bitunix_client)
"""

import os
import asyncio
import logging
from datetime import datetime
from dataclasses import asdict, fields
from typing import List, Dict, Any, Optional, Tuple

from state_io import atomic_json_write, safe_json_read
from history import fetch_trade_history, build_rich_trades
from close import fetch_positions as chain_fetch_positions

logger = logging.getLogger("GMXBot.rebuilder")

_rebuild_lock = asyncio.Lock()

TRADE_HISTORY_FILE = "json/trade_history.json"
ONCHAIN_TRADES_FILE = "json/onchain_trades.json"

# Only show trades from this date forward.
TRADE_START_DATE = "2026-03-09T01:00"


def _parse_start_ts() -> int:
    from zoneinfo import ZoneInfo
    try:
        fmt = "%Y-%m-%dT%H:%M" if "T" in TRADE_START_DATE else "%Y-%m-%d"
        dt = datetime.strptime(TRADE_START_DATE, fmt).replace(tzinfo=ZoneInfo("America/New_York"))
        return int(dt.timestamp())
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────
# On-chain event fetch + merge + persist
# ─────────────────────────────────────────────────────────────────────

async def _fetch_and_merge_onchain(w3, wallets: List[Tuple[int, Any]]) -> Tuple[List[dict], List[dict]]:
    """Fetch fresh on-chain events, merge with stored cache, persist, return (merged, created_orders)."""
    fresh = []
    all_created_orders = []
    for _wid, acct in wallets:
        try:
            trades, created_orders = await asyncio.to_thread(
                fetch_trade_history, w3, acct.address
            )
            fresh.extend(trades)
            all_created_orders.extend(created_orders)
        except Exception as e:
            logger.warning(f"On-chain trade fetch failed for wallet {_wid}: {e}")

    stored = safe_json_read(ONCHAIN_TRADES_FILE, default=[])

    # Merge: tx_hash:log_index as unique key
    by_key = {}
    stored_tx_only = set()
    for t in stored:
        tx = t.get("tx_hash", "")
        li = t.get("log_index")
        if tx and li is not None:
            by_key[f"{tx}:{li}"] = t
        elif tx:
            by_key[tx] = t
            stored_tx_only.add(tx)
    for t in fresh:
        tx = t.get("tx_hash", "")
        li = t.get("log_index", 0)
        if tx:
            if tx in stored_tx_only and tx in by_key:
                del by_key[tx]
                stored_tx_only.discard(tx)
            by_key[f"{tx}:{li}"] = t

    merged = sorted(by_key.values(), key=lambda x: x.get("timestamp", 0))

    # Filter by start date
    start_ts = _parse_start_ts()
    if start_ts:
        merged = [t for t in merged if t.get("timestamp", 0) >= start_ts]

    # Overwrite cache
    try:
        atomic_json_write(ONCHAIN_TRADES_FILE, merged)
    except Exception as e:
        logger.warning(f"Failed to save onchain trades: {e}")

    logger.info(f"On-chain trades: {len(fresh)} fetched, {len(merged)} total stored")
    return merged, all_created_orders


# ─────────────────────────────────────────────────────────────────────
# Bitunix closed-trade fetch
# ─────────────────────────────────────────────────────────────────────

async def _fetch_bitunix_trades(bitunix_client, open_position_ids: set = None) -> tuple:
    """Fetch closed Bitunix positions from API.

    Returns:
        (trades: list[TradeRecord], success: bool) — success=False means API failed,
        success=True means API responded (even if 0 trades returned).
    """
    from analytics import TradeRecord

    try:
        positions = await asyncio.to_thread(
            bitunix_client.get_history_positions, None, 50
        )
        tpsl = await asyncio.to_thread(
            bitunix_client.get_history_tpsl_orders, None, 100
        )
        logger.info(f"Bitunix API: {len(positions)} closed positions, {len(tpsl)} TP/SL orders")
    except Exception as e:
        logger.warning(f"Failed to fetch Bitunix trade history: {e}")
        return [], False

    start_ts = _parse_start_ts()
    trades = []

    for p in positions:
        try:
            pos_id = p.get("positionId", "")
            if open_position_ids and str(pos_id) in open_position_ids:
                continue

            symbol_raw = p.get("symbol", "")
            symbol = symbol_raw.replace("USDT", "").replace("-", "")

            side = p.get("side", "").upper()
            if side in ("BUY", "LONG"):
                side = "LONG"
            elif side in ("SELL", "SHORT"):
                side = "SHORT"

            entry_price = float(p.get("avgOpenPrice", 0) or p.get("entryPrice", 0) or p.get("openPrice", 0) or 0)
            exit_price = float(p.get("avgClosePrice", 0) or p.get("closePrice", 0) or 0)
            leverage = float(p.get("leverage", 0) or 0)
            qty = float(p.get("qty", 0) or p.get("volume", 0) or 0)
            size_usd = entry_price * qty if entry_price > 0 and qty > 0 else 0

            pnl_usd = float(p.get("realizedPNL", 0) or p.get("realizedPnl", 0) or p.get("profit", 0) or 0)

            close_ts = int(p.get("mtime", 0) or p.get("closeTime", 0) or 0)
            open_ts = int(p.get("ctime", 0) or p.get("openTime", 0) or 0)
            if close_ts > 1e12:
                close_ts = close_ts // 1000
            if open_ts > 1e12:
                open_ts = open_ts // 1000

            if close_ts < start_ts:
                continue

            collateral = size_usd / leverage if leverage > 0 else size_usd
            pnl_pct = (pnl_usd / collateral * 100) if collateral > 0 else 0

            # TP/SL orders for this position
            pos_id = p.get("positionId", "")
            all_tp_orders = []
            sl_price_val = None
            for order in tpsl:
                if order.get("positionId") != pos_id:
                    continue
                if order.get("tpPrice") is not None:
                    all_tp_orders.append((float(order["tpPrice"]), order.get("status", "").upper()))
                elif order.get("slPrice") is not None and sl_price_val is None:
                    if order.get("status", "").upper() in ("TRIGGERED", "FILLED", "EXECUTED"):
                        sl_price_val = float(order["slPrice"])

            if side == "LONG":
                all_tp_orders.sort(key=lambda x: x[0])
            else:
                all_tp_orders.sort(key=lambda x: x[0], reverse=True)

            # TP allocation percentages from env
            total_tps = len(all_tp_orders)
            tp_allocs = []
            if total_tps >= 3:
                for tp_idx in range(1, total_tps + 1):
                    env_key = f"BX_TP_{total_tps}_{tp_idx}"
                    alloc = float(os.getenv(env_key, 0))
                    if alloc == 0:
                        env_key = f"TP_{total_tps}_{tp_idx}"
                        alloc = float(os.getenv(env_key, 0))
                    tp_allocs.append(alloc)
            if not tp_allocs or sum(tp_allocs) == 0:
                n_fills = total_tps + (1 if sl_price_val is not None else 0)
                if n_fills == 0:
                    n_fills = 1
                tp_allocs = [100 / n_fills] * total_tps

            # Build tp_details for filled TPs
            tp_hits = 0
            tp_details = []
            for idx, (tp_p, tp_status) in enumerate(all_tp_orders):
                if tp_status not in ("TRIGGERED", "FILLED", "EXECUTED"):
                    continue
                tp_hits += 1
                pct = tp_allocs[idx] if idx < len(tp_allocs) else 0
                tp_size = size_usd * pct / 100
                if side == "LONG":
                    tp_pnl = (tp_p - entry_price) / entry_price * tp_size if entry_price > 0 else 0
                else:
                    tp_pnl = (entry_price - tp_p) / entry_price * tp_size if entry_price > 0 else 0
                tp_details.append({"price": tp_p, "pct": pct, "pnl": tp_pnl})

            # SL details
            sl_details = None
            if sl_price_val is not None:
                sl_pct = 100 - sum(tp.get("pct", 0) for tp in tp_details)
                sl_size = size_usd * sl_pct / 100
                if side == "LONG":
                    sl_pnl = (sl_price_val - entry_price) / entry_price * sl_size if entry_price > 0 else 0
                else:
                    sl_pnl = (entry_price - sl_price_val) / entry_price * sl_size if entry_price > 0 else 0
                sl_details = {"price": sl_price_val, "pct": sl_pct, "pnl": sl_pnl}
            elif not tp_details:
                if side == "LONG":
                    sl_pnl = (exit_price - entry_price) / entry_price * size_usd if entry_price > 0 else 0
                else:
                    sl_pnl = (entry_price - exit_price) / entry_price * size_usd if entry_price > 0 else 0
                sl_details = {"price": exit_price, "pct": 100, "pnl": sl_pnl}

            # Scale PnLs to match actual realized PnL (accounts for fees)
            raw_sum = sum(tp.get("pnl", 0) for tp in tp_details) + (sl_details.get("pnl", 0) if sl_details else 0)
            if abs(raw_sum) > 0.01 and abs(pnl_usd) > 0.01:
                scale = pnl_usd / raw_sum
                for tp in tp_details:
                    tp["pnl"] = tp["pnl"] * scale
                if sl_details:
                    sl_details["pnl"] = sl_details["pnl"] * scale

            duration_hours = (close_ts - open_ts) / 3600 if close_ts > open_ts else 0

            # Unfilled (cancelled) TP orders
            unfilled_targets = []
            for tp_p, tp_status in all_tp_orders:
                if tp_status in ("SYSTEM_CANCELED", "CANCELED", "CANCELLED"):
                    unfilled_targets.append({"price": tp_p})

            if tp_hits > 0:
                exit_reason = f"tp_hit_x{tp_hits}"
            elif sl_price_val is not None:
                exit_reason = "sl_triggered"
            else:
                exit_reason = "exchange_closed"

            trades.append(TradeRecord(
                id=f"bx_{pos_id}",
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                size_usd=size_usd,
                leverage=leverage,
                duration_hours=duration_hours,
                pnl_usd=pnl_usd,
                pnl_percentage=pnl_pct,
                exit_reason=exit_reason,
                opened_at=open_ts,
                closed_at=close_ts,
                exchange="bitunix",
                tp_hits=tp_hits,
                tp_details=tp_details,
                sl_details=sl_details,
                unfilled_targets=unfilled_targets if unfilled_targets else None,
            ))
        except Exception as e:
            logger.warning(f"Skipping Bitunix position: {e}")
            continue

    logger.info(f"Parsed {len(trades)} Bitunix trade(s) from API (after date filter)")
    return trades, True


# ─────────────────────────────────────────────────────────────────────
# Public: rebuild_all_trades
# ─────────────────────────────────────────────────────────────────────

async def rebuild_all_trades(
    w3,
    wallets: List[Tuple[int, Any]],
    markets: dict,
    bitunix_client=None,
    open_positions: Optional[dict] = None,
) -> list:
    """Rebuild complete closed-trade list from live sources.

    Queries on-chain RPC + Bitunix API, overwrites onchain_trades.json
    and trade_history.json with fresh data.

    Args:
        w3: Web3 instance
        wallets: list of (wallet_id, account) tuples
        markets: dict of {symbol: market_address}
        bitunix_client: optional BitunixClient instance
        open_positions: dict of open Position objects (to exclude from closed trades)

    Returns:
        Sorted list of TradeRecord (GMX + Bitunix)
    """
    from analytics import TradeRecord

    async with _rebuild_lock:
        return await _rebuild_all_trades_inner(w3, wallets, markets, bitunix_client, open_positions)


async def _rebuild_all_trades_inner(w3, wallets, markets, bitunix_client, open_positions):
    from analytics import TradeRecord

    # Step 1: Fetch + merge on-chain events
    on_chain, all_created_orders = await _fetch_and_merge_onchain(w3, wallets)

    if not on_chain and not bitunix_client:
        logger.info("Rebuild: no on-chain events and no Bitunix client")
        return []

    # Step 2: Build market → symbol map
    market_to_sym = {addr.lower(): sym for sym, addr in markets.items()}

    # Step 3: Identify open positions to exclude (by market, direction, AND opened_at)
    open_keys = set()
    if open_positions:
        for pos in open_positions.values():
            if getattr(pos, 'is_open', False) and getattr(pos, 'market_addr', None):
                is_long = getattr(pos, 'side', '') == "LONG"
                opened_at = int(getattr(pos, 'opened_at', 0) or 0)
                open_keys.add((pos.market_addr.lower(), is_long, opened_at))

    # Step 4: Build rich GMX trades
    trade_history = []
    if on_chain:
        rich_trades = build_rich_trades(on_chain, all_created_orders, open_keys, market_to_sym)
        for t in rich_trades:
            try:
                trade_history.append(TradeRecord(
                    id=f"rebuild_{t['symbol']}_{t['side']}_{int(t['opened_at'])}",
                    symbol=t["symbol"],
                    side=t["side"],
                    entry_price=t["entry_price"],
                    exit_price=t["exit_price"],
                    size_usd=t["size_usd"],
                    leverage=t["leverage"],
                    duration_hours=t["duration_hours"],
                    pnl_usd=t["pnl_usd"],
                    pnl_percentage=t["pnl_percentage"],
                    exit_reason="on-chain",
                    opened_at=t["opened_at"],
                    closed_at=t["closed_at"],
                    exchange="gmx",
                    tp_hits=t["tp_hits"],
                    tp_details=t["tp_details"],
                    sl_details=t["sl_details"],
                    unfilled_targets=t["unfilled_targets"] or None,
                ))
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(f"Skipping malformed GMX trade: {e}")

    gmx_count = len(trade_history)

    # Step 5: Fetch Bitunix closed trades (exclude still-open positions)
    open_bx_position_ids = set()
    if open_positions:
        for pos in open_positions.values():
            if getattr(pos, 'exchange', '') == 'bitunix' and getattr(pos, 'is_open', False):
                bx_id = getattr(pos, 'bitunix_position_id', None)
                if bx_id:
                    open_bx_position_ids.add(str(bx_id))

    bx_count = 0
    bx_success = False
    if bitunix_client:
        bx_trades, bx_success = await _fetch_bitunix_trades(bitunix_client, open_bx_position_ids)
        trade_history.extend(bx_trades)
        bx_count = len(bx_trades)

    # Step 6: Preserve existing trades that couldn't be re-fetched
    #   - Bitunix trades when Bitunix API failed (not just 0 results)
    #   - Live-recorded trades (UUID IDs, not "rebuild_" or "bx_" prefixed)
    fresh_keys = {(t.exchange, t.symbol, t.side, int(t.opened_at)) for t in trade_history}
    fresh_ids = {t.id for t in trade_history}
    preserved = 0

    existing_data = safe_json_read(TRADE_HISTORY_FILE, default=[])
    if existing_data:
        valid_fields = {f.name for f in fields(TradeRecord)}
        for record in existing_data:
            rec_id = record.get("id", "")
            if rec_id in fresh_ids:
                continue
            rec_key = (record.get("exchange", ""), record.get("symbol", ""),
                       record.get("side", ""), int(record.get("opened_at", 0)))
            if rec_key in fresh_keys:
                continue  # Same trade re-fetched under a different ID

            is_bitunix = record.get("exchange") == "bitunix"
            bitunix_api_failed = bitunix_client is None or not bx_success
            is_live_recorded = not rec_id.startswith("rebuild_") and not rec_id.startswith("bx_")

            if (is_bitunix and bitunix_api_failed) or is_live_recorded:
                try:
                    filtered = {k: v for k, v in record.items() if k in valid_fields}
                    trade_history.append(TradeRecord(**filtered))
                    preserved += 1
                except Exception as e:
                    logger.warning(f"Skipping preserved trade {rec_id}: {e}")

    # Step 7: Sort and persist
    trade_history.sort(key=lambda t: t.closed_at)

    try:
        data = [asdict(t) for t in trade_history]
        atomic_json_write(TRADE_HISTORY_FILE, data)
    except Exception as e:
        logger.error(f"Failed to save trade history: {e}", exc_info=True)

    logger.info(f"Rebuild complete: {gmx_count} GMX + {bx_count} Bitunix + {preserved} preserved trade(s)")
    return trade_history


# ─────────────────────────────────────────────────────────────────────
# Public: rebuild_open_positions
# ─────────────────────────────────────────────────────────────────────

async def rebuild_open_positions(
    w3,
    wallets: List[Tuple[int, Any]],
    positions: dict,
    markets: dict,
    bitunix_client=None,
) -> dict:
    """Refresh open-position state from live on-chain data.

    For each open GMX position, queries PositionDecrease events to find
    TP fills that may have been missed (e.g. during bot downtime).
    Updates verified_decreases and returns the corrected positions dict.

    Does NOT overwrite position_state.json directly — the caller (bot)
    handles persistence via _save_position_state().

    Args:
        w3: Web3 instance
        wallets: list of (wallet_id, account) tuples
        positions: dict of Position objects (keyed by pos.id)
        markets: dict of {symbol: market_address}
        bitunix_client: optional BitunixClient (for Bitunix TP verification)

    Returns:
        dict of corrections made: {pos_id: {"new_tp_fills": int, "verified_decreases": list}}
    """
    from history import fetch_recent_position_decreases

    corrections = {}
    market_to_sym = {addr.lower(): sym for sym, addr in markets.items()}

    for pos in list(positions.values()):
        if not getattr(pos, 'is_open', False):
            continue
        exchange = getattr(pos, 'exchange', 'gmx')

        if exchange == 'gmx' and getattr(pos, 'market_addr', None):
            # Query on-chain for all decrease events on this position
            is_long = pos.side == "LONG"
            wid = getattr(pos, 'wallet_id', 1) or 1

            # Find the wallet account
            acct = None
            for w_id, w_acct in wallets:
                if w_id == wid:
                    acct = w_acct
                    break
            if not acct:
                continue

            try:
                decreases = await asyncio.to_thread(
                    fetch_recent_position_decreases,
                    w3, acct.address, pos.market_addr, is_long,
                    lookback_seconds=7200  # 2 hours
                )
            except Exception as e:
                logger.warning(f"Failed to fetch decreases for {pos.symbol} {pos.side}: {e}")
                continue

            if not decreases:
                continue

            # Compare with existing verified_decreases
            existing_txs = set()
            for vd in getattr(pos, 'verified_decreases', []) or []:
                tx = vd.get("tx_hash", "")
                li = vd.get("log_index")
                existing_txs.add(f"{tx}:{li}" if li is not None else tx)

            new_fills = []
            for d in decreases:
                tx = d.get("tx_hash", "")
                li = d.get("log_index")
                key = f"{tx}:{li}" if li is not None else tx
                if key not in existing_txs:
                    new_fills.append(d)

            if new_fills:
                if not hasattr(pos, 'verified_decreases') or pos.verified_decreases is None:
                    pos.verified_decreases = []
                pos.verified_decreases.extend(new_fills)
                corrections[pos.id] = {
                    "new_tp_fills": len(new_fills),
                    "verified_decreases": pos.verified_decreases,
                }
                logger.info(
                    f"Rebuild: {pos.symbol} {pos.side} [W{wid}] found {len(new_fills)} "
                    f"new decrease event(s) (total: {len(pos.verified_decreases)})"
                )

        elif exchange == 'bitunix' and bitunix_client:
            # Query Bitunix API for TP/SL order history
            try:
                symbol_raw = getattr(pos, 'bitunix_symbol', None) or f"{pos.symbol}USDT"
                tpsl_history = await asyncio.to_thread(
                    bitunix_client.get_history_tpsl_orders, symbol_raw, 100
                )

                bx_pos_id = getattr(pos, 'bitunix_position_id', None)
                if not bx_pos_id:
                    continue

                existing_vd = getattr(pos, 'verified_decreases', []) or []
                existing_order_ids = {vd.get("orderId") for vd in existing_vd if vd.get("orderId")}

                new_fills = []
                for order in tpsl_history:
                    if order.get("positionId") != bx_pos_id:
                        continue
                    if order.get("status", "").upper() not in ("TRIGGERED", "FILLED", "EXECUTED"):
                        continue
                    oid = order.get("orderId", "")
                    if oid in existing_order_ids:
                        continue
                    # This is a newly discovered fill
                    tp_price = order.get("tpPrice")
                    sl_price = order.get("slPrice")
                    if tp_price is not None:
                        new_fills.append({
                            "orderId": oid,
                            "type": "tp",
                            "price": float(tp_price),
                            "status": order.get("status", ""),
                        })
                    elif sl_price is not None:
                        new_fills.append({
                            "orderId": oid,
                            "type": "sl",
                            "price": float(sl_price),
                            "status": order.get("status", ""),
                        })

                if new_fills:
                    if not hasattr(pos, 'verified_decreases') or pos.verified_decreases is None:
                        pos.verified_decreases = []
                    pos.verified_decreases.extend(new_fills)
                    corrections[pos.id] = {
                        "new_tp_fills": len(new_fills),
                        "verified_decreases": pos.verified_decreases,
                    }
                    logger.info(
                        f"Rebuild: {pos.symbol} {pos.side} [Bitunix] found {len(new_fills)} "
                        f"new TP/SL fill(s)"
                    )
            except Exception as e:
                logger.warning(f"Failed to check Bitunix fills for {pos.symbol}: {e}")

    if corrections:
        logger.info(f"Rebuild open positions: {len(corrections)} position(s) updated")
    else:
        logger.info("Rebuild open positions: all positions up to date")

    return corrections
