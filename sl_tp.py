"""
Stop Loss & Take Profit (SL/TP) Mixin for GMX V2 Trading Bot.

Contains methods for:
  - Moving stop loss orders after TP hits
  - Monitoring TP price triggers
  - Manual SL/TP management via Telegram
  - Order creation/cancellation

SLTPMixin is designed to be mixed into GMXBot.

Expected host class attributes:
  cfg, logger, client, notify, send_message, w3, account, account2, account3, account4,
  positions, trade_history, health_stats,
  _all_wallets, _get_account, get_current_price, _fetch_all_positions_and_orders,
  send_message
"""

import asyncio
import time
import logging
import traceback
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from gmx import Position
from web3 import Web3

from open import (
    fetch_open_orders, create_sl_order, create_tp_order, TakeProfit,
    EXCHANGE_ROUTER_ABI, ORDER_TYPE_STOP_LOSS_DECREASE, ORDER_TYPE_LIMIT_DECREASE,
    fetch_current_price
)
import open as _open_mod
from close import fetch_positions as chain_fetch_positions, GMXPosition
from risk import verify_tp_hit_by_price, determine_new_sl_target, calculate_unrealized_pnl


logger = logging.getLogger("GMXBot.sl_tp")


class SLTPMixin:
    """Mixin providing SL/TP management for GMXBot."""

    # ──────────────────────────────────────────────────────────────────────
    # TP Monitor: check_tp_hits (run in background loop)
    # ──────────────────────────────────────────────────────────────────────

    async def check_tp_hits(self):
        """Monitor on-chain TP orders and track TP hits.

        Called from tp_monitor_loop every 5 seconds.
        For each open position:
          1. Fetch on-chain orders and count remaining TP orders
          2. If TP count decreased since last check → on-chain TP was executed
          3. Use price to identify WHICH TP was hit
          4. Move SL to entry or previous TP, notify admin

        On-chain verification prevents false TP hits from price tolerance.
        """
        for pos_id, pos in list(self.positions.items()):
            if not pos.is_open or not pos.take_profits:
                continue
            if not pos.market_addr:
                continue

            try:
                # Get current price
                current_price = await self.get_current_price(pos.symbol)
                if not current_price:
                    continue
                pos.current_price = current_price

                # Fetch on-chain orders to check if TP orders were executed
                acct = self._get_account(pos.wallet_id)
                orders = await asyncio.to_thread(
                    fetch_open_orders, self.w3, acct.address
                )

                # Count remaining TP orders on-chain for this market
                market_lower = pos.market_addr.lower()
                current_tp_count = sum(
                    1 for o in orders
                    if o["market"].lower() == market_lower
                    and o["order_type"] == ORDER_TYPE_LIMIT_DECREASE
                )

                # First run: initialize baseline count, don't trigger hits
                if pos.last_known_tp_count == 0 and current_tp_count > 0:
                    pos.last_known_tp_count = current_tp_count
                    self.logger.debug(
                        f"{pos.symbol}: initialized TP count baseline = {current_tp_count}"
                    )
                    continue

                # Check if on-chain TP orders decreased (keepers executed one)
                on_chain_hits = pos.last_known_tp_count - current_tp_count
                pos.last_known_tp_count = current_tp_count

                if on_chain_hits <= 0:
                    continue  # no on-chain TP was executed

                self.logger.info(
                    f"{pos.symbol}: {on_chain_hits} on-chain TP order(s) filled "
                    f"(remaining: {current_tp_count})"
                )

                # Identify which TPs were hit using price verification
                if pos.side == "LONG":
                    sorted_tps = sorted(pos.take_profits, key=lambda tp: tp.price)
                else:
                    sorted_tps = sorted(pos.take_profits, key=lambda tp: tp.price, reverse=True)

                new_hits = 0
                for i, tp in enumerate(sorted_tps):
                    if new_hits >= on_chain_hits:
                        break  # matched all on-chain hits
                    if not tp.executed and verify_tp_hit_by_price(pos.side == "LONG", tp.price, current_price):
                        tp.executed = True
                        tp.executed_at = time.time()
                        new_hits += 1
                        self.logger.info(f"{pos.symbol} TP{i+1} HIT @ ${current_price:,.0f} (on-chain confirmed)")

                # Implied hits: if a later TP was verified, all earlier TPs
                # (closer to entry) must have been hit too — price has to pass
                # through them. This handles price-bounce scenarios where the
                # keeper executed multiple TPs but price bounced back before
                # our check ran.
                if new_hits > 0 and new_hits < on_chain_hits:
                    farthest_idx = -1
                    for i, tp in enumerate(sorted_tps):
                        if tp.executed:
                            farthest_idx = i
                    for i in range(farthest_idx):
                        if not sorted_tps[i].executed:
                            sorted_tps[i].executed = True
                            sorted_tps[i].executed_at = time.time()
                            new_hits += 1
                            self.logger.info(
                                f"{pos.symbol} TP{i+1} HIT (implied — price passed through)"
                            )

                # If on-chain TP count decreased but NO price verified at all,
                # this may be manual cancellation — don't move SL.
                if new_hits == 0 and on_chain_hits > 0:
                    self.logger.warning(
                        f"{pos.symbol}: {on_chain_hits} on-chain TP order(s) disappeared "
                        f"but price ${current_price:,.0f} didn't verify any. "
                        f"Possible manual cancellation — NOT moving SL."
                    )
                    await self.notify(
                        f"⚠️ {pos.symbol} {pos.side} [W{pos.wallet_id}]: "
                        f"{on_chain_hits} TP order(s) disappeared on-chain but "
                        f"price ${current_price:,.0f} didn't reach any TP level.\n"
                        f"SL NOT moved — may have been manually cancelled."
                    )

                if new_hits > 0:
                    pos.tp_hits_count += new_hits
                    self.logger.info(f"{pos.symbol} {pos.side}: {new_hits} new TP hits, total={pos.tp_hits_count}")

                    # Calculate realized PnL from all executed TPs
                    executed_tps = [tp for tp in pos.take_profits if tp.executed]
                    remaining_tps = [tp for tp in pos.take_profits if not tp.executed]
                    realized_pnl = 0.0
                    for tp in executed_tps:
                        tp_size = pos.size_usd * tp.percentage
                        if pos.side == "LONG":
                            realized_pnl += ((tp.price - pos.entry_price) / pos.entry_price) * tp_size
                        else:
                            realized_pnl += ((pos.entry_price - tp.price) / pos.entry_price) * tp_size
                    pos.realized_pnl = realized_pnl

                    # Calculate unrealized PnL on remaining position
                    remaining_size = pos.size_usd * sum(tp.percentage for tp in remaining_tps)
                    unrealized_pnl = calculate_unrealized_pnl(
                        pos.side, pos.entry_price, current_price, remaining_size
                    )
                    total_pnl = realized_pnl + unrealized_pnl
                    r_sign = "+" if realized_pnl >= 0 else ""
                    u_sign = "+" if unrealized_pnl >= 0 else ""
                    t_sign = "+" if total_pnl >= 0 else ""

                    try:
                        await self.notify(
                            f"✅ {pos.symbol} {pos.side} [W{pos.wallet_id}]: "
                            f"TP{pos.tp_hits_count} hit @ ${current_price:,.0f}\n"
                            f"TPs: {len(executed_tps)}/{len(pos.take_profits)} | "
                            f"Remaining: {current_tp_count}\n"
                            f"Realized:   {r_sign}${realized_pnl:,.2f}\n"
                            f"Unrealized: {u_sign}${unrealized_pnl:,.2f}\n"
                            f"Total PnL:  {t_sign}${total_pnl:,.2f}"
                        )
                    except Exception:
                        pass

                    # Move SL to entry or previous TP
                    try:
                        await self.move_sl(pos, orders)
                    except Exception as e:
                        self.logger.warning(f"Failed to move SL after TP hit: {e}")
                        await self.notify(
                            f"⚠️ {pos.symbol} {pos.side} [W{pos.wallet_id}]: "
                            f"TP hit but failed to move SL: {e}"
                        )

            except Exception as e:
                self.logger.debug(f"Error checking TPs for {pos.symbol}: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # Move SL after TP hits or manual command
    # ──────────────────────────────────────────────────────────────────────

    async def move_sl(self, pos: "Position", orders: list, new_sl_price: Optional[float] = None, sl_label: Optional[str] = None, *, manual: bool = False):
        """Move SL to entry (breakeven) or previous TP after TP hit(s).

        Called in two ways:
          1. Auto mode (from check_tp_hits): new_sl_price=None, sl_label=None
             → Auto-compute target using determine_new_sl_target()
          2. Manual mode (from cmd_sl): new_sl_price and sl_label provided, manual=True
             → Use them directly; skip the auto notification (cmd_sl sends its own)

        On-chain flow:
          1. Cancel existing SL order(s) for this market
          2. Create new SL order at the new price
          3. Update in-memory state
          4. Notify admin (auto mode only)

        Args:
            pos: Internal Position object
            orders: List of open orders (for this wallet)
            new_sl_price: (Optional) Manual SL price (from cmd_sl). If None, auto-compute.
            sl_label: (Optional) Label for manual SL (e.g., "Entry", "TP1", "TP2")
            manual: If True, suppress the notification (caller handles its own).
        """
        try:
            if pos.tp_hits_count == 0 and new_sl_price is None:
                return

            # Auto-compute target if not provided manually
            if new_sl_price is None:
                sorted_tps = sorted(
                    pos.take_profits,
                    key=lambda tp: tp.price,
                    reverse=(pos.side == "SHORT")
                )
                new_sl_price, sl_label = determine_new_sl_target(pos.tp_hits_count, pos.entry_price, sorted_tps)

            self.logger.info(f"{pos.symbol} {pos.side}: Moving SL to {sl_label} (${new_sl_price:,.0f})")

            cfg = self.cfg
            acct = self._get_account(pos.wallet_id)
            exchange = self.w3.eth.contract(
                address=Web3.to_checksum_address(cfg.exchange_router),
                abi=EXCHANGE_ROUTER_ABI,
            )
            wallet = Web3.to_checksum_address(acct.address)

            # 1. Cancel existing SL orders for this market
            # Re-fetch orders fresh to get accurate keys (avoids stale key misalignment)
            market_lower = pos.market_addr.lower() if pos.market_addr else ""
            cancelled = 0
            for cleanup_round in range(3):
                fresh_orders = await asyncio.to_thread(fetch_open_orders, self.w3, acct.address)
                sl_orders = [
                    o for o in fresh_orders
                    if o["market"].lower() == market_lower
                    and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE
                    and o.get("key_hex")
                ]
                if not sl_orders:
                    break
                for sl_order in sl_orders:
                    try:
                        key_bytes = bytes.fromhex(sl_order["key_hex"])
                        data = exchange.encode_abi("cancelOrder", [key_bytes])
                        tx = _open_mod.build_tx(self.w3, wallet, exchange.address, data, value=0)
                        txh = _open_mod.sign_send(self.w3, acct, tx, dry_run=cfg.dry_run)
                        if not cfg.dry_run:
                            _open_mod.wait_receipt(self.w3, txh)
                        cancelled += 1
                        self.logger.info(f"Cancelled old SL order for {pos.symbol}: {txh}")
                    except Exception as e:
                        self.logger.warning(f"Cancel SL failed for {pos.symbol}: {e}")
                await asyncio.sleep(2)

            # 2. Create new SL order at new price
            order_vault = Web3.to_checksum_address(cfg.order_vault)
            collateral_token = Web3.to_checksum_address(cfg.collateral_token)

            # Calculate remaining position size after executed TPs
            remaining_size = pos.size_usd
            if pos.take_profits:
                executed_pct = sum(tp.percentage for tp in pos.take_profits if tp.executed)
                remaining_size = pos.size_usd * max(1.0 - executed_pct, 0.01)

            try:
                new_sl_txh = await asyncio.to_thread(
                    create_sl_order,
                    self.w3, acct, exchange, acct.address,
                    pos.market_addr, collateral_token, order_vault,
                    new_sl_price, remaining_size, pos.symbol, pos.side == "LONG",
                    cfg.slippage_bps, cfg.execution_fee_wei, cfg.dry_run,
                )
                self.logger.info(f"New SL order created for {pos.symbol} at ${new_sl_price:,.0f} size=${remaining_size:,.2f}: {new_sl_txh}")
            except Exception as e:
                self.logger.error(f"Failed to create new SL order for {pos.symbol}: {e}")
                pos.sl_move_failed = True
                await self.notify(
                    f"⚠️ {pos.symbol} {pos.side} [W{pos.wallet_id}]: "
                    f"Failed to move SL to {sl_label} (${new_sl_price:,.2f})\n"
                    f"Error: {e}\n"
                    f"Old SL cancelled ({cancelled}). Manual intervention may be needed."
                )
                return

            # 3. Update in-memory state
            pos.sl_moved_to_entry = True
            pos.sl_move_label = sl_label
            pos.stop_loss = new_sl_price
            pos.sl_move_failed = False

            # 4. Notify admin (auto mode only — manual callers send their own)
            if not manual:
                await self.notify(
                    f"🎯 {pos.symbol} {pos.side} [W{pos.wallet_id}]: "
                    f"{pos.tp_hits_count} TP(s) hit!\n"
                    f"SL moved to {sl_label} (${new_sl_price:,.2f})\n"
                    f"TX: {new_sl_txh}"
                )

        except Exception as e:
            self.logger.error(f"Error in move_sl: {e}")
            pos.sl_move_failed = True

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /sl <#> <target>
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_sl(self, chat_id: int, arg: Optional[str]):
        """Telegram /sl command handler.

        Manually move SL to entry (breakeven) or a specific TP level.

        Usage:
            /sl                   — show open positions & available SL targets
            /sl 1 entry           — move position #1 SL to entry price
            /sl 1 tp2             — move position #1 SL to TP2 price
            /sl 1 tp3             — move position #1 SL to TP3 price (etc.)

        Args:
            chat_id: Telegram chat ID
            arg: "<position_number> <target>" where target is "entry" or "tp1"-"tp8"
        """
        try:
            positions, orders = await self._fetch_all_positions_and_orders()
            if not positions:
                await self.send_message(chat_id, "No open positions.")
                return

            if not arg or not arg.strip():
                msg = "**Move Stop Loss**\n\nUsage: `/sl <#> <target>`\n\n"
                for i, pos in enumerate(positions, 1):
                    side = "LONG" if pos.is_long else "SHORT"
                    wid_label = f" [W{pos._wallet_id}]" if hasattr(pos, '_wallet_id') else ""
                    market_lower = pos.market.lower()
                    sl_orders = [o for o in orders
                                 if o["market"].lower() == market_lower
                                 and o["order_type"] == ORDER_TYPE_STOP_LOSS_DECREASE]
                    current_sl = f"${sl_orders[0]['trigger_price']:,.2f}" if sl_orders else "None"

                    internal_pos = None
                    for p in self.positions.values():
                        if (p.is_open and p.market_addr
                                and p.market_addr.lower() == market_lower
                                and p.side == side):
                            internal_pos = p
                            break

                    targets = ["entry"]
                    if internal_pos and internal_pos.take_profits:
                        sorted_tps = sorted(internal_pos.take_profits,
                                            key=lambda t: t.price,
                                            reverse=(side == "SHORT"))
                        for j, tp in enumerate(sorted_tps, 1):
                            targets.append(f"tp{j} (${tp.price:,.2f})")

                    msg += (
                        f"**#{i} {pos.symbol} {side}{wid_label}**\n"
                        f"  Entry: ${pos.entry_price:,.2f} | SL: {current_sl}\n"
                        f"  Targets: {', '.join(targets)}\n\n"
                    )
                msg += "Example: `/sl 1 entry` or `/sl 1 tp2`"
                await self.send_message(chat_id, msg)
                return

            parts = arg.strip().split()
            if len(parts) < 2:
                await self.send_message(chat_id, "Usage: `/sl <#> <target>`\nExample: `/sl 1 entry` or `/sl 1 tp2`")
                return

            try:
                pos_num = int(parts[0])
            except ValueError:
                await self.send_message(chat_id, f"Invalid position number: {parts[0]}")
                return

            target = parts[1].lower().strip()

            if pos_num < 1 or pos_num > len(positions):
                await self.send_message(chat_id, f"Position #{pos_num} not found. Use /sl to see available positions.")
                return

            chain_pos = positions[pos_num - 1]
            side = "LONG" if chain_pos.is_long else "SHORT"
            market_lower = chain_pos.market.lower()

            internal_pos = None
            for p in self.positions.values():
                if (p.is_open and p.market_addr
                        and p.market_addr.lower() == market_lower
                        and p.side == side):
                    internal_pos = p
                    break

            if not internal_pos:
                await self.send_message(
                    chat_id,
                    f"Position #{pos_num} ({chain_pos.symbol} {side}) not tracked internally. "
                    "Cannot resolve TP prices — try using /addorder to set TP levels first."
                )
                return

            if target == "entry":
                new_sl_price = internal_pos.entry_price
                sl_label = "Entry"
            elif target.startswith("tp"):
                try:
                    tp_num = int(target[2:])
                except ValueError:
                    await self.send_message(chat_id, f"Invalid target: {target}. Use 'entry' or 'tp1'-'tp8'.")
                    return
                sorted_tps = sorted(internal_pos.take_profits,
                                    key=lambda t: t.price,
                                    reverse=(side == "SHORT"))
                if tp_num < 1 or tp_num > len(sorted_tps):
                    await self.send_message(chat_id, f"TP{tp_num} not found. Position has {len(sorted_tps)} TP(s).")
                    return
                new_sl_price = sorted_tps[tp_num - 1].price
                sl_label = f"TP{tp_num}"
            else:
                await self.send_message(chat_id, f"Invalid target: {target}. Use 'entry' or 'tp1'-'tp8'.")
                return

            current_price = chain_pos.current_price or await self.get_current_price(chain_pos.symbol)
            if current_price:
                if side == "LONG" and new_sl_price >= current_price:
                    await self.send_message(
                        chat_id,
                        f"⚠️ Warning: SL ${new_sl_price:,.2f} is above current price "
                        f"${current_price:,.2f} for a LONG. This would close the position immediately.\n"
                        "Proceeding anyway..."
                    )
                elif side == "SHORT" and new_sl_price <= current_price:
                    await self.send_message(
                        chat_id,
                        f"⚠️ Warning: SL ${new_sl_price:,.2f} is below current price "
                        f"${current_price:,.2f} for a SHORT. This would close the position immediately.\n"
                        "Proceeding anyway..."
                    )

            pos_acct = self._get_account(internal_pos.wallet_id)
            try:
                fresh_orders = await asyncio.to_thread(fetch_open_orders, self.w3, pos_acct.address)
            except Exception as e:
                await self.send_message(chat_id, f"Error fetching orders: {e}")
                return

            # Call move_sl with manual=True so it skips its own notification
            await self.move_sl(internal_pos, fresh_orders, new_sl_price, sl_label, manual=True)

            if internal_pos.sl_move_failed:
                # move_sl already sent a failure notification via notify()
                await self.send_message(
                    chat_id,
                    f"❌ Failed to move SL for #{pos_num} {internal_pos.symbol} {side}. Check logs."
                )
            else:
                await self.send_message(
                    chat_id,
                    f"✅ SL moved for #{pos_num} {internal_pos.symbol} {side} [W{internal_pos.wallet_id}] "
                    f"→ {sl_label} (${new_sl_price:,.2f})"
                )

        except Exception as e:
            self.logger.error(f"cmd_sl error: {e}\n{traceback.format_exc()}")
            await self.send_message(chat_id, f"Error: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /addorder <sl|tp> <#> <prices...>
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_addorder(self, chat_id: int, arg: Optional[str]):
        """Telegram /addorder command handler.

        Manually add SL or TP orders to an open position.

        Usage:
            /addorder                           — show available positions
            /addorder sl 1 95000                — add SL to position #1 at $95k
            /addorder tp 1 100000               — add single TP to position #1 at $100k
            /addorder tp 1 98000 100000 103000  — add multiple TPs (split remaining size evenly)

        TPs are allocated by weight based on how many already exist:
          - Existing TPs are ranked 1, 2, ...
          - New TPs get higher weight numbers
          - Size = remaining_size * (weight / total_weight_sum)

        Args:
            chat_id: Telegram chat ID
            arg: "<sl|tp> <position_number> <price1> [price2] [price3] ..."
        """
        cfg = self.cfg
        await self.send_message(chat_id, "Fetching positions...")
        try:
            positions, orders = await self._fetch_all_positions_and_orders()
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching positions: {e}")
            return

        if not positions:
            await self.send_message(chat_id, "No open positions on-chain.")
            return

        if arg is None:
            msg = "**Open Positions**\n\n"
            for i, pos in enumerate(positions, 1):
                side = "LONG" if pos.is_long else "SHORT"
                wid = ""
                if hasattr(pos, '_wallet_id'):
                    wid = f" [W{pos._wallet_id}]"
                pos_orders = [o for o in orders if o["market"].lower() == pos.market.lower()]
                sl_orders = [o for o in pos_orders if o["order_type"] == 6]
                tp_orders = sorted([o for o in pos_orders if o["order_type"] == 5], key=lambda o: o["trigger_price"])
                msg += (
                    f"**#{i} {pos.symbol} {side}{wid}**\n"
                    f"  Size: ${pos.size_usd:,.2f} @ {pos.leverage:.1f}x\n"
                    f"  Entry: ${pos.entry_price:,.2f}  |  Current: ${pos.current_price:,.2f}\n"
                )
                if sl_orders:
                    for o in sl_orders:
                        msg += f"  SL @ ${o['trigger_price']:,.2f}\n"
                if tp_orders:
                    for j, o in enumerate(tp_orders, 1):
                        msg += f"  TP{j} @ ${o['trigger_price']:,.2f}  (${o['size_usd']:,.2f})\n"
                existing_tp_size = sum(o["size_usd"] for o in tp_orders)
                remaining = max(0, pos.size_usd - existing_tp_size)
                remaining_pct = (remaining / pos.size_usd * 100) if pos.size_usd > 0 else 0
                if tp_orders:
                    msg += f"  Remaining: ${remaining:,.2f} ({remaining_pct:.0f}%)\n"
                msg += "\n"

            msg += "**Usage:**\n"
            msg += "  /addorder sl 1 95000              — add SL on #1\n"
            msg += "  /addorder tp 1 100000             — single TP (uses all remaining size)\n"
            msg += "  /addorder tp 1 98000 100000 103000 — multiple TPs (split remaining evenly)"
            await self.send_message(chat_id, msg)
            return

        parts = arg.strip().split()
        if len(parts) < 3:
            await self.send_message(
                chat_id,
                "Usage:\n  /addorder sl <#> <price>\n  /addorder tp <#> <price>           — single TP (auto-size)\n  /addorder tp <#> <p1> <p2> <p3>    — multiple TPs (split evenly)"
            )
            return

        order_kind = parts[0].upper()
        if order_kind not in ("SL", "TP"):
            await self.send_message(chat_id, "First argument must be 'sl' or 'tp'.")
            return

        try:
            pos_num = int(parts[1])
        except ValueError:
            await self.send_message(chat_id, "Position number must be an integer.")
            return
        if pos_num < 1 or pos_num > len(positions):
            await self.send_message(chat_id, f"Position #{pos_num} out of range (1-{len(positions)}).")
            return

        pos = positions[pos_num - 1]
        acct = getattr(pos, '_wallet_acct', self.account)
        side = "LONG" if pos.is_long else "SHORT"

        exchange = self.w3.eth.contract(
            address=Web3.to_checksum_address(cfg.exchange_router),
            abi=EXCHANGE_ROUTER_ABI,
        )
        order_vault = Web3.to_checksum_address(cfg.order_vault)
        collateral_token = Web3.to_checksum_address(cfg.collateral_token)

        if order_kind == "SL":
            try:
                price = float(parts[2].replace(",", ""))
            except ValueError:
                await self.send_message(chat_id, "Price must be a number.")
                return
            if price <= 0:
                await self.send_message(chat_id, "Price must be positive.")
                return
            if pos.is_long and price >= pos.entry_price:
                await self.send_message(chat_id, f"Warning: SL ${price:,.2f} is above entry ${pos.entry_price:,.2f} for a LONG. Send again to confirm.")
                return
            elif not pos.is_long and price <= pos.entry_price:
                await self.send_message(chat_id, f"Warning: SL ${price:,.2f} is below entry ${pos.entry_price:,.2f} for a SHORT. Send again to confirm.")
                return

            try:
                self.logger.info(f"addorder: placing SL on {pos.symbol} {side} at ${price:,.2f} size=${pos.size_usd:,.2f}")
                txh = await asyncio.to_thread(
                    create_sl_order,
                    self.w3, acct, exchange, acct.address,
                    pos.market, collateral_token, order_vault,
                    price, pos.size_usd, pos.symbol, pos.is_long,
                    cfg.slippage_bps, cfg.execution_fee_wei, cfg.dry_run,
                )
                await self.send_message(
                    chat_id,
                    f"SL placed on {pos.symbol} {side} @ ${price:,.2f}\nSize: ${pos.size_usd:,.2f} (100% close)\nTX: {txh}"
                )
            except Exception as e:
                self.logger.error(f"addorder SL failed: {e}\n{traceback.format_exc()}")
                await self.send_message(chat_id, f"Failed to place SL: {e}")
            return

        # ── TP (single or multiple) ──
        price_strs = parts[2:]
        tp_prices = []
        for ps in price_strs:
            try:
                p = float(ps.replace(",", ""))
                if p <= 0:
                    await self.send_message(chat_id, f"Price must be positive, got: {ps}")
                    return
                tp_prices.append(p)
            except ValueError:
                await self.send_message(chat_id, f"Invalid price: '{ps}'")
                return

        if not tp_prices:
            await self.send_message(chat_id, "Provide at least one TP price.")
            return

        pos_tp_orders = [o for o in orders if o["market"].lower() == pos.market.lower() and o["order_type"] == 5]
        existing_tp_size = sum(o["size_usd"] for o in pos_tp_orders)
        remaining_size = max(0, pos.size_usd - existing_tp_size)

        if remaining_size <= 0:
            await self.send_message(
                chat_id,
                f"Existing TPs already cover the full position size (${existing_tp_size:,.2f} / ${pos.size_usd:,.2f}).\nCancel a TP first with /cancelorder."
            )
            return

        num_existing = len(pos_tp_orders)
        num_new = len(tp_prices)
        total_tps = num_existing + num_new
        new_weights = [i for i in range(num_existing + 1, total_tps + 1)]
        weight_sum = sum(new_weights)

        tp_allocations = []
        for i, tp_price in enumerate(sorted(tp_prices)):
            tp_size = remaining_size * (new_weights[i] / weight_sum)
            tp_pct = tp_size / pos.size_usd
            tp_allocations.append((tp_price, tp_size, tp_pct))

        results = []
        for tp_price, tp_size, tp_pct in tp_allocations:
            tp = TakeProfit(price=tp_price, close_pct=tp_pct)
            try:
                txh = await asyncio.to_thread(
                    create_tp_order,
                    self.w3, acct, exchange, acct.address,
                    pos.market, collateral_token, order_vault,
                    tp, pos.size_usd, pos.symbol, pos.is_long,
                    cfg.slippage_bps, cfg.execution_fee_wei, cfg.dry_run,
                )
                results.append((tp_price, tp_size, tp_pct * 100, txh, None))
            except Exception as e:
                self.logger.error(f"addorder TP @ ${tp_price:,.2f} failed: {e}")
                results.append((tp_price, tp_size, tp_pct * 100, None, str(e)))

        ok = [r for r in results if r[3]]
        fail = [r for r in results if r[4]]

        msg = f"**Add TP — {pos.symbol} {side}**\n\n"
        for tp_price, size, pct, txh, err in results:
            if txh:
                msg += f"  TP @ ${tp_price:,.2f} — ${size:,.2f} ({pct:.0f}%) — {txh}\n"
            else:
                msg += f"  TP @ ${tp_price:,.2f} — FAILED: {err}\n"

        if ok and not fail:
            msg += f"\n{len(ok)} TP(s) placed successfully."
        elif ok and fail:
            msg += f"\n{len(ok)} placed, {len(fail)} failed."
        else:
            msg += "\nAll TPs failed."

        await self.send_message(chat_id, msg)

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /cancelorder [#|all]
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_cancelorder(self, chat_id: int, arg: Optional[str]):
        """Telegram /cancelorder command handler.

        List and cancel individual SL/TP orders by number.

        Usage:
            /cancelorder              — list all open orders with numbers
            /cancelorder 3            — cancel order #3
            /cancelorder 1,3,5        — cancel multiple orders
            /cancelorder all          — cancel all cancellable orders (SL, TP, LimitIncrease)

        Market orders (MarketIncrease, MarketDecrease) cannot be cancelled (they execute immediately).

        Args:
            chat_id: Telegram chat ID
            arg: "<order_number>[,<number>...]" or "all"
        """
        cfg = self.cfg
        await self.send_message(chat_id, "Fetching open orders...")
        try:
            _positions, all_orders = await self._fetch_all_positions_and_orders()
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching orders: {e}")
            return

        ORDER_TYPE_NAMES = {2: "MarketInc", 3: "LimitInc", 4: "MarketDec", 5: "TP", 6: "SL"}
        CANCELLABLE = {3, 5, 6}

        if not all_orders:
            await self.send_message(chat_id, "No open orders on-chain.")
            return

        numbered = []
        for o in all_orders:
            o_type = o["order_type"]
            cancellable = o_type in CANCELLABLE and o.get("key_hex")
            numbered.append({**o, "_cancellable": cancellable})

        if arg is None:
            msg = "**Open Orders**\n\n"
            for i, o in enumerate(numbered, 1):
                label = ORDER_TYPE_NAMES.get(o["order_type"], f"Type{o['order_type']}")
                side = "LONG" if o["is_long"] else "SHORT"
                cancel_mark = "" if o["_cancellable"] else " (not cancellable)"
                wid = ""
                if o.get("_wallet_id"):
                    wid = f" [W{o['_wallet_id']}]"
                msg += (
                    f"**#{i}** {o['symbol']} {side} — {label} "
                    f"@ ${o['trigger_price']:,.2f}  "
                    f"(${o['size_usd']:,.2f}){wid}{cancel_mark}\n"
                )
            msg += "\nReply with:\n  /cancelorder 3    — cancel order #3\n  /cancelorder 1,3  — cancel multiple\n  /cancelorder all  — cancel all"
            await self.send_message(chat_id, msg)
            return

        arg_stripped = arg.strip().upper()
        if arg_stripped == "ALL":
            indices = [i for i, o in enumerate(numbered) if o["_cancellable"]]
        else:
            try:
                indices = []
                for part in arg.replace(" ", ",").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    num = int(part)
                    if num < 1 or num > len(numbered):
                        await self.send_message(chat_id, f"Order #{num} out of range (1-{len(numbered)})")
                        return
                    indices.append(num - 1)
            except ValueError:
                await self.send_message(chat_id, "Usage: /cancelorder 3  or  /cancelorder 1,3,5  or  /cancelorder all")
                return

        if not indices:
            await self.send_message(chat_id, "No cancellable orders selected.")
            return

        for idx in indices:
            o = numbered[idx]
            if not o["_cancellable"]:
                label = ORDER_TYPE_NAMES.get(o["order_type"], f"Type{o['order_type']}")
                await self.send_message(
                    chat_id,
                    f"Order #{idx+1} ({o['symbol']} {label}) cannot be cancelled (market orders execute immediately)."
                )
                return

        exchange = self.w3.eth.contract(
            address=Web3.to_checksum_address(cfg.exchange_router),
            abi=EXCHANGE_ROUTER_ABI,
        )

        cancelled = 0
        failed = 0
        for idx in indices:
            o = numbered[idx]
            label = ORDER_TYPE_NAMES.get(o["order_type"], f"Type{o['order_type']}")
            key_bytes = bytes.fromhex(o["key_hex"])
            acct = o.get("_wallet_acct", self.account)
            wallet = Web3.to_checksum_address(acct.address)

            self.logger.info(f"cancelorder: cancelling #{idx+1} {o['symbol']} {label} key=0x{o['key_hex'][:16]}...")

            if cfg.dry_run:
                self.logger.info(f"  [DRY_RUN] Would cancel order #{idx+1}")
                cancelled += 1
                continue

            try:
                data = exchange.encode_abi("cancelOrder", [key_bytes])
                tx = _open_mod.build_tx(self.w3, wallet, exchange.address, data, value=0)
                txh = _open_mod.sign_send(self.w3, acct, tx, dry_run=False)
                receipt = _open_mod.wait_receipt(self.w3, txh)
                if receipt.get("status") == 1:
                    self.logger.info(f"  Cancelled #{idx+1}: {txh}")
                    cancelled += 1
                else:
                    self.logger.warning(f"  Cancel tx reverted for #{idx+1}: {txh}")
                    failed += 1
            except Exception as e:
                self.logger.warning(f"  Failed to cancel #{idx+1}: {e}")
                failed += 1

        parts_msg = []
        if cancelled:
            parts_msg.append(f"{cancelled} cancelled")
        if failed:
            parts_msg.append(f"{failed} failed")
        summary = ", ".join(parts_msg)

        detail_lines = []
        for idx in indices:
            o = numbered[idx]
            label = ORDER_TYPE_NAMES.get(o["order_type"], f"Type{o['order_type']}")
            detail_lines.append(f"  #{idx+1} {o['symbol']} {label} @ ${o['trigger_price']:,.2f}")

        msg = f"**Cancel Orders: {summary}**\n" + "\n".join(detail_lines)
        await self.send_message(chat_id, msg)
