#!/usr/bin/env python3
"""
Diagnostic: Query BOTH GMX on-chain + Bitunix API and show what /positions
would display. Sends each position as a separate message to Telegram admin.

NO TRADES ARE PLACED. Read-only queries only.
"""

import os
import asyncio
import logging
import time

from dotenv import load_dotenv
load_dotenv()

from web3 import Web3
from eth_account import Account

from bitunix_api import BitunixClient
from close import fetch_positions as gmx_fetch_positions
from open import fetch_open_orders, ORDER_TYPE_LIMIT_DECREASE, ORDER_TYPE_STOP_LOSS_DECREASE
from history import fetch_recent_position_decreases
from risk import verify_tp_hit_by_price
import bot_api

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("diagnose")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT = os.getenv("ADMIN_CHAT_ID")
BX_KEY = os.getenv("BITUNIX_API_KEY")
BX_SECRET = os.getenv("BITUNIX_SECRET_KEY")
RPC_URL = os.getenv("RPC_URL", "https://arb1.arbitrum.io/rpc")

PRIVATE_KEYS = [
    os.getenv("PRIVATE_KEY"),
    os.getenv("PRIVATE_KEY_2"),
    os.getenv("PRIVATE_KEY_3"),
    os.getenv("PRIVATE_KEY_4"),
]

MARKETS = {
    "BTC": os.getenv("GMX_V2_MARKET_BTC", os.getenv("GMX_V2_MARKET", "0x47c031236e19d024b42f8ae6780e44a573170703")),
    "ETH": os.getenv("GMX_V2_MARKET_ETH", "0x70d95587d40A2caf56bd97485aB3Eec10Bee6336"),
    "SOL": os.getenv("GMX_V2_MARKET_SOL", "0x09400D9DB990D5ed3f35D7be61DfAEB900Af03C9"),
}
MARKET_TO_SYM = {v.lower(): k for k, v in MARKETS.items()}


def _fmt_sign(v):
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.2f}"


async def send_msg(text):
    """Send to Telegram and print."""
    print(text)
    print()
    await bot_api.send_admin_message(BOT_TOKEN, ADMIN_CHAT, text)


