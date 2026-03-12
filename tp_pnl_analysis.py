"""
TP PnL Analysis — Pull last 250 signals from monitored channel
and calculate the average %PnL from entry to TP1, TP2, TP3, TP4.

Usage:  python tp_pnl_analysis.py
"""

import os
import sys
import asyncio
import statistics
from collections import defaultdict
from typing import List

from dotenv import load_dotenv
from telethon import TelegramClient

# Add commercial-trading-bot to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "commercial-trading-bot"))

from config import load_config, parse_telegram_channels
from open import parse_signal, classify_signal
from risk import is_update_message


load_dotenv(os.path.join(os.path.dirname(__file__), "commercial-trading-bot", ".env"))


async def main():
    cfg = load_config()
    channels = cfg.telegram_channels

    if not channels:
        print("ERROR: No channels configured in .env (TELEGRAM_CHANNELS)")
        sys.exit(1)

    client = TelegramClient(
        cfg.telegram_session,
        cfg.telegram_api_id,
        cfg.telegram_api_hash,
    )
    await client.start()

    # Collect signals across all monitored channels
    all_signals = []

    for channel in channels:
        try:
            ch_id = int(channel)
        except ValueError:
            ch_id = channel

        try:
            entity = await client.get_entity(ch_id)
            name = getattr(entity, "title", str(ch_id))
        except Exception as e:
            print(f"Could not resolve channel {channel}: {e}")
            continue

        print(f"Fetching messages from: {name} ...")

        # Fetch enough messages to get 250 signals (signals are a subset of messages)
        messages = []
        async for msg in client.iter_messages(ch_id, limit=5000, reverse=False):
            if msg.text and len(msg.text.strip()) >= 10:
                messages.append(msg)

        print(f"  Fetched {len(messages)} text messages")

        # Parse signals (skip update messages)
        for msg in messages:
            text = msg.text.strip()
            if is_update_message(text):
                continue
            try:
                sig = parse_signal(text)
                classify_signal(sig)
                all_signals.append(sig)
            except Exception:
                pass

    print(f"\nTotal signals parsed: {len(all_signals)}")

    # Take last 250 (messages fetched newest-first, so first 250 are the latest)
    signals = all_signals[:250]
    print(f"Analyzing last {len(signals)} signals\n")

    if not signals:
        print("No signals found.")
        await client.disconnect()
        return

    # Calculate %PnL from entry to each TP level
    # For LONG:  pnl% = (TP - entry) / entry * 100
    # For SHORT: pnl% = (entry - TP) / entry * 100
    # With leverage: pnl% * leverage

    tp_pnls_no_lev = defaultdict(list)   # without leverage
    tp_pnls_with_lev = defaultdict(list)  # with leverage

    for sig in signals:
        entry = (sig.entry_low + sig.entry_high) / 2
        if entry == 0:
            continue

        is_long = sig.side == "LONG"

        for i, tp in enumerate(sig.take_profits):
            level = i + 1
            if level > 4:
                break

            if is_long:
                pnl_pct = (tp.price - entry) / entry * 100
            else:
                pnl_pct = (entry - tp.price) / entry * 100

            tp_pnls_no_lev[level].append(pnl_pct)
            tp_pnls_with_lev[level].append(pnl_pct * sig.leverage)

    # Print results
    W = 65
    print("=" * W)
    print("  AVG %PnL FROM ENTRY TO TAKE PROFIT (Last 250 Signals)")
    print("=" * W)

    print(f"\n{'--- Without Leverage ---':^{W}}")
    print(f"  {'Level':<8} {'Avg %PnL':>10} {'Median':>10} {'Min':>10} {'Max':>10} {'Count':>7}")
    print(f"  {'-'*55}")
    for level in range(1, 5):
        vals = tp_pnls_no_lev.get(level, [])
        if vals:
            print(f"  TP{level:<5} {statistics.mean(vals):>+10.2f}% {statistics.median(vals):>+9.2f}%"
                  f" {min(vals):>+9.2f}% {max(vals):>+9.2f}% {len(vals):>7}")
        else:
            print(f"  TP{level:<5} {'N/A':>10} {'N/A':>10} {'N/A':>10} {'N/A':>10} {'0':>7}")

    print(f"\n{'--- With Leverage ---':^{W}}")
    print(f"  {'Level':<8} {'Avg %PnL':>10} {'Median':>10} {'Min':>10} {'Max':>10} {'Count':>7}")
    print(f"  {'-'*55}")
    for level in range(1, 5):
        vals = tp_pnls_with_lev.get(level, [])
        if vals:
            print(f"  TP{level:<5} {statistics.mean(vals):>+10.2f}% {statistics.median(vals):>+9.2f}%"
                  f" {min(vals):>+9.2f}% {max(vals):>+9.2f}% {len(vals):>7}")
        else:
            print(f"  TP{level:<5} {'N/A':>10} {'N/A':>10} {'N/A':>10} {'N/A':>10} {'0':>7}")

    # Breakdown by side
    for side in ["LONG", "SHORT"]:
        side_sigs = [s for s in signals if s.side == side]
        if not side_sigs:
            continue

        print(f"\n{'--- ' + side + ' Only (No Leverage) ---':^{W}}")
        print(f"  {'Level':<8} {'Avg %PnL':>10} {'Median':>10} {'Count':>7}")
        print(f"  {'-'*35}")

        for level in range(1, 5):
            vals = []
            for sig in side_sigs:
                entry = (sig.entry_low + sig.entry_high) / 2
                if entry == 0 or len(sig.take_profits) < level:
                    continue
                tp = sig.take_profits[level - 1]
                if side == "LONG":
                    pnl = (tp.price - entry) / entry * 100
                else:
                    pnl = (entry - tp.price) / entry * 100
                vals.append(pnl)

            if vals:
                print(f"  TP{level:<5} {statistics.mean(vals):>+10.2f}% {statistics.median(vals):>+9.2f}% {len(vals):>7}")
            else:
                print(f"  TP{level:<5} {'N/A':>10} {'N/A':>10} {'0':>7}")

    # Breakdown by trade type
    for ttype in ["swing", "scalp"]:
        type_sigs = [s for s in signals if s.trade_type == ttype]
        if not type_sigs:
            continue

        print(f"\n{'--- ' + ttype.upper() + ' Only (With Leverage) ---':^{W}}")
        print(f"  {'Level':<8} {'Avg %PnL':>10} {'Median':>10} {'Avg Lev':>9} {'Count':>7}")
        print(f"  {'-'*45}")

        for level in range(1, 5):
            vals = []
            levs = []
            for sig in type_sigs:
                entry = (sig.entry_low + sig.entry_high) / 2
                if entry == 0 or len(sig.take_profits) < level:
                    continue
                tp = sig.take_profits[level - 1]
                if sig.side == "LONG":
                    pnl = (tp.price - entry) / entry * 100
                else:
                    pnl = (entry - tp.price) / entry * 100
                vals.append(pnl * sig.leverage)
                levs.append(sig.leverage)

            if vals:
                print(f"  TP{level:<5} {statistics.mean(vals):>+10.2f}% {statistics.median(vals):>+9.2f}%"
                      f" {statistics.mean(levs):>8.1f}x {len(vals):>7}")
            else:
                print(f"  TP{level:<5} {'N/A':>10} {'N/A':>10} {'N/A':>9} {'0':>7}")

    # Summary stats
    print(f"\n{'--- Summary ---':^{W}}")
    long_count = len([s for s in signals if s.side == "LONG"])
    short_count = len(signals) - long_count
    swing_count = len([s for s in signals if s.trade_type == "swing"])
    scalp_count = len(signals) - swing_count
    leverages = [s.leverage for s in signals]

    print(f"  Signals analyzed:  {len(signals)}")
    print(f"  Long / Short:      {long_count} / {short_count}")
    print(f"  Swing / Scalp:     {swing_count} / {scalp_count}")
    print(f"  Avg leverage:      {statistics.mean(leverages):.1f}x")
    print(f"  Median leverage:   {statistics.median(leverages):.1f}x")

    print("\n" + "=" * W)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
