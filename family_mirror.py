"""
Family Mirror Mixin for GMX V2 Trading Bot.

Mirrors trades to family members' GMX wallets on Arbitrum.
Each family member has their own wallet, isolated positions, and trade history.
Family members DM the shared Telegram bot and see only their own data.

Host class (GMXBot) must provide:
  - self.cfg: Config
  - self.w3: Web3
  - self.account: Account (admin W1)
  - self.logger: Logger
  - self.positions: Dict[str, Position]
  - self.send_message(chat_id, msg)
  - self.notify(msg)
  - self._send_tx(to, data, value, acct)
  - self._get_portfolio_value_for(acct)
"""

import asyncio
import logging
import time
import traceback
import uuid
from typing import Optional, List, Dict, Any

from web3 import Web3
from eth_account import Account

from family import FamilyMember, load_family_members
from state_io import atomic_json_write, safe_json_read
import bot_api

logger = logging.getLogger("GMXBot.family")


class FamilyMirrorMixin:
    """Mixin: family member trade mirroring, /send, and family commands."""

    def _init_family_members(self):
        """Load family members from env, init accounts, load persisted state."""
        self.family_members: List[FamilyMember] = []
        # Lock to prevent concurrent modifications to family position state
        self._family_lock = asyncio.Lock()

        raw_members = load_family_members()
        for member in raw_members:
            try:
                member.account = Account.from_key(member.private_key)
                logger.info(
                    f"Family {member.id} ({member.name}): {member.short_address}"
                )
            except Exception as e:
                logger.error(f"Family {member.id} ({member.name}): bad key — {e}")
                continue

            # Load persisted state
            self._load_family_state(member)
            self.family_members.append(member)

        if self.family_members:
            names = ", ".join(m.name for m in self.family_members)
            logger.info(f"Loaded {len(self.family_members)} family members: {names}")

    # ──────────────────────────────────────────────────────────────────────
    # Lookups
    # ──────────────────────────────────────────────────────────────────────

    def _get_family_member_by_chat_id(self, chat_id) -> Optional[FamilyMember]:
        try:
            chat_id = int(chat_id)
        except (ValueError, TypeError):
            return None
        for m in self.family_members:
            if m.chat_id == chat_id:
                return m
        return None

    def _get_family_member_by_name(self, name: str) -> Optional[FamilyMember]:
        name_lower = name.lower()
        for m in self.family_members:
            if m.name.lower() == name_lower:
                return m
        return None

    # ──────────────────────────────────────────────────────────────────────
    # State persistence
    # ──────────────────────────────────────────────────────────────────────

    def _family_positions_file(self, member: FamilyMember) -> str:
        return f"family_{member.id}_positions.json"

    def _family_trades_file(self, member: FamilyMember) -> str:
        return f"family_{member.id}_trades.json"

    def _save_family_state(self, member: FamilyMember):
        """Persist family member positions and trade history."""
        # Positions
        pos_data = {}
        for pid, pos in member.positions.items():
            pos_data[pid] = {
                "id": pos.id,
                "symbol": pos.symbol,
                "side": pos.side,
                "size_usd": pos.size_usd,
                "leverage": pos.leverage,
                "entry_price": pos.entry_price,
                "current_price": pos.current_price,
                "stop_loss": pos.stop_loss,
                "is_open": pos.is_open,
                "opened_at": pos.opened_at,
                "closed_at": pos.closed_at,
                "unrealized_pnl": pos.unrealized_pnl,
                "realized_pnl": pos.realized_pnl,
                "tx_hash": pos.tx_hash,
                "exit_reason": pos.exit_reason,
                "wallet_id": 1,
                "exchange": "gmx",
                "market_addr": pos.market_addr,
                "sl_moved_to_entry": pos.sl_moved_to_entry,
                "sl_move_label": pos.sl_move_label,
                "original_size_usd": pos.original_size_usd,
                "take_profits": [
                    {"price": tp.price, "percentage": tp.percentage}
                    for tp in pos.take_profits
                ],
                "verified_decreases": pos.verified_decreases,
            }
        atomic_json_write(self._family_positions_file(member), pos_data)

        # Trade history
        trades = []
        for t in member.trade_history:
            trades.append({
                "id": t.get("id", ""),
                "symbol": t.get("symbol", ""),
                "side": t.get("side", ""),
                "entry_price": t.get("entry_price", 0),
                "exit_price": t.get("exit_price", 0),
                "size_usd": t.get("size_usd", 0),
                "leverage": t.get("leverage", 0),
                "pnl_usd": t.get("pnl_usd", 0),
                "pnl_percentage": t.get("pnl_percentage", 0),
                "exit_reason": t.get("exit_reason", ""),
                "opened_at": t.get("opened_at", 0),
                "closed_at": t.get("closed_at", 0),
                "duration_hours": t.get("duration_hours", 0),
            })
        atomic_json_write(self._family_trades_file(member), trades)

    def _load_family_state(self, member: FamilyMember):
        """Load persisted state for a family member."""
        from gmx import Position, TakeProfitLevel

        # Positions
        pos_data = safe_json_read(self._family_positions_file(member), {})
        for pid, pd in pos_data.items():
            try:
                tps = [
                    TakeProfitLevel(price=t["price"], percentage=t["percentage"])
                    for t in pd.get("take_profits", [])
                ]
                pos = Position(
                    id=pd["id"],
                    symbol=pd["symbol"],
                    side=pd["side"],
                    size_usd=pd["size_usd"],
                    leverage=pd["leverage"],
                    entry_price=pd["entry_price"],
                    current_price=pd.get("current_price", 0.0),
                    stop_loss=pd.get("stop_loss"),
                    is_open=pd.get("is_open", True),
                    opened_at=pd.get("opened_at", 0),
                    closed_at=pd.get("closed_at"),
                    unrealized_pnl=pd.get("unrealized_pnl", 0),
                    realized_pnl=pd.get("realized_pnl", 0),
                    tx_hash=pd.get("tx_hash"),
                    exit_reason=pd.get("exit_reason"),
                    wallet_id=1,
                    exchange="gmx",
                    market_addr=pd.get("market_addr"),
                    take_profits=tps,
                    sl_moved_to_entry=pd.get("sl_moved_to_entry", False),
                    sl_move_label=pd.get("sl_move_label"),
                    original_size_usd=pd.get("original_size_usd", pd["size_usd"]),
                    verified_decreases=pd.get("verified_decreases", []),
                )
                member.positions[pid] = pos
            except Exception as e:
                logger.warning(f"Failed to load position {pid} for {member.name}: {e}")

        # Trade history
        member.trade_history = safe_json_read(self._family_trades_file(member), [])

        open_count = sum(1 for p in member.positions.values() if p.is_open)
        if open_count:
            logger.info(f"Family {member.name}: loaded {open_count} open positions")

    # ──────────────────────────────────────────────────────────────────────
    # Notifications
    # ──────────────────────────────────────────────────────────────────────

    async def _notify_family(self, member: FamilyMember, message: str):
        """Send a message to a family member's chat via Bot API."""
        try:
            await bot_api.send_admin_message(
                self.cfg.telegram_bot_token, str(member.chat_id), message
            )
        except Exception as e:
            logger.error(f"Notify family {member.name} failed: {e}")

    async def _notify_all_family(self, message: str, *, skip_pnl_alerts: bool = False):
        """Send a message to all family members."""
        for member in self.family_members:
            await self._notify_family(member, message)

    # ──────────────────────────────────────────────────────────────────────
    # Trade mirroring
    # ──────────────────────────────────────────────────────────────────────

    async def _mirror_to_family(self, signal):
        """Mirror a trade signal to all enabled family members."""
        if not self.family_members:
            return

        tasks = []
        for member in self.family_members:
            tasks.append(self._execute_family_trade(member, signal))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for member, result in zip(self.family_members, results):
            if isinstance(result, Exception):
                logger.error(f"Family trade failed for {member.name}: {result}")
                await self._notify_family(
                    member,
                    f"**Trade Failed**\n{signal.symbol} {signal.side}\nError: {result}"
                )

    async def _execute_family_trade(self, member: FamilyMember, signal):
        """Execute a trade for a single family member."""
        from open import execute_signal
        from risk import cap_leverage
        from gmx import Position, TakeProfitLevel

        # Check for duplicate under lock to prevent concurrent signals from
        # both passing the check before either records its position
        async with self._family_lock:
            for pos in member.positions.values():
                if pos.is_open and pos.symbol == signal.symbol and pos.side == signal.side:
                    logger.info(f"Family {member.name}: {signal.symbol} {signal.side} already open, skipping")
                    return

        # Get member's USDC balance
        usdc_balance = await asyncio.to_thread(
            self._get_portfolio_value_for, member.account
        )
        if usdc_balance <= 0:
            logger.warning(f"Family {member.name}: no USDC balance")
            await self._notify_family(member, f"Skipped {signal.symbol} {signal.side}: no USDC balance")
            return

        # Same risk % as main bot
        leverage = cap_leverage(signal.leverage, self.cfg.max_leverage, self.cfg.min_leverage)
        collateral_usd = usdc_balance * self.cfg.portfolio_pct
        size_usd = collateral_usd * leverage

        if collateral_usd < self.cfg.min_position_usd:
            logger.info(f"Family {member.name}: collateral ${collateral_usd:.2f} below minimum")
            return

        logger.info(
            f"Family {member.name}: opening {signal.symbol} {signal.side} "
            f"${size_usd:.0f} @ {leverage:.0f}x (collateral ${collateral_usd:.0f})"
        )

        # Create position object
        market_addr = self.cfg.markets.get(signal.symbol)
        if not market_addr:
            logger.error(f"Unknown symbol {signal.symbol} for family trade")
            return

        position = Position(
            id=str(uuid.uuid4()),
            symbol=signal.symbol,
            side=signal.side,
            size_usd=size_usd,
            leverage=leverage,
            entry_price=signal.entry_mid,
            stop_loss=signal.stop_loss,
            take_profits=[
                TakeProfitLevel(price=tp.price, percentage=tp.close_pct)
                for tp in signal.take_profits
            ],
            market_addr=market_addr,
            collateral_token=self.cfg.collateral_token or None,
            opened_at=time.time(),
            wallet_id=1,
            original_size_usd=size_usd,
            exchange="gmx",
        )

        # Execute on-chain
        try:
            results = await asyncio.to_thread(
                execute_signal,
                w3=self.w3,
                acct=member.account,
                signal=signal,
                exchange_router=self.cfg.exchange_router,
                order_vault=self.cfg.order_vault,
                market=market_addr,
                collateral_token=self.cfg.collateral_token,
                size_usd=size_usd,
                collateral_usd=collateral_usd,
                slippage_bps=self.cfg.slippage_bps,
                execution_fee=self.cfg.execution_fee_wei,
                dry_run=self.cfg.dry_run,
            )

            position.tx_hash = results.get("tx_hash", "")
            position.tp_tx_hashes = results.get("tp_tx_hashes", [])
            position.sl_tx_hash = results.get("sl_tx_hash", "")

            # Store position (locked to prevent concurrent state corruption)
            async with self._family_lock:
                member.positions[position.id] = position
                self._save_family_state(member)

            # Notify family member
            tp_lines = ""
            for i, tp in enumerate(position.take_profits):
                tp_lines += f"  TP{i+1}: ${tp.price:,.2f} ({tp.percentage:.0%})\n"
            sl_str = f"${position.stop_loss:,.2f}" if position.stop_loss else "None"

            await self._notify_family(
                member,
                f"**Position Opened**\n\n"
                f"{position.symbol} {position.side}\n"
                f"Size: ${position.size_usd:,.2f} @ {position.leverage:.0f}x\n"
                f"Entry: ${position.entry_price:,.2f}\n"
                f"SL: {sl_str}\n"
                f"{tp_lines}"
                f"TX: {position.tx_hash}"
            )

            logger.info(f"Family {member.name}: opened {signal.symbol} {signal.side}")

        except Exception as e:
            logger.error(f"Family {member.name}: trade execution failed: {e}")
            raise

    # ──────────────────────────────────────────────────────────────────────
    # Mirror close
    # ──────────────────────────────────────────────────────────────────────

    async def _mirror_close_to_family(self, symbol: str, side: str):
        """Close matching positions for all family members when admin closes."""
        if not self.family_members:
            return

        from close import create_close_order, fetch_positions as chain_fetch_positions

        tasks = []
        for member in self.family_members:
            matching = [
                p for p in member.positions.values()
                if p.is_open and p.symbol == symbol and p.side == side
            ]
            if matching:
                tasks.append(self._close_family_member_positions(
                    member, matching, symbol, side, create_close_order, chain_fetch_positions
                ))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _close_family_member_positions(
        self, member, matching, symbol, side, create_close_order, chain_fetch_positions
    ):
        """Close matching positions for a single family member."""
        for pos in matching:
            try:
                # Find on-chain position
                chain_positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, member.account.address
                )
                chain_pos = None
                for cp in chain_positions:
                    cp_side = "LONG" if cp.is_long else "SHORT"
                    if cp.symbol.upper().split("/")[0] == symbol and cp_side == side:
                        chain_pos = cp
                        break

                if chain_pos:
                    await asyncio.to_thread(
                        create_close_order,
                        self.w3, member.account,
                        chain_pos,
                        1.0,                # percentage (100% close)
                        self.cfg.dry_run,
                    )

                async with self._family_lock:
                    if not pos.is_open:
                        continue  # Already closed by monitor or another close
                    # Update PnL from chain position before recording
                    if chain_pos:
                        pos.current_price = chain_pos.current_price
                        pos.unrealized_pnl = chain_pos.unrealized_pnl
                    pos.is_open = False
                    pos.closed_at = time.time()
                    pos.exit_reason = "admin_close"
                    self._record_family_trade(member, pos)
                    self._save_family_state(member)

                pnl = pos.unrealized_pnl
                pnl_sign = "+" if pnl >= 0 else ""
                await self._notify_family(
                    member,
                    f"**Position Closed**\n"
                    f"{pos.symbol} {pos.side}\n"
                    f"PnL: {pnl_sign}${pnl:,.2f}\n"
                    f"Reason: closed by Sheldon"
                )
                logger.info(f"Family {member.name}: closed {symbol} {side}")

            except Exception as e:
                logger.error(f"Family {member.name}: close failed for {symbol}: {e}")
                await self._notify_family(
                    member,
                    f"**Close Failed** {symbol} {side}\nError: {e}"
                )

    def _record_family_trade(self, member: FamilyMember, pos):
        """Record a closed trade for a family member."""
        trade = {
            "id": pos.id,
            "symbol": pos.symbol,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_price": pos.current_price or pos.entry_price,
            "size_usd": pos.size_usd,
            "leverage": pos.leverage,
            "pnl_usd": pos.unrealized_pnl,
            "pnl_percentage": pos.pnl_percentage,
            "exit_reason": pos.exit_reason or "unknown",
            "opened_at": pos.opened_at,
            "closed_at": pos.closed_at or time.time(),
            "duration_hours": pos.duration_hours,
        }
        member.trade_history.append(trade)

    # ──────────────────────────────────────────────────────────────────────
    # Family monitor loop (TP/SL tracking)
    # ──────────────────────────────────────────────────────────────────────

    async def family_monitor_loop(self):
        """Background loop: sync family positions with on-chain state."""
        from close import fetch_positions as chain_fetch_positions

        # Require 2 consecutive misses before marking closed (guards against RPC errors)
        family_missing_count: Dict[str, int] = {}

        while True:
            try:
                await asyncio.sleep(30)  # Check every 30s

                for member in self.family_members:
                    open_positions = [
                        p for p in member.positions.values() if p.is_open
                    ]
                    if not open_positions:
                        continue

                    try:
                        chain_positions = await asyncio.to_thread(
                            chain_fetch_positions, self.w3, member.account.address
                        )
                        chain_markets = set()
                        for cp in chain_positions:
                            side = "LONG" if cp.is_long else "SHORT"
                            chain_markets.add((cp.market.lower(), side))

                            # Update PnL
                            sym = cp.symbol.upper().split("/")[0]
                            for pos in open_positions:
                                if pos.symbol == sym and pos.side == side:
                                    pos.current_price = cp.current_price
                                    pos.unrealized_pnl = cp.unrealized_pnl

                        # Detect closed positions (gone from chain)
                        for pos in open_positions:
                            market = (pos.market_addr or "").lower()
                            if market and (market, pos.side) not in chain_markets:
                                # Require 2 consecutive misses to avoid RPC false positives
                                count = family_missing_count.get(pos.id, 0) + 1
                                family_missing_count[pos.id] = count
                                logger.debug(f"Family {member.name}: {pos.symbol} {pos.side} missing ({count}/2)")
                                if count < 2:
                                    continue

                                family_missing_count.pop(pos.id, None)
                                async with self._family_lock:
                                    if not pos.is_open:
                                        continue
                                    pos.is_open = False
                                    pos.closed_at = time.time()
                                    pos.exit_reason = pos.exit_reason or "tp_or_sl"
                                    self._record_family_trade(member, pos)
                                    self._save_family_state(member)

                                pnl = pos.unrealized_pnl
                                pnl_sign = "+" if pnl >= 0 else ""
                                await self._notify_family(
                                    member,
                                    f"**Position Closed**\n"
                                    f"{pos.symbol} {pos.side}\n"
                                    f"PnL: {pnl_sign}${pnl:,.2f}\n"
                                    f"Duration: {pos.duration_hours:.1f}h"
                                )
                            else:
                                # Position found on chain — reset missing counter
                                family_missing_count.pop(pos.id, None)

                    except Exception as e:
                        logger.debug(f"Family {member.name} monitor error: {e}")

            except asyncio.CancelledError:
                logger.info("Family monitor loop cancelled")
                return
            except Exception as e:
                logger.error(f"Family monitor loop error: {e}")
                await asyncio.sleep(30)

    # ──────────────────────────────────────────────────────────────────────
    # /send — admin sends USDC to family member
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_send(self, chat_id: int, args: list):
        """Send USDC from admin W1 to a family member's wallet.

        Usage: /send <name> <amount>
        """
        if len(args) < 3:
            await self.send_message(
                chat_id,
                "Usage: /send <name> <amount>\n\n"
                "Example: /send Mom 100"
            )
            return

        name = args[1]
        try:
            amount = float(args[2])
        except ValueError:
            await self.send_message(chat_id, "Invalid amount. Usage: /send Mom 100")
            return

        if amount <= 0:
            await self.send_message(chat_id, "Amount must be greater than 0.")
            return

        member = self._get_family_member_by_name(name)
        if not member:
            names = ", ".join(m.name for m in self.family_members)
            await self.send_message(
                chat_id,
                f"Family member '{name}' not found.\n"
                f"Available: {names or 'none configured'}"
            )
            return

        destination = member.account.address

        # Check W1 balance
        try:
            from open import ERC20_ABI, wait_receipt as open_wait_receipt

            w1_balance = await asyncio.to_thread(
                self._get_portfolio_value_for, self.account
            )
            if w1_balance < amount:
                await self.send_message(
                    chat_id,
                    f"Insufficient W1 balance: ${w1_balance:,.2f} (need ${amount:,.2f})"
                )
                return

            await self.send_message(
                chat_id,
                f"Sending ${amount:,.2f} USDC to {member.name} ({member.short_address})..."
            )

            # Build USDC transfer
            usdc_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.cfg.collateral_token),
                abi=ERC20_ABI,
            )
            decimals = await asyncio.to_thread(
                lambda: usdc_contract.functions.decimals().call()
            )
            raw_amount = round(amount * (10 ** decimals))

            transfer_data = usdc_contract.encode_abi(
                "transfer",
                [Web3.to_checksum_address(destination), raw_amount],
            )

            if self.cfg.dry_run:
                await self.send_message(
                    chat_id,
                    f"[DRY RUN] Would send ${amount:,.2f} USDC to {member.name} ({member.short_address})"
                )
                await self._notify_family(
                    member,
                    f"[DRY RUN] Sheldon sent you ${amount:,.2f} USDC"
                )
                return

            tx_hash = await asyncio.to_thread(
                self._send_tx,
                self.cfg.collateral_token,
                transfer_data,
                0,
                self.account,
            )

            receipt = await asyncio.to_thread(open_wait_receipt, self.w3, tx_hash)
            if receipt.get("status") != 1:
                await self.send_message(
                    chat_id,
                    f"**Transfer failed** (tx reverted).\n"
                    f"TX: https://arbiscan.io/tx/{tx_hash}"
                )
                return

            # Notify admin
            await self.send_message(
                chat_id,
                f"**Sent ${amount:,.2f} USDC to {member.name}**\n"
                f"To: `{member.short_address}`\n"
                f"TX: https://arbiscan.io/tx/{tx_hash}"
            )

            # Notify family member
            await self._notify_family(
                member,
                f"Sheldon sent you ${amount:,.2f} USDC\n"
                f"TX: https://arbiscan.io/tx/{tx_hash}"
            )

        except Exception as e:
            logger.error(f"Send to {member.name} failed: {e}\n{traceback.format_exc()}")
            await self.send_message(chat_id, f"**Send Failed**: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # Family command handlers (scoped to member's own data)
    # ──────────────────────────────────────────────────────────────────────

    async def process_family_command(self, text: str, chat_id: int, member: FamilyMember):
        """Dispatch commands for a family member."""
        try:
            parts = text.strip().split()
            cmd = parts[0].lower()

            # Admin-only commands
            if cmd == "/send":
                await self.send_message(chat_id, "This command is only available for the admin.")
                return

            if cmd in ("/help", "/start"):
                await self.send_message(chat_id, self._family_help_text(member))
            elif cmd == "/positions":
                await self._cmd_family_positions(chat_id, member)
            elif cmd == "/balance":
                await self._cmd_family_balance(chat_id, member)
            elif cmd == "/status":
                await self._cmd_family_status(chat_id, member)
            elif cmd == "/close":
                arg = parts[1] if len(parts) > 1 else None
                await self._cmd_family_close(chat_id, member, arg)
            elif cmd == "/trades":
                await self._cmd_family_trades(chat_id, member)
            elif cmd == "/pnl":
                await self._cmd_family_pnl(chat_id, member)
            elif cmd == "/withdraw":
                await self._cmd_family_withdraw(chat_id, member, parts)
            elif cmd == "/wallet":
                await self._cmd_family_wallet(chat_id, member)
            else:
                await self.send_message(chat_id, "Unknown command. Type /help")

        except Exception as e:
            logger.error(f"Family command error ({member.name}): {e}")
            await self.send_message(chat_id, f"Error: {e}")

    def _family_help_text(self, member: FamilyMember) -> str:
        return (
            f"**Hi {member.name}!**\n\n"
            f"Your trades are automatically mirrored from Sheldon's signals.\n\n"
            f"**Commands:**\n"
            f"/positions — view your open positions\n"
            f"/balance — check your wallet balance\n"
            f"/status — bot status\n"
            f"/close <symbol> — close a position\n"
            f"/trades — trade history\n"
            f"/pnl — profit & loss summary\n"
            f"/withdraw <amount> <address> — withdraw USDC\n"
            f"/wallet — your wallet address\n"
        )

    async def _cmd_family_positions(self, chat_id: int, member: FamilyMember):
        """Show family member's open positions."""
        open_positions = [p for p in member.positions.values() if p.is_open]
        if not open_positions:
            await self.send_message(chat_id, f"No open positions, {member.name}.")
            return

        msg = f"**Your Positions ({len(open_positions)})**\n\n"
        for i, pos in enumerate(open_positions, 1):
            pnl = pos.unrealized_pnl
            pnl_sign = "+" if pnl >= 0 else ""
            pnl_pct = pos.pnl_percentage
            pct_sign = "+" if pnl_pct >= 0 else ""
            msg += (
                f"**{i}. {pos.symbol} {pos.side}**\n"
                f"  Size: ${pos.size_usd:,.2f} @ {pos.leverage:.0f}x\n"
                f"  Entry: ${pos.entry_price:,.2f}\n"
                f"  PnL: {pnl_sign}${pnl:,.2f} ({pct_sign}{pnl_pct:.1f}%)\n"
            )
            if pos.stop_loss:
                msg += f"  SL: ${pos.stop_loss:,.2f}"
                if pos.sl_move_label:
                    msg += f" ({pos.sl_move_label})"
                msg += "\n"
            msg += f"  Duration: {pos.duration_hours:.1f}h\n\n"

        await self.send_message(chat_id, msg)

    async def _cmd_family_balance(self, chat_id: int, member: FamilyMember):
        """Show family member's wallet balance."""
        try:
            usdc = await asyncio.to_thread(
                self._get_portfolio_value_for, member.account
            )
            # Count deployed collateral
            deployed = sum(
                p.collateral_usd for p in member.positions.values() if p.is_open
            )
            total = usdc + deployed

            msg = (
                f"**{member.name}'s Balance**\n\n"
                f"Free USDC: ${usdc:,.2f}\n"
                f"Deployed: ${deployed:,.2f}\n"
                f"Total: ${total:,.2f}\n"
                f"Wallet: `{member.short_address}`"
            )
            await self.send_message(chat_id, msg)
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching balance: {e}")

    async def _cmd_family_status(self, chat_id: int, member: FamilyMember):
        """Show bot status for family member."""
        open_count = sum(1 for p in member.positions.values() if p.is_open)
        total_trades = len(member.trade_history)
        wins = sum(1 for t in member.trade_history if t.get("pnl_usd", 0) > 0)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        msg = (
            f"**Status — {member.name}**\n\n"
            f"Open positions: {open_count}\n"
            f"Total trades: {total_trades}\n"
            f"Win rate: {win_rate:.0f}%\n"
            f"Exchange: GMX (Arbitrum)\n"
            f"Mode: {'DRY RUN' if self.cfg.dry_run else 'LIVE'}"
        )
        await self.send_message(chat_id, msg)

    async def _cmd_family_close(self, chat_id: int, member: FamilyMember, arg: Optional[str]):
        """Close a family member's position."""
        from close import create_close_order, fetch_positions as chain_fetch_positions

        open_positions = [p for p in member.positions.values() if p.is_open]
        if not open_positions:
            await self.send_message(chat_id, "No open positions to close.")
            return

        if arg is None:
            msg = "**Your Open Positions:**\n"
            for i, pos in enumerate(open_positions, 1):
                pnl = pos.unrealized_pnl
                pnl_sign = "+" if pnl >= 0 else ""
                msg += f"  {i}. {pos.symbol} {pos.side} — {pnl_sign}${pnl:,.2f}\n"
            msg += "\nReply with /close <symbol> (e.g. /close BTC)"
            await self.send_message(chat_id, msg)
            return

        symbol = arg.upper()
        matching = [p for p in open_positions if p.symbol == symbol]
        if not matching:
            await self.send_message(chat_id, f"No open position for {symbol}.")
            return

        for pos in matching:
            try:
                # Find on-chain position
                chain_positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, member.account.address
                )
                chain_pos = None
                for cp in chain_positions:
                    cp_side = "LONG" if cp.is_long else "SHORT"
                    if cp.symbol.upper().split("/")[0] == symbol and cp_side == pos.side:
                        chain_pos = cp
                        break

                if chain_pos:
                    await asyncio.to_thread(
                        create_close_order,
                        self.w3, member.account,
                        chain_pos,
                        1.0,                # percentage (100% close)
                        self.cfg.dry_run,
                    )

                async with self._family_lock:
                    if not pos.is_open:
                        continue
                    # Update PnL from chain position before recording
                    if chain_pos:
                        pos.current_price = chain_pos.current_price
                        pos.unrealized_pnl = chain_pos.unrealized_pnl
                    pos.is_open = False
                    pos.closed_at = time.time()
                    pos.exit_reason = "manual_close"
                    self._record_family_trade(member, pos)
                    self._save_family_state(member)

                pnl = pos.unrealized_pnl
                pnl_sign = "+" if pnl >= 0 else ""
                await self.send_message(
                    chat_id,
                    f"**Closed {pos.symbol} {pos.side}**\n"
                    f"PnL: {pnl_sign}${pnl:,.2f}"
                )

            except Exception as e:
                logger.error(f"Family {member.name}: close {symbol} failed: {e}")
                await self.send_message(chat_id, f"Close failed for {symbol}: {e}")

    async def _cmd_family_trades(self, chat_id: int, member: FamilyMember):
        """Show trade history for a family member."""
        if not member.trade_history:
            await self.send_message(chat_id, f"No trade history yet, {member.name}.")
            return

        # Show last 10 trades
        recent = member.trade_history[-10:]
        total_pnl = sum(t.get("pnl_usd", 0) for t in member.trade_history)
        wins = sum(1 for t in member.trade_history if t.get("pnl_usd", 0) > 0)
        total = len(member.trade_history)

        msg = f"**Trade History — {member.name}**\n"
        win_pct = (wins / total * 100) if total > 0 else 0
        msg += f"Win rate: {wins}/{total} ({win_pct:.0f}%)\n"
        pnl_sign = "+" if total_pnl >= 0 else ""
        msg += f"Total PnL: {pnl_sign}${total_pnl:,.2f}\n\n"

        msg += "**Recent Trades:**\n"
        for t in reversed(recent):
            pnl = t.get("pnl_usd", 0)
            p_sign = "+" if pnl >= 0 else ""
            symbol = t.get("symbol", "?")
            side = t.get("side", "?")
            reason = t.get("exit_reason", "?")
            msg += f"  {symbol} {side}: {p_sign}${pnl:,.2f} ({reason})\n"

        await self.send_message(chat_id, msg)

    async def _cmd_family_withdraw(self, chat_id: int, member: FamilyMember, parts: list):
        """Handle /withdraw for family members — simple USDC transfer from their wallet."""
        from open import ERC20_ABI, wait_receipt as open_wait_receipt

        if len(parts) < 3:
            await self.send_message(
                chat_id,
                "Usage: /withdraw <amount> <address>\n\n"
                "Example: /withdraw 100 0xYourAddress"
            )
            return

        try:
            amount = float(parts[1].replace("$", "").replace(",", ""))
        except ValueError:
            await self.send_message(chat_id, "Invalid amount.")
            return

        if amount <= 0:
            await self.send_message(chat_id, "Amount must be greater than 0.")
            return

        dest = parts[2].strip()
        if not Web3.is_address(dest):
            await self.send_message(chat_id, "Invalid Arbitrum address.")
            return
        dest = Web3.to_checksum_address(dest)

        # Check balance
        try:
            balance = await asyncio.to_thread(
                self._get_portfolio_value_for, member.account
            )
            if balance < amount:
                await self.send_message(
                    chat_id,
                    f"Insufficient balance: ${balance:,.2f} (need ${amount:,.2f})"
                )
                return
        except Exception as e:
            await self.send_message(chat_id, f"Error checking balance: {e}")
            return

        short_dest = f"{dest[:8]}...{dest[-6:]}"
        await self.send_message(
            chat_id,
            f"Sending ${amount:,.2f} USDC to {short_dest}..."
        )

        try:
            usdc_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.cfg.collateral_token),
                abi=ERC20_ABI,
            )
            decimals = await asyncio.to_thread(
                lambda: usdc_contract.functions.decimals().call()
            )
            raw_amount = round(amount * (10 ** decimals))

            transfer_data = usdc_contract.encode_abi(
                "transfer",
                [dest, raw_amount],
            )

            if self.cfg.dry_run:
                await self.send_message(
                    chat_id,
                    f"[DRY RUN] Would send ${amount:,.2f} USDC to {short_dest}"
                )
                return

            tx_hash = await asyncio.to_thread(
                self._send_tx,
                self.cfg.collateral_token,
                transfer_data,
                0,
                member.account,
            )

            receipt = await asyncio.to_thread(open_wait_receipt, self.w3, tx_hash)
            if receipt.get("status") != 1:
                await self.send_message(
                    chat_id,
                    f"**Transfer failed** (tx reverted).\n"
                    f"TX: https://arbiscan.io/tx/{tx_hash}"
                )
                return

            await self.send_message(
                chat_id,
                f"**Withdrawal Complete**\n\n"
                f"Sent: ${amount:,.2f} USDC\n"
                f"To: `{short_dest}`\n"
                f"TX: https://arbiscan.io/tx/{tx_hash}"
            )

        except Exception as e:
            logger.error(f"Family {member.name} withdraw failed: {e}")
            await self.send_message(chat_id, f"**Withdrawal Failed**: {e}")

    async def _cmd_family_wallet(self, chat_id: int, member: FamilyMember):
        """Show family member's wallet address."""
        addr = member.account.address if member.account else "?"
        try:
            balance = await asyncio.to_thread(
                self._get_portfolio_value_for, member.account
            )
            await self.send_message(
                chat_id,
                f"**{member.name}'s Wallet**\n\n"
                f"Balance: ${balance:,.2f} USDC\n"
                f"Address (Arbitrum):"
            )
            # Send address separately for easy copy
            await self.send_message(chat_id, addr)
        except Exception as e:
            await self.send_message(chat_id, f"Wallet: {addr}\nError fetching balance: {e}")

    async def _cmd_family_pnl(self, chat_id: int, member: FamilyMember):
        """Show PnL summary for family member."""
        if not member.trade_history and not any(p.is_open for p in member.positions.values()):
            await self.send_message(chat_id, "No trades or positions yet.")
            return

        realized = sum(t.get("pnl_usd", 0) for t in member.trade_history)
        unrealized = sum(p.unrealized_pnl for p in member.positions.values() if p.is_open)
        total = realized + unrealized

        r_sign = "+" if realized >= 0 else ""
        u_sign = "+" if unrealized >= 0 else ""
        t_sign = "+" if total >= 0 else ""

        msg = (
            f"**PnL — {member.name}**\n\n"
            f"Realized: {r_sign}${realized:,.2f}\n"
            f"Unrealized: {u_sign}${unrealized:,.2f}\n"
            f"Total: {t_sign}${total:,.2f}"
        )
        await self.send_message(chat_id, msg)