async def diagnose_gmx():
    """Query GMX on-chain positions + orders. Returns list of per-position messages + summary."""
    messages = []

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        return ["ERROR: Cannot connect to Arbitrum RPC"]

    pos_num = 0
    total_unrealized = 0.0
    total_realized = 0.0

    for idx, pk in enumerate(PRIVATE_KEYS):
        if not pk:
            continue
        acct = Account.from_key(pk)
        wallet_id = idx + 1

        try:
            positions = gmx_fetch_positions(w3, acct.address)
        except Exception as e:
            messages.append(f"W{wallet_id}: fetch failed: {e}")
            continue

        if not positions:
            continue

        try:
            orders = fetch_open_orders(w3, acct.address)
        except Exception as e:
            orders = []
            log.warning(f"W{wallet_id} orders fetch failed: {e}")

        for pos in positions:
            pos_num += 1
            sym = pos.symbol.upper().split("/")[0]
            side = "LONG" if pos.is_long else "SHORT"
            collateral = pos.collateral_amount
            leverage = pos.leverage
            entry = pos.entry_price
            current = pos.current_price

            pnl = pos.net_pnl_usd if pos.pnl_source == "onchain" else pos.unrealized_pnl
            total_unrealized += pnl
            pnl_pct = (pnl / collateral * 100) if collateral > 0 else 0.0
            price_chg = ((current - entry) / entry * 100) if entry > 0 else 0.0

            msg = f"**#{pos_num} {sym} {side} GMX | ${collateral:,.2f} @{leverage:.1f}x**\n{'=' * 37}\n"
            msg += f"Entry: ${entry:,.2f} - Current: ${current:,.2f} ({price_chg:+.1f}%)\n"

            # Fetch executed TPs from PositionDecrease events
            market_lower = pos.market.lower()
            executed_tps = []
            realized_pnl = 0.0
            try:
                decreases = fetch_recent_position_decreases(
                    w3, acct.address, pos.market, pos.is_long,
                    lookback_seconds=86400,
                )
                for d in decreases:
                    ot = d.get("order_type")
                    trigger = d.get("trigger_price", 0)
                    exec_price = d.get("execution_price", 0)
                    size_delta = d.get("size_delta_usd", 0)
                    pnl_usd = d.get("net_pnl_usd", 0)
                    is_tp = (ot == ORDER_TYPE_LIMIT_DECREASE)
                    if not is_tp and trigger > 0:
                        if pos.is_long and trigger > entry:
                            is_tp = True
                        elif not pos.is_long and trigger < entry:
                            is_tp = True
                    if is_tp and (trigger > 0 or exec_price > 0):
                        executed_tps.append({
                            "price": trigger if trigger > 0 else exec_price,
                            "size_usd": size_delta,
                            "pnl": pnl_usd,
                        })
                        realized_pnl += pnl_usd
            except Exception as e:
                log.warning(f"W{wallet_id} {sym}: PositionDecrease fetch failed: {e}")

            total_realized += realized_pnl

            msg += f"Realized: {_fmt_sign(realized_pnl)} - Unrealized: {_fmt_sign(pnl)} ({pnl_pct:+.0f}%)\n"

            # Hit targets from chain history
            executed_tps.sort(key=lambda t: t["price"], reverse=not pos.is_long)
            original_size = pos.size_usd + sum(et["size_usd"] for et in executed_tps)
            tp_num = 0

            for et in executed_tps:
                tp_num += 1
                pnl_str = f" {_fmt_sign(et['pnl'])}" if et["pnl"] != 0 else ""
                pct_str = ""
                if original_size > 0:
                    pct = et["size_usd"] / original_size * 100
                    pct_str = f" ({pct:.1f}%)"
                msg += f"  Target {tp_num}: ${et['price']:,.2f}{pct_str}{pnl_str} ✅\n"

            # Pending TP orders
            tp_orders = sorted(
                [o for o in orders
                 if o["market"].lower() == market_lower
                 and o["order_type"] == ORDER_TYPE_LIMIT_DECREASE],
                key=lambda o: o.get("trigger_price", 0),
                reverse=not pos.is_long,
            )

            for o in tp_orders:
                tp_num += 1
                tp_price = o.get("trigger_price", 0)
                tp_size = o.get("size_usd", 0)
                pct_str = ""
                if original_size > 0:
                    pct = tp_size / original_size * 100
                    pct_str = f" ({pct:.1f}%)"
                if tp_price and entry > 0:
                    if pos.is_long:
                        proj = (tp_price - entry) / entry * tp_size
                    else:
                        proj = (entry - tp_price) / entry * tp_size
                    msg += f"  Target {tp_num}: ${tp_price:,.2f}{pct_str} {_fmt_sign(proj)}\n"
                else:
                    msg += f"  Target {tp_num}: ${tp_price:,.2f}{pct_str}\n"

            # SL orders
            sl_orders = [
                o for o in orders
                if o["market"].lower() == market_lower
                and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
            ]
            for sl in sl_orders[:1]:
                sl_price = sl.get("trigger_price", 0)
                sl_size = sl.get("size_usd", 0)
                if sl_price and entry > 0:
                    if pos.is_long:
                        sl_proj = (sl_price - entry) / entry * sl_size
                    else:
                        sl_proj = (entry - sl_price) / entry * sl_size
                    sl_label = ""
                    if abs(sl_price - entry) / entry < 0.003:
                        sl_label = " (Entry)"
                    msg += f"  Stop Loss: ${sl_price:,.2f}{sl_label} ({_fmt_sign(sl_proj)})\n"
                else:
                    msg += f"  Stop Loss: ${sl_price:,.2f}\n"

            hit_count = len(executed_tps)
            pending_count = len(tp_orders)
            msg += f"  [{hit_count} target(s) hit, {pending_count} pending]"

            messages.append(msg)

    if pos_num == 0:
        messages.append("No open GMX positions found on-chain.")
    else:
        # Append total to last position message
        summary = f"\n\n**Total** | Realized: {_fmt_sign(total_realized)} | Unrealized: {_fmt_sign(total_unrealized)}"
        messages[-1] += summary

    return messages


