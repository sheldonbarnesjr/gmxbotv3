#!/usr/bin/env python3
"""
Fix TP orders for BTC SHORT position on W2.

Current state: 1 TP at $62,203 (100% close)
Desired state: 5 TPs with .env splits (TP_5_*):
  TP1: $67,450 (5%)
  TP2: $66,000 (5%)
  TP3: $64,949 (40%)
  TP4: $63,953 (30%)
  TP5: $62,203 (20%)

Steps:
  1. Cancel the existing single TP order
  2. Place 5 new TP orders with correct splits
"""

import os
import sys
import time

# Load .env before any other imports
from dotenv import load_dotenv
load_dotenv()

from web3 import Web3
from eth_account import Account

from open import (
    TakeProfit,
    create_tp_order,
    fetch_open_orders,
    EXCHANGE_ROUTER_ABI,
    build_tx,
    sign_send,
    wait_receipt,
    ORDER_TYPE_LIMIT_DECREASE,
)


def main():
    # ── Config ──
    RPC_URL = os.getenv("RPC_URL")
    PK_W2 = os.getenv("PRIVATE_KEY_2")
    EXCHANGE_ROUTER = os.getenv("GMX_V2_EXCHANGE_ROUTER")
    ORDER_VAULT = os.getenv("GMX_V2_ORDER_VAULT")
    COLLATERAL_TOKEN = os.getenv("GMX_V2_COLLATERAL_TOKEN")
    BTC_MARKET = "0x47c031236e19d024b42f8ae6780e44a573170703"
    EXECUTION_FEE = int(os.getenv("GMX_V2_EXECUTION_FEE_WEI", "200000000000000"))
    SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "30"))

    if not all([RPC_URL, PK_W2, EXCHANGE_ROUTER, ORDER_VAULT, COLLATERAL_TOKEN]):
        print("Missing required env vars. Check .env file.")
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    acct = Account.from_key(PK_W2)
    wallet = Web3.to_checksum_address(acct.address)
    market = Web3.to_checksum_address(BTC_MARKET)
    collateral_token = Web3.to_checksum_address(COLLATERAL_TOKEN)
    order_vault = Web3.to_checksum_address(ORDER_VAULT)
    exchange = w3.eth.contract(
        address=Web3.to_checksum_address(EXCHANGE_ROUTER),
        abi=EXCHANGE_ROUTER_ABI,
    )

    print(f"Wallet (W2): {wallet}")
    print(f"Chain ID: {w3.eth.chain_id}")
    print(f"ETH balance: {w3.eth.get_balance(wallet) / 1e18:.6f} ETH")

    # ── Step 1: Find and cancel existing TP orders for BTC market ──
    print("\n" + "=" * 60)
    print("STEP 1: Cancel existing TP order(s) for BTC SHORT")
    print("=" * 60)

    orders = fetch_open_orders(w3, wallet)
    btc_tp_orders = [
        o for o in orders
        if o["market"].lower() == BTC_MARKET.lower()
        and o["order_type"] == ORDER_TYPE_LIMIT_DECREASE  # 5 = LimitDecrease (TP)
    ]

    if not btc_tp_orders:
        print("No BTC TP orders found to cancel.")
    else:
        print(f"Found {len(btc_tp_orders)} BTC TP order(s) to cancel:")
        for o in btc_tp_orders:
            print(f"  TP @ ${o['trigger_price']:,.2f} (${o['size_usd']:,.2f}) key={o['key_hex'][:16]}...")

        for o in btc_tp_orders:
            key_hex = o["key_hex"]
            if not key_hex:
                print(f"  Skipping order with no key")
                continue
            key_bytes = bytes.fromhex(key_hex)
            print(f"  Cancelling TP @ ${o['trigger_price']:,.2f}...")
            data = exchange.encode_abi("cancelOrder", [key_bytes])
            tx = build_tx(w3, wallet, exchange.address, data, value=0)
            txh = sign_send(w3, acct, tx, dry_run=False)
            receipt = wait_receipt(w3, txh)
            if receipt.get("status") == 1:
                print(f"    Cancelled: {txh}")
            else:
                print(f"    FAILED to cancel: {txh}")
                sys.exit(1)
            time.sleep(2)

    # ── Step 2: Place 5 TP orders with correct splits ──
    print("\n" + "=" * 60)
    print("STEP 2: Place 5 TP orders with correct .env splits")
    print("=" * 60)

    # Position params (from the execution log)
    TOTAL_SIZE_USD = 3339.76
    COLLATERAL_USD = 334.0  # ~$334 collateral at 10x
    IS_LONG = False  # SHORT
    SYMBOL = "BTC"

    # TP levels with .env TP_5_* splits:
    # TP_5_1=5, TP_5_2=5, TP_5_3=40, TP_5_4=30, TP_5_5=20
    tp_levels = [
        TakeProfit(price=67_450.0, close_pct=0.05),   # TP1: 5%
        TakeProfit(price=66_000.0, close_pct=0.05),   # TP2: 5%
        TakeProfit(price=64_949.0, close_pct=0.40),   # TP3: 40%
        TakeProfit(price=63_953.0, close_pct=0.30),   # TP4: 30%
        TakeProfit(price=62_203.0, close_pct=0.20),   # TP5: 20%
    ]

    total_pct = sum(tp.close_pct for tp in tp_levels)
    print(f"\nPosition: {SYMBOL} SHORT, Size: ${TOTAL_SIZE_USD:,.2f}, Collateral: ${COLLATERAL_USD:,.2f}")
    print(f"Total TP %: {total_pct:.0%}")
    print()

    for i, tp in enumerate(tp_levels):
        tp_size = TOTAL_SIZE_USD * tp.close_pct
        print(f"  TP{i+1}: ${tp.price:,.2f} ({tp.close_pct:.0%} = ${tp_size:,.2f})")

    print()
    results = []
    for i, tp in enumerate(tp_levels):
        print(f"Placing TP{i+1} @ ${tp.price:,.2f} ({tp.close_pct:.0%})...")
        try:
            txh = create_tp_order(
                w3=w3, acct=acct, exchange=exchange, wallet=wallet,
                market=market, collateral_token=collateral_token,
                order_vault=order_vault, tp=tp, total_size_usd=TOTAL_SIZE_USD,
                collateral_usd=COLLATERAL_USD,
                symbol=SYMBOL, is_long=IS_LONG,
                slippage_bps=SLIPPAGE_BPS, execution_fee=EXECUTION_FEE,
                dry_run=False,
            )
            results.append((tp.price, tp.close_pct, txh, None))
            print(f"  OK: {txh}")
            time.sleep(2)
        except Exception as e:
            results.append((tp.price, tp.close_pct, None, str(e)))
            print(f"  FAILED: {e}")

    # ── Summary ──
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    ok = [r for r in results if r[2]]
    fail = [r for r in results if r[3]]
    for price, pct, txh, err in results:
        status = txh if txh else f"FAILED: {err}"
        print(f"  TP @ ${price:,.2f} ({pct:.0%}): {status}")

    print(f"\n{len(ok)} placed, {len(fail)} failed")

    if fail:
        print("\n⚠️  Some TPs failed. You may need to retry manually.")
        sys.exit(1)
    else:
        print("\nAll 5 TPs placed successfully!")


if __name__ == "__main__":
    main()
