"""
Bitunix Futures executor for GMXBot.

Provides execute_bitunix_signal() that takes a parsed Signal (from open.py)
and executes it on Bitunix via the REST API.  Returns a result dict
compatible with the GMXBot position-creation flow.

This module is the bridge between GMXBot's signal pipeline and Bitunix.
"""

import time
import logging
import functools
from typing import Dict, Any, Optional, List

from bitunix_api import BitunixClient
from bitunix_pairs import get_bitunix_symbol, get_pair_info

log = logging.getLogger("GMXBot.bitunix")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Retry decorator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _retry(max_retries: int = 3, base_delay: float = 2.0, label: str = ""):
    """Retry API calls with exponential backoff on transient errors."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (ConnectionError, TimeoutError, OSError) as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = base_delay * (2 ** (attempt - 1))
                        log.warning(f"{label or func.__name__}: attempt {attempt}/{max_retries} "
                                    f"({type(e).__name__}), retrying in {delay:.0f}s...")
                        time.sleep(delay)
                except Exception as e:
                    err_str = str(e).lower()
                    if any(kw in err_str for kw in ["connection", "timeout", "rate limit", "502", "503", "429"]):
                        last_exc = e
                        if attempt < max_retries:
                            delay = base_delay * (2 ** (attempt - 1))
                            log.warning(f"{label or func.__name__}: attempt {attempt}/{max_retries} "
                                        f"({e}), retrying in {delay:.0f}s...")
                            time.sleep(delay)
                            continue
                    raise
            raise last_exc
        return wrapper
    return decorator


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Symbol / quantity helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def to_bitunix_symbol(symbol: str) -> str:
    """Convert short symbol (BTC) to Bitunix pair (BTCUSDT).

    Uses bitunix_pairs for correct mapping (e.g. PEPE -> 1000PEPEUSDT).
    """
    return get_bitunix_symbol(symbol) or f"{symbol}USDT"


def format_qty(symbol: str, qty: float) -> str:
    """Format quantity with the correct precision for a symbol."""
    info = get_pair_info(symbol)
    precision = info["basePrecision"] if info else 6
    return f"{qty:.{precision}f}"


def get_qty_for_size(client: BitunixClient, symbol: str,
                     size_usd: float, price: float) -> str:
    """Calculate quantity in base coin for a given USD size."""
    if price <= 0:
        raise ValueError(f"Invalid price {price} for {symbol} — cannot calculate qty")
    qty_raw = size_usd / price
    pair_info = get_pair_info(symbol)
    if pair_info:
        qty_precision = pair_info["basePrecision"]
        min_qty = float(pair_info["minQty"])
        qty_raw = max(qty_raw, min_qty)
        return f"{qty_raw:.{qty_precision}f}"
    # Fallback: fetch from API
    pairs = client.get_trading_pairs(to_bitunix_symbol(symbol))
    if pairs:
        pair = pairs[0] if isinstance(pairs, list) else pairs
        qty_precision = int(pair.get("quantityPrecision", pair.get("qtyPrecision", 3)))
        min_qty = float(pair.get("minQty", "0.001"))
        qty_raw = max(qty_raw, min_qty)
        return f"{qty_raw:.{qty_precision}f}"
    return f"{qty_raw:.6f}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Leverage / margin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@_retry(label="set_leverage")
def set_leverage(client: BitunixClient, symbol: str, leverage: int):
    """Set leverage for the trading pair."""
    bitunix_sym = to_bitunix_symbol(symbol)
    try:
        client.change_leverage(bitunix_sym, leverage)
        log.info(f"Leverage set to {leverage}x for {bitunix_sym}")
    except Exception as e:
        if "same" in str(e).lower() or "already" in str(e).lower():
            log.info(f"Leverage already at {leverage}x for {bitunix_sym}")
        else:
            raise


@_retry(label="set_margin_mode")
def set_margin_mode(client: BitunixClient, symbol: str, mode: str = "ISOLATION"):
    """Set margin mode (ISOLATION or CROSS) for a symbol."""
    bitunix_sym = to_bitunix_symbol(symbol)
    try:
        client.change_margin_mode(bitunix_sym, mode)
        log.info(f"Margin mode set to {mode} for {bitunix_sym}")
    except Exception as e:
        if "same" in str(e).lower() or "already" in str(e).lower():
            log.info(f"Margin mode already {mode} for {bitunix_sym}")
        else:
            raise


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Order execution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@_retry(label="open_position")
def open_position(client: BitunixClient, symbol: str, is_long: bool,
                  size_usd: float, dry_run: bool = False) -> dict:
    """Open a market position on Bitunix. Returns dict with orderId, price, qty."""
    bitunix_sym = to_bitunix_symbol(symbol)
    side = "BUY" if is_long else "SELL"

    price = client.get_current_price(bitunix_sym)
    qty = get_qty_for_size(client, symbol, size_usd, price)

    log.info(f"Opening {'LONG' if is_long else 'SHORT'} {symbol}: "
             f"qty={qty} (${size_usd:.2f} at ${price:,.2f})")

    if dry_run:
        log.info("[DRY RUN] Would place order -- skipping")
        return {"orderId": "dry-run", "price": price, "qty": qty}

    result = client.place_order(
        symbol=bitunix_sym,
        side=side,
        qty=qty,
        order_type="MARKET",
        trade_side="OPEN",
    )
    log.info(f"Order placed: {result}")
    return {**result, "price": price, "qty": qty}


def _merge_tiny_tps(take_profits: list, total_qty: float, symbol: str) -> list:
    """Merge TP levels whose qty would be below the exchange minQty.

    Carries the pct of any too-small TP forward into the next TP.
    If the last TP is too small, merges it into the previous one.
    Returns a new list (may be shorter than the input).
    """
    info = get_pair_info(symbol)
    if not info:
        return take_profits  # unknown symbol, can't check

    min_qty = float(info["minQty"])
    merged = []
    carry_pct = 0.0

    for tp in take_profits:
        pct = (tp.close_pct if hasattr(tp, "close_pct") else tp.get("close_pct", 0)) + carry_pct
        carry_pct = 0.0
        qty = total_qty * pct

        if qty < min_qty:
            # Too small — carry forward
            carry_pct = pct
            log.info(f"  Merging tiny TP ${tp.price if hasattr(tp, 'price') else tp['price']:,.2f} "
                     f"({pct:.1%} = {qty:.6f} < minQty {min_qty}) into next TP")
        else:
            if hasattr(tp, "close_pct"):
                from dataclasses import replace
                merged.append(replace(tp, close_pct=pct))
            else:
                merged.append({**tp, "close_pct": pct})

    # If there's leftover carry, add it to the last merged TP
    if carry_pct > 0 and merged:
        last = merged[-1]
        if hasattr(last, "close_pct"):
            from dataclasses import replace
            merged[-1] = replace(last, close_pct=last.close_pct + carry_pct)
        else:
            merged[-1] = {**last, "close_pct": last["close_pct"] + carry_pct}
        log.info(f"  Carried {carry_pct:.1%} into last TP (now {merged[-1].close_pct if hasattr(merged[-1], 'close_pct') else merged[-1]['close_pct']:.1%})")
    elif carry_pct > 0 and not merged and take_profits:
        # ALL TPs were too small individually — consolidate into a single TP
        # at the last (furthest) price with 100% of the position
        last_tp = take_profits[-1]
        if hasattr(last_tp, "close_pct"):
            from dataclasses import replace
            merged.append(replace(last_tp, close_pct=carry_pct))
        else:
            merged.append({**last_tp, "close_pct": carry_pct})
        log.warning(
            f"  ALL {len(take_profits)} TPs below minQty — consolidated into single TP "
            f"at ${last_tp.price if hasattr(last_tp, 'price') else last_tp['price']:,.2f} "
            f"({carry_pct:.1%})"
        )

    return merged


def place_tp_orders(client: BitunixClient, symbol: str, position_id: str,
                    take_profits: list, total_qty: float,
                    dry_run: bool = False) -> list:
    """Place TP orders (partial closes) for each take profit level.

    take_profits: list of objects with .price and .close_pct (or percentage) attrs,
                  OR dicts with "price" and "close_pct" keys.
    """
    # Merge any TPs whose qty would be below exchange minQty
    take_profits = _merge_tiny_tps(take_profits, total_qty, symbol)

    bitunix_sym = to_bitunix_symbol(symbol)
    results = []

    for i, tp in enumerate(take_profits):
        tp_price = tp.price if hasattr(tp, "price") else tp["price"]
        tp_pct = tp.close_pct if hasattr(tp, "close_pct") else tp.get("close_pct", tp.get("percentage", 0))
        tp_qty = total_qty * tp_pct
        tp_qty_str = format_qty(symbol, tp_qty)

        log.info(f"  TP{i+1}: ${tp_price:,.2f} ({tp_pct:.0%} = {tp_qty_str} {symbol})")

        if dry_run:
            results.append({"price": tp_price, "pct": tp_pct, "orderId": "dry-run"})
            continue

        try:
            result = client.place_tpsl_order(
                symbol=bitunix_sym,
                position_id=position_id,
                tp_price=str(tp_price),
                tp_qty=tp_qty_str,
            )
            if isinstance(result, list) and result:
                order_id = result[0].get("orderId")
            elif isinstance(result, dict):
                order_id = result.get("orderId")
            else:
                order_id = None
            results.append({"price": tp_price, "pct": tp_pct, "orderId": order_id})
        except Exception as e:
            log.error(f"  TP{i+1} failed: {e}")
            results.append({"price": tp_price, "pct": tp_pct, "orderId": None, "error": str(e)})

    return results


def place_sl_order(client: BitunixClient, symbol: str, position_id: str,
                   sl_price: float, dry_run: bool = False) -> Optional[str]:
    """Place a stop loss order on the position."""
    bitunix_sym = to_bitunix_symbol(symbol)
    log.info(f"  SL: ${sl_price:,.2f} (full position)")

    if dry_run:
        return "dry-run"

    try:
        result = client.place_position_tpsl(
            symbol=bitunix_sym,
            position_id=position_id,
            sl_price=str(sl_price),
        )
        return result.get("orderId")
    except Exception as e:
        log.error(f"  SL order failed: {e}")
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Full signal execution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def execute_bitunix_signal(
    client: BitunixClient,
    symbol: str,
    is_long: bool,
    leverage: float,
    stop_loss: float,
    take_profits: list,
    size_usd: float,
    margin_mode: str = "ISOLATION",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Execute a full signal on Bitunix: set margin/leverage, open, place TP/SL.

    Args:
        client: Authenticated BitunixClient
        symbol: Short symbol (e.g. "BTC", "ETH")
        is_long: True for LONG, False for SHORT
        leverage: Desired leverage (e.g. 10.0)
        stop_loss: Stop loss price
        take_profits: List of TP objects/dicts with price and close_pct/percentage
        size_usd: Total position size in USD
        margin_mode: "ISOLATION" or "CROSS"
        dry_run: If True, skip actual order placement

    Returns:
        Dict with keys: open, tp, sl, position_id, tp_sl_failed
    """
    results: Dict[str, Any] = {
        "open": None, "tp": [], "sl": None,
        "position_id": None, "tp_sl_failed": False,
    }

    # Step 0: Set margin mode + leverage
    try:
        set_margin_mode(client, symbol, margin_mode)
    except Exception as e:
        log.warning(f"Margin mode setting failed (continuing): {e}")

    try:
        set_leverage(client, symbol, int(leverage))
    except Exception as e:
        log.warning(f"Leverage setting failed (continuing): {e}")

    # Step 1: Open position
    open_result = open_position(client, symbol, is_long, size_usd, dry_run)
    results["open"] = open_result

    # Wait for position to appear
    position_id = None
    total_qty = float(open_result.get("qty", 0))

    if not dry_run:
        bitunix_sym = to_bitunix_symbol(symbol)
        expected_side = "LONG" if is_long else "SHORT"

        for attempt in range(1, 6):
            wait = 2 * attempt
            log.info(f"Waiting {wait}s for position to register (attempt {attempt}/5)...")
            time.sleep(wait)

            positions = client.get_pending_positions(bitunix_sym)
            for pos in positions:
                raw_side = pos.get("side", "").upper()
                pos_side = "LONG" if raw_side in ("BUY", "LONG") else "SHORT"
                if pos_side == expected_side:
                    position_id = pos.get("positionId")
                    total_qty = float(pos.get("qty", total_qty))
                    break

            if position_id:
                log.info(f"Position found: {position_id} (qty={total_qty})")
                break

        if not position_id:
            log.error("CRITICAL: Could not find position ID after 5 attempts -- TP/SL skipped!")
            results["tp_sl_failed"] = True
    else:
        position_id = "dry-run-pos"

    results["position_id"] = position_id

    # Step 2: Place TP orders
    if take_profits and position_id:
        if total_qty <= 0:
            log.error(f"CRITICAL: total_qty={total_qty} after open — TP/SL orders skipped!")
            results["tp_sl_failed"] = True
        else:
            log.info(f"Placing {len(take_profits)} Take Profit order(s)")
            results["tp"] = place_tp_orders(
                client, symbol, position_id, take_profits, total_qty, dry_run
            )

    # Step 3: Place SL order
    if stop_loss and position_id and not results.get("tp_sl_failed"):
        log.info("Placing Stop Loss order")
        results["sl"] = place_sl_order(
            client, symbol, position_id, stop_loss, dry_run
        )

    return results