async def diagnose_bitunix():
    """Query Bitunix API positions + TP/SL orders. Returns list of per-position messages + summary."""
    messages = []

    if not BX_KEY or not BX_SECRET:
        return ["Bitunix credentials not configured."]

    client = BitunixClient(BX_KEY, BX_SECRET)
    positions = client.get_pending_positions()

    if not positions:
        return ["No open Bitunix positions."]

    total_unrealized = 0.0
    total_realized = 0.0

    for i, p in enumerate(positions, 1):
        symbol_raw = p.get("symbol", "?")
        symbol = symbol_raw.replace("USDT", "")
        if symbol.startswith("1000"):
            symbol = symbol[4:]
        raw_side = (p.get("side") or "").upper()
        side = "LONG" if raw_side in ("BUY", "LONG") else "SHORT"
        position_id = p.get("positionId", "?")
        entry = float(p.get("avgOpenPrice", 0))
        leverage = float(p.get("leverage", 1))
        margin = float(p.get("margin", 0))
        size_usd = margin * leverage
        total_qty = float(p.get("qty", 0))

        try:
            current_price = client.get_current_price(symbol_raw)
        except Exception:
            current_price = 0.0

        collateral = margin
        is_long = side == "LONG"

        # Fetch TP/SL orders
        try:
            pending_tpsl = client.get_pending_tpsl_orders(symbol_raw)
        except Exception:
            pending_tpsl = []
        try:
            history_tpsl = client.get_history_tpsl_orders(symbol_raw, 200)
        except Exception:
            history_tpsl = []

        my_pending = [o for o in pending_tpsl if o.get("positionId") == position_id]
        my_history = [o for o in history_tpsl if o.get("positionId") == position_id]

        # Build unified target list with actual quantities from exchange
        targets = []
        seen_prices = set()
        original_qty = total_qty

        for o in my_history:
            status = (o.get("status") or "").upper()
            if status in ("SYSTEM_CANCELED", "CANCELED"):
                continue
            tp_price = float(o.get("tpPrice") or o.get("triggerPrice") or 0)
            tp_qty = float(o.get("tpQty") or 0)
            if tp_price > 0 and round(tp_price, 2) not in seen_prices:
                original_qty += tp_qty
                targets.append({
                    "price": tp_price, "hit": True, "qty": tp_qty, "status": status,
                })
                seen_prices.add(round(tp_price, 2))

        for o in my_pending:
            tp_price = float(o.get("tpPrice") or o.get("triggerPrice") or 0)
            tp_qty = float(o.get("tpQty") or 0)
            if tp_price > 0 and round(tp_price, 2) not in seen_prices:
                targets.append({
                    "price": tp_price, "hit": False, "qty": tp_qty, "status": "PENDING",
                })
                seen_prices.add(round(tp_price, 2))

        targets.sort(key=lambda t: t["price"], reverse=not is_long)

        # Unrealized PnL on remaining position
        unrealized_pnl = 0.0
        if entry > 0 and current_price > 0 and total_qty > 0:
            remaining_notional = total_qty * entry
            if is_long:
                unrealized_pnl = (current_price - entry) / entry * remaining_notional
            else:
                unrealized_pnl = (entry - current_price) / entry * remaining_notional

        total_unrealized += unrealized_pnl
        pnl_pct = (unrealized_pnl / collateral * 100) if collateral > 0 else 0.0
        price_chg = ((current_price - entry) / entry * 100) if entry > 0 else 0.0

        msg = f"**#{i} {symbol} {side} BITUNIX | ${collateral:,.2f} @{leverage:.0f}x**\n{'=' * 37}\n"
        msg += f"Entry: ${entry:,.2f} - Current: ${current_price:,.2f} ({price_chg:+.1f}%)\n"

        # Realized PnL from hit targets
        realized_pnl = 0.0
        for t in targets:
            if not t["hit"]:
                continue
            tp_notional = t["qty"] * entry if entry > 0 else 0
            if entry > 0:
                if is_long:
                    tp_pnl = (t["price"] - entry) / entry * tp_notional
                else:
                    tp_pnl = (entry - t["price"]) / entry * tp_notional
            else:
                tp_pnl = 0
            realized_pnl += tp_pnl
        total_realized += realized_pnl

        msg += f"Realized: {_fmt_sign(realized_pnl)} - Unrealized: {_fmt_sign(unrealized_pnl)} ({pnl_pct:+.0f}%)\n"

        # Display targets
        for j, t in enumerate(targets, 1):
            tp_pct = (t["qty"] / original_qty * 100) if original_qty > 0 else 0
            tp_notional = t["qty"] * entry if entry > 0 else 0

            if t["hit"]:
                if entry > 0:
                    if is_long:
                        tp_pnl = (t["price"] - entry) / entry * tp_notional
                    else:
                        tp_pnl = (entry - t["price"]) / entry * tp_notional
                else:
                    tp_pnl = 0
                msg += f"  Target {j}: ${t['price']:,.2f} ({tp_pct:.1f}%) {_fmt_sign(tp_pnl)} ✅\n"
            else:
                if entry > 0:
                    if is_long:
                        proj = (t["price"] - entry) / entry * tp_notional
                    else:
                        proj = (entry - t["price"]) / entry * tp_notional
                    msg += f"  Target {j}: ${t['price']:,.2f} ({tp_pct:.1f}%) {_fmt_sign(proj)}\n"
                else:
                    msg += f"  Target {j}: ${t['price']:,.2f} ({tp_pct:.1f}%)\n"

        # SL from pending orders
        for o in my_pending:
            sl_price = float(o.get("slPrice") or 0)
            if sl_price > 0:
                if entry > 0:
                    if is_long:
                        sl_proj = (sl_price - entry) / entry * size_usd
                    else:
                        sl_proj = (entry - sl_price) / entry * size_usd
                    sl_label = ""
                    if abs(sl_price - entry) / entry < 0.003:
                        sl_label = " (Entry)"
                    msg += f"  Stop Loss: ${sl_price:,.2f}{sl_label} ({_fmt_sign(sl_proj)})\n"
                else:
                    msg += f"  Stop Loss: ${sl_price:,.2f}\n"
                break

        hit_count = sum(1 for t in targets if t["hit"])
        pending_count = sum(1 for t in targets if not t["hit"])
        msg += f"  [{hit_count} target(s) hit, {pending_count} pending]"

        messages.append(msg)

    # Append total to last position message
    summary = f"\n\n**Total** | Realized: {_fmt_sign(total_realized)} | Unrealized: {_fmt_sign(total_unrealized)}"
    messages[-1] += summary

    return messages


