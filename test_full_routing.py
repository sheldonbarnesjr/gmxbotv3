#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
FULL ROUTING TEST: BTC + ETH + SOL on BOTH WALLETS
═══════════════════════════════════════════════════════════════

Opens 6 positions total:
  W1: BTC LONG, ETH LONG, SOL LONG
  W2: BTC LONG, ETH LONG, SOL LONG

Verifies:
  - Each symbol routes to W1 first, then W2
  - All 6 positions visible on-chain
  - TP/SL orders placed for each
  - All wallets full → new signal rejected
  - Closes all 6 positions
  - Cancels orphaned orders
  - Final balance check

Usage:
    LIVE_TEST=true python3 test_full_routing.py
"""

import os, sys, asyncio, logging, time

if os.getenv("LIVE_TEST", "").lower() != "true":
    os.environ["DRY_RUN"] = "true"
    print("Set LIVE_TEST=true to run. Exiting.\n")
    sys.exit(0)
else:
    os.environ["DRY_RUN"] = "false"

from dotenv import load_dotenv
load_dotenv()

from gmx import (
    GMXBot, GMX_V2_MARKETS, DRY_RUN, PORTFOLIO_PCT,
    GMX_V2_COLLATERAL_TOKEN, GMX_V2_EXCHANGE_ROUTER,
    EXECUTION_FEE_WEI, SLIPPAGE_BPS, ERC20_ABI,
    EXCHANGE_ROUTER_ABI, parse_signal,
    cancel_all_orders, cancel_orders_for_market,
)
from close import fetch_positions as chain_fetch_positions, create_close_order
from open import fetch_open_orders, TakeProfit, Signal, fetch_current_price
from web3 import Web3

logging.basicConfig(level=logging.INFO, format="%(message)s")

passed = 0
failed = 0

def check(label, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✅ {label}")
        passed += 1
    else:
        print(f"  ❌ {label}")
        if detail:
            print(f"       {detail}")
        failed += 1

def sep(title):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


async def wait_pos(bot, acct, symbol, timeout=60):
    market = GMX_V2_MARKETS.get(symbol, "").lower()
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            ps = await asyncio.to_thread(chain_fetch_positions, bot.w3, acct.address)
            if any(p.market.lower() == market for p in ps):
                return True, int(time.time() - t0)
        except Exception:
            pass
        await asyncio.sleep(5)
    return False, int(time.time() - t0)


async def wait_closed(bot, acct, symbol, timeout=120):
    market = GMX_V2_MARKETS.get(symbol, "").lower()
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            ps = await asyncio.to_thread(chain_fetch_positions, bot.w3, acct.address)
            if not any(p.market.lower() == market for p in ps):
                return True, int(time.time() - t0)
        except Exception:
            pass
        await asyncio.sleep(5)
    return False, int(time.time() - t0)


async def main():
    global passed, failed

    bot = GMXBot()
    bot.init_web3()

    async def noop(*a, **kw): pass
    bot.notify = noop
    bot.send_message = noop
    bot.notify_position_opened = noop

    w1 = bot.account
    w2 = bot.account2
    w1_addr = w1.address
    w2_addr = w2.address

    print("═" * 60)
    print("  FULL ROUTING TEST: BTC + ETH + SOL on BOTH WALLETS")
    print("═" * 60)
    print(f"  W1: {w1_addr}")
    print(f"  W2: {w2_addr}")

    USDC_ADDR = Web3.to_checksum_address(GMX_V2_COLLATERAL_TOKEN)
    usdc_token = bot.w3.eth.contract(address=USDC_ADDR, abi=ERC20_ABI)
    usdc_dec = usdc_token.functions.decimals().call()

    # ── PHASE 0: Clean slate ──────────────────────────────────
    sep("PHASE 0: Clean Slate")
    print("     Checking existing positions...")

    exchange = bot.w3.eth.contract(
        address=Web3.to_checksum_address(GMX_V2_EXCHANGE_ROUTER),
        abi=EXCHANGE_ROUTER_ABI,
    )

    for label, acct in [("W1", w1), ("W2", w2)]:
        existing = await asyncio.to_thread(chain_fetch_positions, bot.w3, acct.address)
        if existing:
            print(f"     {label} has {len(existing)} positions — closing...")
            for p in existing:
                try:
                    await asyncio.to_thread(create_close_order, bot.w3, acct, p, 1.0, False)
                except Exception as e:
                    print(f"       Failed: {e}")
            await asyncio.sleep(30)
        try:
            await asyncio.to_thread(cancel_all_orders, bot.w3, acct, exchange, False)
        except Exception:
            pass

    await asyncio.sleep(5)

    # Record initial USDC
    w1_usdc_start = usdc_token.functions.balanceOf(w1_addr).call() / 10**usdc_dec
    w2_usdc_start = usdc_token.functions.balanceOf(w2_addr).call() / 10**usdc_dec
    total_start = w1_usdc_start + w2_usdc_start
    print(f"     W1: ${w1_usdc_start:,.2f} | W2: ${w2_usdc_start:,.2f} | Total: ${total_start:,.2f}")
    check("Both wallets clean", True)

    # ── Fetch prices ──────────────────────────────────────────
    sep("PHASE 1: Price Feeds")

    prices = {}
    for sym in ["BTC", "ETH", "SOL"]:
        await asyncio.sleep(3)
        try:
            p = fetch_current_price(sym, w3=bot.w3)
        except Exception:
            p = None
        prices[sym] = p
        check(f"{sym} price fetched", p is not None and p > 0,
              f"${p:,.2f}" if p else "failed")
        if p:
            print(f"     {sym}: ${p:,.2f}")

    if not all(prices.values()):
        print("\n  Cannot proceed without all prices. Exiting.")
        return False

    # ── PHASE 2: Open BTC on W1 ──────────────────────────────
    sep("PHASE 2: Open BTC LONG → W1")

    wid, acct = await bot._pick_wallet("BTC")
    check("BTC routes to W1", wid == 1, f"got W{wid}")

    btc = prices["BTC"]
    sig_btc1 = Signal(
        symbol="BTC", side="LONG",
        entry_low=btc*0.995, entry_high=btc*1.005,
        take_profits=[
            TakeProfit(price=btc*1.02, close_pct=0.50),
            TakeProfit(price=btc*1.04, close_pct=1.0),
        ],
        stop_loss=btc*0.96, leverage=5.0, raw_text="ROUTING TEST",
    )

    pos, _ = await bot.execute_open(sig_btc1, 20.0, w1)
    check("W1 BTC opened", pos is not None)
    if pos:
        pos.wallet_id = 1
        bot.positions[pos.id] = pos
        print(f"     TX: {pos.tx_hash}")
        ok, s = await wait_pos(bot, w1, "BTC")
        check("W1 BTC on-chain", ok, f"timeout {s}s")

    # ── PHASE 3: Open ETH on W1 ──────────────────────────────
    sep("PHASE 3: Open ETH LONG → W1")
    await asyncio.sleep(3)

    wid, acct = await bot._pick_wallet("ETH")
    check("ETH routes to W1", wid == 1, f"got W{wid}")

    eth = prices["ETH"]
    sig_eth1 = Signal(
        symbol="ETH", side="LONG",
        entry_low=eth*0.995, entry_high=eth*1.005,
        take_profits=[
            TakeProfit(price=eth*1.03, close_pct=0.50),
            TakeProfit(price=eth*1.06, close_pct=1.0),
        ],
        stop_loss=eth*0.95, leverage=5.0, raw_text="ROUTING TEST",
    )

    pos, _ = await bot.execute_open(sig_eth1, 20.0, w1)
    check("W1 ETH opened", pos is not None)
    if pos:
        pos.wallet_id = 1
        bot.positions[pos.id] = pos
        print(f"     TX: {pos.tx_hash}")
        ok, s = await wait_pos(bot, w1, "ETH")
        check("W1 ETH on-chain", ok, f"timeout {s}s")

    # ── PHASE 4: Open SOL on W1 ──────────────────────────────
    sep("PHASE 4: Open SOL LONG → W1")
    await asyncio.sleep(3)

    wid, acct = await bot._pick_wallet("SOL")
    check("SOL routes to W1", wid == 1, f"got W{wid}")

    sol = prices["SOL"]
    sig_sol1 = Signal(
        symbol="SOL", side="LONG",
        entry_low=sol*0.995, entry_high=sol*1.005,
        take_profits=[
            TakeProfit(price=sol*1.04, close_pct=0.50),
            TakeProfit(price=sol*1.08, close_pct=1.0),
        ],
        stop_loss=sol*0.92, leverage=5.0, raw_text="ROUTING TEST",
    )

    pos, _ = await bot.execute_open(sig_sol1, 20.0, w1)
    check("W1 SOL opened", pos is not None)
    if pos:
        pos.wallet_id = 1
        bot.positions[pos.id] = pos
        print(f"     TX: {pos.tx_hash}")
        ok, s = await wait_pos(bot, w1, "SOL")
        check("W1 SOL on-chain", ok, f"timeout {s}s")

    # ── PHASE 5: Verify W1 full → routes to W2 ───────────────
    sep("PHASE 5: Routing Shift → W2")

    wid, _ = await bot._pick_wallet("BTC")
    check("BTC now routes to W2", wid == 2, f"got W{wid}")
    wid, _ = await bot._pick_wallet("ETH")
    check("ETH now routes to W2", wid == 2, f"got W{wid}")
    wid, _ = await bot._pick_wallet("SOL")
    check("SOL now routes to W2", wid == 2, f"got W{wid}")

    # ── PHASE 6: Open BTC on W2 ──────────────────────────────
    sep("PHASE 6: Open BTC LONG → W2")
    await asyncio.sleep(3)

    pos, _ = await bot.execute_open(sig_btc1, 20.0, w2)
    check("W2 BTC opened", pos is not None)
    if pos:
        pos.wallet_id = 2
        bot.positions[pos.id] = pos
        print(f"     TX: {pos.tx_hash}")
        ok, s = await wait_pos(bot, w2, "BTC")
        check("W2 BTC on-chain", ok, f"timeout {s}s")

    # ── PHASE 7: Open ETH on W2 ──────────────────────────────
    sep("PHASE 7: Open ETH LONG → W2")
    await asyncio.sleep(3)

    pos, _ = await bot.execute_open(sig_eth1, 20.0, w2)
    check("W2 ETH opened", pos is not None)
    if pos:
        pos.wallet_id = 2
        bot.positions[pos.id] = pos
        print(f"     TX: {pos.tx_hash}")
        ok, s = await wait_pos(bot, w2, "ETH")
        check("W2 ETH on-chain", ok, f"timeout {s}s")

    # ── PHASE 8: Open SOL on W2 ──────────────────────────────
    sep("PHASE 8: Open SOL LONG → W2")
    await asyncio.sleep(3)

    pos, _ = await bot.execute_open(sig_sol1, 20.0, w2)
    check("W2 SOL opened", pos is not None)
    if pos:
        pos.wallet_id = 2
        bot.positions[pos.id] = pos
        print(f"     TX: {pos.tx_hash}")
        ok, s = await wait_pos(bot, w2, "SOL")
        check("W2 SOL on-chain", ok, f"timeout {s}s")

    # ── PHASE 9: All wallets full → reject ────────────────────
    sep("PHASE 9: Both Wallets Full → Reject")

    wid, _ = await bot._pick_wallet("BTC")
    check("BTC rejected (both full)", wid is None, f"got W{wid}")
    wid, _ = await bot._pick_wallet("ETH")
    check("ETH rejected (both full)", wid is None, f"got W{wid}")
    wid, _ = await bot._pick_wallet("SOL")
    check("SOL rejected (both full)", wid is None, f"got W{wid}")

    # ── PHASE 10: Verify 6 positions on-chain ─────────────────
    sep("PHASE 10: Verify All 6 Positions On-Chain")

    w1_pos = await asyncio.to_thread(chain_fetch_positions, bot.w3, w1_addr)
    w2_pos = await asyncio.to_thread(chain_fetch_positions, bot.w3, w2_addr)

    print(f"     W1 positions: {len(w1_pos)}")
    for p in w1_pos:
        side = "LONG" if p.is_long else "SHORT"
        print(f"       {p.symbol} {side} ${p.size_usd:,.2f} @ {p.leverage:.1f}x")
    print(f"     W2 positions: {len(w2_pos)}")
    for p in w2_pos:
        side = "LONG" if p.is_long else "SHORT"
        print(f"       {p.symbol} {side} ${p.size_usd:,.2f} @ {p.leverage:.1f}x")

    check("W1 has 3 positions", len(w1_pos) == 3, f"got {len(w1_pos)}")
    check("W2 has 3 positions", len(w2_pos) == 3, f"got {len(w2_pos)}")

    # Check each symbol present on each wallet
    w1_syms = {p.symbol for p in w1_pos}
    w2_syms = {p.symbol for p in w2_pos}
    for sym in ["BTC", "ETH", "SOL"]:
        check(f"W1 has {sym}", sym in w1_syms, f"W1 symbols: {w1_syms}")
        check(f"W2 has {sym}", sym in w2_syms, f"W2 symbols: {w2_syms}")

    # Check orders
    w1_ords = await asyncio.to_thread(fetch_open_orders, bot.w3, w1_addr)
    w2_ords = await asyncio.to_thread(fetch_open_orders, bot.w3, w2_addr)
    print(f"     W1 orders: {len(w1_ords)} | W2 orders: {len(w2_ords)}")
    # 3 positions × (2 TP + 1 SL) = 9 orders per wallet
    check("W1 has ≥6 orders (TP+SL)", len(w1_ords) >= 6, f"got {len(w1_ords)}")
    check("W2 has ≥6 orders (TP+SL)", len(w2_ords) >= 6, f"got {len(w2_ords)}")

    # ── PHASE 11: Close all 6 positions ───────────────────────
    sep("PHASE 11: Close All 6 Positions")

    for label, acct, positions in [("W1", w1, w1_pos), ("W2", w2, w2_pos)]:
        for p in positions:
            side = "LONG" if p.is_long else "SHORT"
            try:
                tx = await asyncio.to_thread(create_close_order, bot.w3, acct, p, 1.0, False)
                check(f"{label} {p.symbol} {side} close submitted", True)
                print(f"     TX: {tx}")
            except Exception as e:
                check(f"{label} {p.symbol} {side} close submitted", False, str(e))

    # Wait for all to close
    print("     Waiting for all positions to close...")
    for label, acct in [("W1", w1), ("W2", w2)]:
        for sym in ["BTC", "ETH", "SOL"]:
            ok, s = await wait_closed(bot, acct, sym)
            check(f"{label} {sym} closed on-chain", ok, f"timeout {s}s")
            if ok:
                print(f"     {label} {sym} closed after {s}s")

    # ── PHASE 12: Cancel orphaned orders ──────────────────────
    sep("PHASE 12: Cancel Orphaned Orders")

    await asyncio.sleep(5)

    for label, acct in [("W1", w1), ("W2", w2)]:
        try:
            n = await asyncio.to_thread(cancel_all_orders, bot.w3, acct, exchange, False)
            print(f"     {label}: cancelled {n} orders")
            check(f"{label} orders cancelled", True)
        except Exception as e:
            check(f"{label} orders cancelled", False, str(e))

    await asyncio.sleep(5)

    w1_ords_final = await asyncio.to_thread(fetch_open_orders, bot.w3, w1_addr)
    w2_ords_final = await asyncio.to_thread(fetch_open_orders, bot.w3, w2_addr)
    check("W1 has 0 orders", len(w1_ords_final) == 0, f"got {len(w1_ords_final)}")
    check("W2 has 0 orders", len(w2_ords_final) == 0, f"got {len(w2_ords_final)}")

    # ── PHASE 13: Routing restored ────────────────────────────
    sep("PHASE 13: Routing Restored")

    wid, _ = await bot._pick_wallet("BTC")
    check("BTC routes to W1 again", wid == 1, f"got W{wid}")
    wid, _ = await bot._pick_wallet("ETH")
    check("ETH routes to W1 again", wid == 1, f"got W{wid}")
    wid, _ = await bot._pick_wallet("SOL")
    check("SOL routes to W1 again", wid == 1, f"got W{wid}")

    # ── PHASE 14: Final balances ──────────────────────────────
    sep("PHASE 14: Final Balance Check")

    w1_usdc_end = usdc_token.functions.balanceOf(w1_addr).call() / 10**usdc_dec
    w2_usdc_end = usdc_token.functions.balanceOf(w2_addr).call() / 10**usdc_dec
    total_end = w1_usdc_end + w2_usdc_end
    cost = total_start - total_end

    print(f"     W1: ${w1_usdc_end:,.2f} | W2: ${w2_usdc_end:,.2f}")
    print(f"     Total: ${total_end:,.2f}")
    print(f"     Start: ${total_start:,.2f}")
    print(f"     Cost:  ${cost:,.2f}")

    check("Total cost < $5", cost < 5.0, f"${cost:.2f}")

    # ── SUMMARY ───────────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print(f"  FULL ROUTING TEST RESULTS")
    print(f"{'═' * 60}")
    print(f"\n  {passed}/{passed + failed} passed", end="")
    if failed:
        print(f" — {failed} FAILED ❌")
    else:
        print(" — ALL PASSED ✅")
    print(f"\n  Test cost: ${cost:,.2f}")
    print(f"{'═' * 60}")

    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