def close_bitunix_position(client: BitunixClient, symbol: str,
                           is_long: bool, dry_run: bool = False) -> Optional[str]:
    """Close a Bitunix position by finding it on-chain and flash-closing.

    Returns position_id if closed, None on failure.
    """
    bitunix_sym = to_bitunix_symbol(symbol)
    expected_side = "LONG" if is_long else "SHORT"

    positions = client.get_pending_positions(bitunix_sym)
    for pos in positions:
        raw_side = pos.get("side", "").upper()
        pos_side = "LONG" if raw_side in ("BUY", "LONG") else "SHORT"
        if pos_side == expected_side:
            position_id = pos.get("positionId")
            if dry_run:
                log.info(f"[DRY RUN] Would close {symbol} {expected_side} ({position_id})")
                return position_id
            try:
                client.flash_close_position(position_id)
                log.info(f"Closed {symbol} {expected_side} ({position_id})")
                return position_id
            except Exception as e:
                log.error(f"Failed to close {symbol} {expected_side}: {e}")
                return None

    log.warning(f"No {symbol} {expected_side} position found on Bitunix")
    return None


def get_bitunix_balance(client: BitunixClient) -> float:
    """Get available USDT balance from Bitunix."""
    return client.get_balance()


def get_bitunix_positions(client: BitunixClient) -> list:
    """Get all open Bitunix positions."""
    return client.get_pending_positions()