async def diagnose_pnl():
    """Simulate /pnl — query on-chain closed trades + Bitunix history + open unrealized."""
    from collections import defaultdict
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from history import fetch_trade_history
    from state_io import safe_json_read

    ET = ZoneInfo("America/New_York")
    now = datetime.now(ET)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_cutoff = int(today_start.timestamp())
    now_ts = int(time.time())
    month_cutoff = now_ts - 30 * 86400

    TRADE_START_DATE = "2026-03-09T01:00"
    try:
        fmt = "%Y-%m-%dT%H:%M" if "T" in TRADE_START_DATE else "%Y-%m-%d"
        start_ts = int(datetime.strptime(TRADE_START_DATE, fmt).timestamp())
    except Exception:
        start_ts = 0

    w3 = Web3(Web3.HTTPProvider(RPC_URL))

    # ── Fetch on-chain trades across all wallets ──
    all_trades = []
    for idx, pk in enumerate(PRIVATE_KEYS):
        if not pk:
            continue
        acct = Account.from_key(pk)
        try:
            trades, _ = fetch_trade_history(w3, acct.address)
            all_trades.extend(trades)
        except Exception as e:
            log.warning(f"W{idx+1} trade fetch failed: {e}")

    # Filter by start date
    all_trades = [t for t in all_trades if t.get("timestamp", 0) >= start_ts]

    # ── Find currently open positions (to exclude from closed PnL) ──
    open_keys = set()
    open_unrealized = defaultdict(float)
    open_realized = defaultdict(float)  # from TP hits on open positions

    for idx, pk in enumerate(PRIVATE_KEYS):
        if not pk:
            continue
        acct = Account.from_key(pk)
        try:
            positions = gmx_fetch_positions(w3, acct.address)
            for pos in positions:
                sym = pos.symbol.upper().split("/")[0]
                is_long = pos.is_long
                open_keys.add((pos.market.lower(), is_long))
                pnl = pos.net_pnl_usd if pos.pnl_source == "onchain" else pos.unrealized_pnl
                open_unrealized[sym] += pnl

                # Get realized from TP hits on this open position
                try:
                    decreases = fetch_recent_position_decreases(
                        w3, acct.address, pos.market, pos.is_long,
                        lookback_seconds=86400,
                    )
                    for d in decreases:
                        ot = d.get("order_type")
                        trigger = d.get("trigger_price", 0)
                        is_tp = (ot == ORDER_TYPE_LIMIT_DECREASE)
                        if not is_tp and trigger > 0:
                            entry = pos.entry_price
                            if is_long and trigger > entry:
                                is_tp = True
                            elif not is_long and trigger < entry:
                                is_tp = True
                        if is_tp:
                            open_realized[sym] += d.get("net_pnl_usd", 0)
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"W{idx+1} position fetch failed: {e}")

    # ── Group closed on-chain trades ──
    groups = defaultdict(list)
    for t in all_trades:
        key = (t.get("market_address", "").lower(), t.get("is_long", True))
        groups[key].append(t)

    closed_trades = []
    for (market, is_long), events in groups.items():
        if (market, is_long) in open_keys:
            continue
        sym = MARKET_TO_SYM.get(market)
        if not sym:
            continue
        total_pnl = sum(e.get("pnl_usd", 0) for e in events)
        last_ts = max(e.get("timestamp", 0) for e in events)
        closed_trades.append({"sym": sym, "pnl": total_pnl, "timestamp": last_ts})

    # ── Bitunix trades from history API ──
    bx_closed_trades = []
    bx_open_unrealized = 0.0
    bx_open_realized = 0.0
    bx_open_count = 0

    if BX_KEY and BX_SECRET:
        client = BitunixClient(BX_KEY, BX_SECRET)

        # Open Bitunix positions
        bx_positions = client.get_pending_positions()
        bx_open_count = len(bx_positions)
        for p in bx_positions:
            entry = float(p.get("avgOpenPrice", 0))
            qty = float(p.get("qty", 0))
            margin = float(p.get("margin", 0))
            symbol_raw = p.get("symbol", "")
            position_id = p.get("positionId", "?")
            raw_side = (p.get("side") or "").upper()
            is_long = raw_side in ("BUY", "LONG")

            try:
                current = client.get_current_price(symbol_raw)
            except Exception:
                current = 0.0

            if entry > 0 and current > 0 and qty > 0:
                notional = qty * entry
                if is_long:
                    bx_open_unrealized += (current - entry) / entry * notional
                else:
                    bx_open_unrealized += (entry - current) / entry * notional

            # TP hits on open position
            try:
                history_tpsl = client.get_history_tpsl_orders(symbol_raw, 200)
                my_history = [o for o in history_tpsl if o.get("positionId") == position_id]
                for o in my_history:
                    status = (o.get("status") or "").upper()
                    if status in ("SYSTEM_CANCELED", "CANCELED"):
                        continue
                    tp_price = float(o.get("tpPrice") or 0)
                    tp_qty = float(o.get("tpQty") or 0)
                    if tp_price > 0 and tp_qty > 0 and entry > 0:
                        tp_notional = tp_qty * entry
                        if is_long:
                            bx_open_realized += (tp_price - entry) / entry * tp_notional
                        else:
                            bx_open_realized += (entry - tp_price) / entry * tp_notional
            except Exception:
                pass

        # Closed Bitunix positions
        try:
            closed_positions = client.get_history_positions(None, 50)
            for p in closed_positions:
                pnl = float(p.get("realizedPNL", 0) or p.get("realizedPnl", 0) or 0)
                closed_ts = float(p.get("ctime", 0) or 0) / 1000  # ms to s
                if closed_ts < start_ts:
                    continue
                if abs(pnl) < 1:
                    continue
                bx_closed_trades.append({"pnl": pnl, "timestamp": closed_ts})
        except Exception as e:
            log.warning(f"Bitunix history fetch failed: {e}")

    # ── Format ──
    def _sign(v):
        return "+" if v >= 0 else ""

    def bucket_stats(trades_list):
        if not trades_list:
            return {"pnl": 0.0, "trades": 0, "wins": 0}
        pnl = sum(t["pnl"] for t in trades_list)
        wins = sum(1 for t in trades_list if t["pnl"] > 0)
        return {"pnl": pnl, "trades": len(trades_list), "wins": wins}

    # GMX section
    msg = f"**PnL Summary**\n{'=' * 37}\n\n"

    # Today
    gmx_today = bucket_stats([t for t in closed_trades if t["timestamp"] >= today_cutoff])
    gmx_month = bucket_stats([t for t in closed_trades if t["timestamp"] >= month_cutoff])
    gmx_all = bucket_stats(closed_trades)
    gmx_unrealized = sum(open_unrealized.values())
    gmx_open_real = sum(open_realized.values())

    msg += "**GMX (on-chain)**\n"

    # Per-symbol for today
    for sym in ("BTC", "ETH"):
        s = bucket_stats([t for t in closed_trades if t["sym"] == sym and t["timestamp"] >= today_cutoff])
        unr = open_unrealized.get(sym, 0.0)
        oreal = open_realized.get(sym, 0.0)
        wr = f"{s['wins']}/{s['trades']}" if s["trades"] else "—"
        line = f"  {sym}: {_sign(s['pnl'])}${s['pnl']:,.2f}  ({wr})"
        if unr != 0:
            line += f"  |  open: {_sign(unr)}${unr:,.2f}"
        msg += line + "\n"

    today_realized = gmx_today["pnl"]
    today_combined = today_realized + gmx_unrealized
    today_wr = f"{gmx_today['wins']}/{gmx_today['trades']}" if gmx_today["trades"] else "—"
    msg += f"  Realized:   {_sign(today_realized)}${today_realized:,.2f}  ({today_wr})\n"
    msg += f"  Unrealized: {_sign(gmx_unrealized)}${gmx_unrealized:,.2f}\n"
    if gmx_open_real:
        msg += f"  Open TP realized: {_sign(gmx_open_real)}${gmx_open_real:,.2f}\n"
    msg += f"  **Today: {_sign(today_combined)}${today_combined:,.2f}**\n"

    # 30d / all time
    for label, stats in [("30 Days", gmx_month), ("All Time", gmx_all)]:
        wr = f"{stats['wins']}/{stats['trades']}" if stats["trades"] else "—"
        wr_pct = f" | {stats['wins']/stats['trades']*100:.0f}% WR" if stats["trades"] else ""
        msg += f"  {label}: {_sign(stats['pnl'])}${stats['pnl']:,.2f}  ({wr}{wr_pct})\n"

    # Bitunix section
    if bx_closed_trades or bx_open_count:
        msg += f"\n**Bitunix**"
        if bx_open_count:
            msg += f" ({bx_open_count} open)"
        msg += "\n"

        bx_today = bucket_stats([t for t in bx_closed_trades if t["timestamp"] >= today_cutoff])
        bx_month = bucket_stats([t for t in bx_closed_trades if t["timestamp"] >= month_cutoff])
        bx_all = bucket_stats(bx_closed_trades)

        bx_today_combined = bx_today["pnl"] + bx_open_unrealized
        bx_today_wr = f"{bx_today['wins']}/{bx_today['trades']}" if bx_today["trades"] else "—"
        msg += f"  Realized:   {_sign(bx_today['pnl'])}${bx_today['pnl']:,.2f}  ({bx_today_wr})\n"
        msg += f"  Unrealized: {_sign(bx_open_unrealized)}${bx_open_unrealized:,.2f}\n"
        if bx_open_realized:
            msg += f"  Open TP realized: {_sign(bx_open_realized)}${bx_open_realized:,.2f}\n"
        msg += f"  **Today: {_sign(bx_today_combined)}${bx_today_combined:,.2f}**\n"

        for label, stats in [("30 Days", bx_month), ("All Time", bx_all)]:
            wr = f"{stats['wins']}/{stats['trades']}" if stats["trades"] else "—"
            wr_pct = f" | {stats['wins']/stats['trades']*100:.0f}% WR" if stats["trades"] else ""
            msg += f"  {label}: {_sign(stats['pnl'])}${stats['pnl']:,.2f}  ({wr}{wr_pct})\n"

    # Combined total
    all_unrealized = gmx_unrealized + bx_open_unrealized
    all_realized_today = gmx_today["pnl"] + (bx_today["pnl"] if bx_closed_trades or bx_open_count else 0)
    all_open_realized = gmx_open_real + bx_open_realized
    grand_total = all_realized_today + all_unrealized + all_open_realized

    msg += f"\n**Combined Today: {_sign(grand_total)}${grand_total:,.2f}**"

    return msg


