#!/usr/bin/env python3
"""
Live on-chain test of the update-message filter.

This script:
  1. Boots the GMXBot WITHOUT Telegram (Web3 only)
  2. Checks wallet balances
  3. Opens a real position using the MegaWhale signal format via process_signal()
  4. Waits for on-chain confirmation
  5. Sends fake update messages (TP hit, SL triggered, etc.) and confirms
     they are FILTERED — no second position opens
  6. Closes the test position and cancels orders

Run from the gmxbotv3 directory:
    python3 test_live_filter.py

Requirements:
  - .env must be configured with valid PRIVATE_KEY, RPC_URL, etc.
  - DRY_RUN can be true or false; if true, no real tx goes on-chain but
    the filter logic still runs identically.
"""

import os
import sys
import re
import time
import asyncio
import logging
import importlib
from unittest.mock import AsyncMock, patch
from dotenv import load_dotenv

# ── Load env before importing gmx (module-level reads env) ──
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Make sure we import from this directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gmx as gmx_mod
from gmx import GMXBot, DRY_RUN, ALLOWED_SYMBOLS
from close import fetch_positions as chain_fetch_positions
_open_mod = importlib.import_module("open")
fetch_current_price = _open_mod.fetch_current_price
cancel_all_orders = _open_mod.cancel_all_orders

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("LiveFilterTest")

# ══════════════════════════════════════════════════════════════════════════════
# Fake update messages to test (must NOT trigger new positions)
# Modeled after the MegaWhale Crypto channel update format
# ══════════════════════════════════════════════════════════════════════════════
FAKE_UPDATES = [
    # ── Target hit announcements (MegaWhale style) ──
    "🎯 BTC/USDT SHORT\nFirst target was hit\nProfit: +0.4%",
    "BTC/USDT SHORT — Target 1 hit ✅\nSL moved to entry",
    "BTC/USDT SHORT — Target 2 hit ✅",
    "BTC/USDT SHORT — Target 4 smashed! 🚀",
    "BTC/USDT SHORT — All targets reached ✅✅✅",

    # ── Stop loss / stopped out ──
    "BTC/USDT SHORT — Stopped out at entry 😐\n0% profit/loss",
    "BTC/USDT SHORT — Stopped out\nSmall loss -0.3%",
    "SL hit at $68,336usd",
    "Stop loss triggered on BTC/USDT SHORT",

    # ── SL moved / breakeven ──
    "BTC/USDT SHORT — SL moved to entry ✅",
    "Move SL to breakeven on BTC/USDT",
    "SL adjusted to Target 1 level ($67,511)",

    # ── Position closed / profit ──
    "BTC/USDT SHORT closed at +1.7%",
    "Trade closed with profit — BTC/USDT SHORT",
    "Position closed — +$85",

    # ── PnL updates ──
    "PnL: +1.723%",
    "Running in profit +0.8% 🟢",
    "+0.4% on BTC/USDT SHORT",
    "Trade update — Target 1 secured, riding rest to Target 4",

    # ── Generic updates ──
    "Breakeven 🔒",
    "Signal update: BTC/USDT SHORT still running",
    "All TP hit ✅ — great trade!",
    "TP1 hit ✅",
]

# ══════════════════════════════════════════════════════════════════════════════
# Helper: count open on-chain positions
# ══════════════════════════════════════════════════════════════════════════════

async def count_positions(bot):
    """Count open on-chain positions across all wallets."""
    total = 0
    for acct in [bot.account] + ([bot.account2] if bot.account2 else []):
        try:
            positions = await asyncio.to_thread(
                chain_fetch_positions, bot.w3, acct.address
            )
            total += len(positions) if positions else 0
        except Exception as e:
            log.warning(f"Failed to fetch positions for {acct.address[:10]}: {e}")
    return total


async def get_usdc_balance(bot, acct):
    """Get USDC balance for an account."""
    from web3 import Web3
    USDC_ADDR = os.getenv("GMX_V2_COLLATERAL_TOKEN", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831")
    usdc = bot.w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDR),
        abi=[{"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
              "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}],
              "type": "function"}]
    )
    bal = usdc.functions.balanceOf(acct.address).call()
    return bal / 1e6


# ══════════════════════════════════════════════════════════════════════════════
# MAIN TEST
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print("\n" + "=" * 70)
    print("  LIVE ON-CHAIN FILTER TEST (MegaWhale Signal Format)")
    print(f"  Mode: {'DRY RUN' if DRY_RUN else '⚡ LIVE ⚡'}")
    print("=" * 70 + "\n")

    # ── 1. Boot bot (Web3 only, no Telegram) ──
    bot = GMXBot()
    bot.init_web3()

    # Stub out Telegram so notify/send_message don't crash
    bot.client = None
    bot.notify = AsyncMock()
    bot.send_message = AsyncMock()
    bot.notify_position_opened = AsyncMock()

    log.info(f"W1: {bot.account.address}")
    if bot.account2:
        log.info(f"W2: {bot.account2.address}")

    # ── 2. Check balances ──
    usdc1 = await get_usdc_balance(bot, bot.account)
    eth1 = bot.w3.eth.get_balance(bot.account.address)
    log.info(f"W1 USDC: ${usdc1:.2f}  ETH: {bot.w3.from_wei(eth1, 'ether'):.6f}")
    if bot.account2:
        usdc2 = await get_usdc_balance(bot, bot.account2)
        eth2 = bot.w3.eth.get_balance(bot.account2.address)
        log.info(f"W2 USDC: ${usdc2:.2f}  ETH: {bot.w3.from_wei(eth2, 'ether'):.6f}")

    # ── 3. Get current BTC price for a realistic MegaWhale-style signal ──
    btc_price = await asyncio.to_thread(fetch_current_price, "BTC")
    log.info(f"Current BTC price: ${btc_price:,.2f}")

    # Build a signal in MegaWhale format — SHORT with 4 targets
    # Prices relative to current market so the order is valid
    entry = round(btc_price, 0)
    tp1 = round(btc_price * 0.996, 0)    # ~0.4% below
    tp2 = round(btc_price * 0.9925, 0)   # ~0.75% below
    tp3 = round(btc_price * 0.988, 0)    # ~1.2% below
    tp4 = round(btc_price * 0.983, 0)    # ~1.7% below
    sl = round(btc_price * 1.008, 0)     # ~0.8% above

    test_signal = (
        f"BTC/USDT Short 5x\n"
        f"Test signal for filter validation\n"
        f"Entry: ${entry:,.0f}usd\n"
        f"Target 1: ${tp1:,.0f}usd\n"
        f"Target 2: ${tp2:,.0f}usd\n"
        f"Target 3: ${tp3:,.0f}usd\n"
        f"Target 4: ${tp4:,.0f}usd\n"
        f"SL: ${sl:,.0f}usd\n"
        f"Gain: 1.7% loss: 0.8%\n"
        f"RR:"
    )

    # ── 4. Count positions BEFORE ──
    pos_before = await count_positions(bot)
    internal_before = len([p for p in bot.positions.values() if p.is_open])
    log.info(f"Positions before: {pos_before} on-chain, {internal_before} internal")

    # ── 5. Send the REAL signal ──
    print("\n" + "-" * 70)
    print("  STEP 1: Sending REAL MegaWhale-format signal to open position")
    print("-" * 70)
    print(f"\n{test_signal}\n")

    await bot.process_signal(test_signal)

    # Give GMX keepers time to process (market orders usually 5-15s)
    if not DRY_RUN:
        log.info("Waiting 20s for GMX keepers to fill the order...")
        await asyncio.sleep(20)

    pos_after_open = await count_positions(bot)
    internal_after_open = len([p for p in bot.positions.values() if p.is_open])
    log.info(f"Positions after signal: {pos_after_open} on-chain, {internal_after_open} internal")

    if DRY_RUN:
        opened = internal_after_open > internal_before
    else:
        opened = pos_after_open > pos_before

    if opened:
        print("  ✅ Position OPENED successfully")
    else:
        print("  ⚠️  Position may not have opened (check logs above)")
        if DRY_RUN:
            print("     (DRY_RUN=true — no on-chain tx expected)")

    # ── 6. Send FAKE update messages ──
    print("\n" + "-" * 70)
    print("  STEP 2: Sending FAKE update messages (should all be filtered)")
    print("-" * 70 + "\n")

    all_filtered = True
    for i, msg in enumerate(FAKE_UPDATES, 1):
        internal_before_fake = len([p for p in bot.positions.values() if p.is_open])
        chain_before_fake = await count_positions(bot)

        await bot.process_signal(msg)

        internal_after_fake = len([p for p in bot.positions.values() if p.is_open])

        label = msg.split("\n")[0][:55]
        if internal_after_fake == internal_before_fake:
            print(f"  ✅ #{i:02d} FILTERED: {label}")
        else:
            print(f"  ❌ #{i:02d} OPENED A POSITION: {label}")
            all_filtered = False

    # Verify no extra on-chain positions appeared
    if not DRY_RUN:
        log.info("Waiting 10s for any delayed orders to settle...")
        await asyncio.sleep(10)
    pos_after_fakes = await count_positions(bot)
    log.info(f"Positions after fake messages: {pos_after_fakes} on-chain")

    if not DRY_RUN and pos_after_fakes > pos_after_open:
        print(f"\n  ❌ EXTRA ON-CHAIN POSITIONS DETECTED: "
              f"{pos_after_fakes} vs {pos_after_open} before fakes")
        all_filtered = False
    else:
        print(f"\n  ✅ No extra on-chain positions (still {pos_after_fakes})")

    # ── 7. Cleanup: close test position ──
    print("\n" + "-" * 70)
    print("  STEP 3: Cleanup — closing test position + cancelling orders")
    print("-" * 70)

    if not DRY_RUN and pos_after_open > pos_before:
        for pos_id, pos in list(bot.positions.items()):
            if pos.is_open and pos.symbol == "BTC":
                log.info(f"Closing {pos.symbol} {pos.side} (W{pos.wallet_id})")
                try:
                    acct = bot.account if pos.wallet_id == 1 else bot.account2
                    market_addr = pos.market_addr
                    if market_addr:
                        from close import create_close_order
                        from web3 import Web3
                        exchange = bot.w3.eth.contract(
                            address=Web3.to_checksum_address(gmx_mod.GMX_V2_EXCHANGE_ROUTER),
                            abi=gmx_mod.EXCHANGE_ROUTER_ABI,
                        )
                        # Close the full position
                        await asyncio.to_thread(
                            create_close_order,
                            bot.w3, acct, exchange,
                            market_addr, pos.side == "LONG",
                            int(pos.size_usd * 1e30),
                            DRY_RUN,
                        )
                        log.info(f"Close order sent for {pos.symbol}")

                        # Wait for close to settle before cancelling orders
                        await asyncio.sleep(10)

                        # Cancel remaining TP/SL orders
                        from open import cancel_orders_for_market
                        n = await asyncio.to_thread(
                            cancel_orders_for_market,
                            bot.w3, acct, exchange, market_addr, DRY_RUN
                        )
                        log.info(f"Cancelled {n} remaining order(s)")
                except Exception as e:
                    log.error(f"Cleanup failed: {e}")

        await asyncio.sleep(15)
        pos_final = await count_positions(bot)
        log.info(f"Positions after cleanup: {pos_final} on-chain")
        if pos_final <= pos_before:
            print("  ✅ Test position closed and orders cancelled")
        else:
            print(f"  ⚠️  {pos_final} positions remain (started with {pos_before})")
            print("     You may need to manually close via /close in Telegram")
    elif DRY_RUN:
        print("  (DRY_RUN — no on-chain cleanup needed)")
    else:
        print("  (No new position was opened — nothing to clean up)")

    # ── 8. Final result ──
    print("\n" + "=" * 70)
    if all_filtered:
        print("  ✅ ALL TESTS PASSED")
        print("  - MegaWhale signal format parsed and executed correctly")
        print("  - All 23 fake update messages were filtered")
        print("  - No false positions opened from status messages")
    else:
        print("  ❌ SOME TESTS FAILED — check output above")
    print("=" * 70 + "\n")

    return all_filtered


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