async def diagnose_trades():
    """Simulate /trades — uses centralized trade_rebuilder for consistent data."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from trade_rebuilder import rebuild_all_trades

    ET = ZoneInfo("America/New_York")
    w3 = Web3(Web3.HTTPProvider(RPC_URL))

    # Build wallets list matching bot format: [(wallet_id, account)]
    wallets = []
    for idx, pk in enumerate(PRIVATE_KEYS):
        if not pk:
            continue
        wallets.append((idx + 1, Account.from_key(pk)))

    # Bitunix client
    bx_client = BitunixClient(BX_KEY, BX_SECRET) if BX_KEY and BX_SECRET else None

    # Use centralized rebuilder (same as bot)
    all_trades = await rebuild_all_trades(
        w3, wallets, MARKETS, bitunix_client=bx_client, open_positions=None,
    )

    # Convert TradeRecords to display dicts
    trades = []
    for t in all_trades:
        if abs(t.pnl_usd) < 1:
            continue
        trades.append({
            "symbol": t.symbol, "side": t.side,
            "exchange": t.exchange.upper() if t.exchange else "GMX",
            "pnl": t.pnl_usd, "size": t.size_usd, "leverage": t.leverage,
            "entry": t.entry_price, "exit": t.exit_price,
            "closed_at": t.closed_at, "duration_h": t.duration_hours,
            "tp_hits": t.tp_hits, "sl_hit": t.sl_details is not None,
            "tp_details": t.tp_details or [], "sl_details": t.sl_details,
            "unfilled_targets": t.unfilled_targets or [], "pnl_pct": t.pnl_percentage,
        })

    if not trades:
        return f"**Trade History**\n{'=' * 37}\n\nNo closed trades found."

    trades.sort(key=lambda x: x["closed_at"], reverse=True)

    msg = f"**Trade History ({len(trades)} trades)**\n{'=' * 37}\n\n"

    for i, t in enumerate(trades[:10], 1):
        result = "WIN" if t["pnl"] > 0 else "LOSS"
        pnl_pct = t.get("pnl_pct", 0)
        if pnl_pct == 0 and t["leverage"] > 0 and t["size"] > 0:
            collateral = t["size"] / t["leverage"]
            pnl_pct = (t["pnl"] / collateral * 100) if collateral > 0 else 0

        msg += f"**{i}. {t['symbol']} {t['side']} {t['exchange']}** — {result}\n"
        msg += f"  PnL: {_fmt_sign(t['pnl'])}"
        if pnl_pct != 0:
            msg += f" ({pnl_pct:+.1f}%)"
        msg += "\n"
        if t["size"] > 0:
            if t["leverage"] > 0:
                msg += f"  Size: ${t['size']:,.2f} @{t['leverage']:.0f}x\n"
            else:
                msg += f"  Size: ${t['size']:,.2f}\n"
        if t["entry"] > 0:
            msg += f"  Entry: ${t['entry']:,.2f} → Exit: ${t['exit']:,.2f}\n"

        # Show individual TP details with prices
        tp_details = t.get("tp_details", [])
        for ti, tp in enumerate(tp_details, 1):
            tp_line = f"  TP{ti}: ${tp['price']:,.2f}"
            if tp.get("pnl"):
                tp_line += f" — {_fmt_sign(tp['pnl'])}"
            if tp.get("pct"):
                tp_line += f" ({tp['pct']:.0f}%)"
            msg += tp_line + "\n"

        # Show SL details
        sl = t.get("sl_details")
        if sl:
            sl_line = f"  SL: ${sl['price']:,.2f}"
            if sl.get("pnl"):
                sl_line += f" — {_fmt_sign(sl['pnl'])}"
            msg += sl_line + "\n"

        # Show unfilled targets
        for uf in t.get("unfilled_targets", []):
            msg += f"  TP (unfilled): ${uf['price']:,.2f}\n"

        if t["duration_h"] >= 24:
            msg += f"  Duration: {t['duration_h']/24:.1f}d\n"
        elif t["duration_h"] >= 1:
            msg += f"  Duration: {t['duration_h']:.1f}h\n"
        msg += "\n"

    return msg


async def main():
    if not BOT_TOKEN or not ADMIN_CHAT:
        print("Missing TELEGRAM_BOT_TOKEN or ADMIN_CHAT_ID in .env")
        return

    # Run both chains — positions
    gmx_msgs = await diagnose_gmx()
    bx_msgs = await diagnose_bitunix()

    for msg in gmx_msgs:
        await send_msg(msg)
        await asyncio.sleep(0.5)

    for msg in bx_msgs:
        await send_msg(msg)
        await asyncio.sleep(0.5)

    # PnL summary
    pnl_msg = await diagnose_pnl()
    await send_msg(pnl_msg)
    await asyncio.sleep(0.5)

    # Trade history preview
    trades_msg = await diagnose_trades()
    await send_msg(trades_msg)

    print("\nDone! Check your Telegram admin channel.")


if __name__ == "__main__":
    asyncio.run(main())
