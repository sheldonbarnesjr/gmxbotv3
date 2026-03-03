"""
Withdraw & Deposit Mixin for GMX V2 Trading Bot.

Provides:
  /withdraw <amount> — Consolidate USDC if needed -> ask for address -> send USDC
  /deposit           — Show W1 deposit address, auto-rebalance when USDC arrives

Host class (GMXBot) must provide:
  - self.cfg: Config with collateral_token, dry_run
  - self.w3: Web3 instance
  - self.account: Account (W1)
  - self._all_wallets(), _consolidate_to_wallet(), _get_portfolio_value_for()
  - self._send_tx(): transaction sender
  - self._rebalance_wallets(): equalise W1-W4
  - self.send_message(): Telegram message sender
  - self.logger: logging.Logger
"""

import asyncio
import logging
import time
import traceback
from typing import Optional, Dict, Any

from web3 import Web3

from open import ERC20_ABI, wait_receipt as open_wait_receipt

logger = logging.getLogger("GMXBot.withdraw")


class WithdrawMixin:
    """Mixin: direct USDC withdrawal (/withdraw) and deposit (/deposit)."""

    _deposit_watch_task: Optional[asyncio.Task] = None

    # ──────────────────────────────────────────────────────────────────────
    # /withdraw — direct USDC transfer to any Arbitrum address
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_withdraw(self, chat_id: int, arg: Optional[str] = None):
        """Start a withdrawal: consolidate if needed -> ask for address -> send.

        Usage: /withdraw <amount>
        """
        if not arg:
            await self.send_message(
                chat_id,
                "Usage: /withdraw <amount>\n\n"
                "Example: /withdraw 100\n\n"
                "Consolidates USDC if needed, asks for your "
                "Arbitrum USDC address, then sends directly."
            )
            return

        # Check for in-progress withdrawal
        if chat_id in self.pending_withdraw:
            state = self.pending_withdraw[chat_id].get("state", "")
            if state in ("sending",):
                await self.send_message(
                    chat_id, "A withdrawal is already in progress."
                )
                return

        # Parse amount
        try:
            amount = float(arg.strip().replace("$", "").replace(",", ""))
        except ValueError:
            await self.send_message(
                chat_id,
                "Invalid amount. Usage: /withdraw <amount>"
            )
            return

        if amount <= 0:
            await self.send_message(chat_id, "Amount must be greater than 0.")
            return

        if amount < 1:
            await self.send_message(chat_id, "Minimum withdrawal is $1.")
            return

        # Check combined wallet balance
        try:
            combined = 0.0
            for _, acct in self._all_wallets():
                combined += await asyncio.to_thread(
                    self._get_portfolio_value_for, acct
                )
        except Exception as e:
            await self.send_message(chat_id, f"Failed to check balances: {e}")
            return

        if combined < amount:
            await self.send_message(
                chat_id,
                f"Insufficient balance.\n"
                f"Combined USDC: ${combined:,.2f}\n"
                f"Requested: ${amount:,.2f}"
            )
            return

        self.pending_withdraw[chat_id] = {
            "amount": amount,
            "state": "awaiting_address",
            "created_at": time.time(),
        }

        await self.send_message(
            chat_id,
            f"**Withdraw ${amount:,.2f} USDC**\n\n"
            f"Paste the destination Arbitrum USDC address:"
        )

    async def handle_withdraw_reply(self, chat_id: int, text: str):
        """State machine for /withdraw interactive stages."""
        if chat_id not in self.pending_withdraw:
            return

        pending = self.pending_withdraw[chat_id]
        text = text.strip()
        state = pending["state"]

        # 5-minute expiry
        if time.time() - pending["created_at"] > 300:
            del self.pending_withdraw[chat_id]
            await self.send_message(
                chat_id, "Withdrawal expired (5 min). Use /withdraw again."
            )
            return

        # ── awaiting_address: user pastes an address ──
        if state == "awaiting_address":
            upper = text.upper()
            if upper in ("CANCEL", "NO", "N"):
                del self.pending_withdraw[chat_id]
                await self.send_message(chat_id, "Withdrawal cancelled.")
                return

            # Validate Ethereum address
            addr = text.strip()
            if not Web3.is_address(addr):
                await self.send_message(
                    chat_id,
                    "Invalid address. Paste a valid Arbitrum address, "
                    "or /cancel to abort."
                )
                return

            pending["destination"] = Web3.to_checksum_address(addr)
            pending["state"] = "confirm"
            amount = pending["amount"]
            short_addr = f"{addr[:8]}...{addr[-6:]}"

            await self.send_message(
                chat_id,
                f"**Confirm Withdrawal**\n\n"
                f"Amount: **${amount:,.2f} USDC**\n"
                f"To: `{short_addr}`\n"
                f"Network: Arbitrum\n\n"
                f"Send /confirm to proceed or /cancel to abort."
            )
            return

        # ── confirm: user confirms ──
        if state == "confirm":
            upper = text.upper()
            if upper in ("YES", "Y", "CONFIRM", "/CONFIRM"):
                await self._execute_withdraw(chat_id)
            elif upper in ("NO", "N", "CANCEL", "/CANCEL"):
                del self.pending_withdraw[chat_id]
                await self.send_message(chat_id, "Withdrawal cancelled.")
            else:
                await self.send_message(
                    chat_id, "Send /confirm to proceed or /cancel to abort."
                )

    # ──────────────────────────────────────────────────────────────────────
    # Withdrawal execution
    # ──────────────────────────────────────────────────────────────────────

    async def _execute_withdraw(self, chat_id: int):
        """Consolidate if needed, then send USDC to the destination address."""
        pending = self.pending_withdraw.get(chat_id)
        if not pending:
            await self.send_message(chat_id, "No pending withdrawal found.")
            return
        amount = pending.get("amount")
        destination = pending.get("destination")
        if not amount or not destination:
            self.pending_withdraw.pop(chat_id, None)
            await self.send_message(chat_id, "Withdrawal data incomplete. Please start over with /withdraw.")
            return

        try:
            # ── Consolidate if W1 doesn't have enough ──
            w1_balance = await asyncio.to_thread(
                self._get_portfolio_value_for, self.account
            )
            if w1_balance < amount:
                await self.send_message(
                    chat_id, "Consolidating USDC to W1..."
                )
                results = await self._consolidate_to_wallet(1)
                if results:
                    await self.send_message(chat_id, "\n".join(results))

                w1_balance = await asyncio.to_thread(
                    self._get_portfolio_value_for, self.account
                )
                if w1_balance < amount:
                    del self.pending_withdraw[chat_id]
                    await self.send_message(
                        chat_id,
                        f"W1 balance (${w1_balance:,.2f}) still less than "
                        f"${amount:,.2f} after consolidation. Withdrawal aborted."
                    )
                    return

            # ── Send USDC ──
            pending["state"] = "sending"
            await self.send_message(
                chat_id, f"Sending ${amount:,.2f} USDC..."
            )

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

            # Dry-run check
            if self.cfg.dry_run:
                del self.pending_withdraw[chat_id]
                short = f"{destination[:8]}...{destination[-6:]}"
                await self.send_message(
                    chat_id,
                    f"[DRY RUN] Would send ${amount:,.2f} USDC to {short}"
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
                del self.pending_withdraw[chat_id]
                await self.send_message(
                    chat_id,
                    f"**Transfer failed** (tx reverted).\n"
                    f"TX: https://arbiscan.io/tx/{tx_hash}"
                )
                return

            del self.pending_withdraw[chat_id]

            short = f"{destination[:8]}...{destination[-6:]}"
            await self.send_message(
                chat_id,
                f"**Withdrawal Complete**\n\n"
                f"Sent: ${amount:,.2f} USDC\n"
                f"To: `{short}`\n"
                f"TX: https://arbiscan.io/tx/{tx_hash}\n\n"
                f"Rebalancing remaining wallets..."
            )

            # ── Auto-rebalance ──
            try:
                await self._rebalance_wallets()
                lines = []
                for wid, acct in self._all_wallets():
                    bal = await asyncio.to_thread(
                        self._get_portfolio_value_for, acct
                    )
                    lines.append(f"  W{wid}: ${bal:,.2f}")
                await self.send_message(
                    chat_id,
                    f"**Rebalance Complete**\n\n" + "\n".join(lines)
                )
            except Exception as e:
                await self.send_message(
                    chat_id,
                    f"Withdrawal sent but rebalance failed: {e}\n"
                    f"Run /balance-wallets manually."
                )

        except Exception as e:
            self.logger.error(
                f"Withdraw failed: {e}\n{traceback.format_exc()}"
            )
            if chat_id in self.pending_withdraw:
                del self.pending_withdraw[chat_id]
            await self.send_message(chat_id, f"**Withdrawal Failed**: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # /deposit — show W1 address + auto-rebalance watcher
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_deposit(self, chat_id: int):
        """Show W1 Arbitrum address and start watching for incoming USDC."""
        w1_addr = self.account.address

        # Get current W1 USDC balance as baseline
        try:
            baseline = await asyncio.to_thread(
                self._get_portfolio_value_for, self.account
            )
        except Exception:
            baseline = 0.0

        await self.send_message(
            chat_id,
            f"**Deposit USDC to Bot**\n\n"
            f"Send USDC (Arbitrum) to:\n"
            f"`{w1_addr}`\n\n"
            f"Current W1 balance: ${baseline:,.2f}\n\n"
            f"Watching for incoming USDC for 30 min.\n"
            f"Will auto-rebalance across all wallets when received."
        )

        # Cancel any existing watcher
        if self._deposit_watch_task and not self._deposit_watch_task.done():
            self._deposit_watch_task.cancel()

        # Start background watcher
        self._deposit_watch_task = asyncio.create_task(
            self._deposit_watch_loop(chat_id, baseline)
        )

    async def _deposit_watch_loop(self, chat_id: int, baseline: float):
        """Poll W1 USDC balance; notify + rebalance when new funds arrive."""
        max_watch = 1800  # 30 minutes
        poll_interval = 30
        start = time.time()

        try:
            while time.time() - start < max_watch:
                await asyncio.sleep(poll_interval)
                try:
                    current = await asyncio.to_thread(
                        self._get_portfolio_value_for, self.account
                    )
                except Exception:
                    continue

                increase = current - baseline
                if increase >= 1.0:
                    await self.send_message(
                        chat_id,
                        f"**Deposit Received**\n\n"
                        f"W1 received ~${increase:,.2f} USDC\n"
                        f"New W1 balance: ${current:,.2f}\n\n"
                        f"Rebalancing wallets..."
                    )
                    try:
                        await self._rebalance_wallets()
                        # Show new balances
                        lines = []
                        for wid, acct in self._all_wallets():
                            bal = await asyncio.to_thread(
                                self._get_portfolio_value_for, acct
                            )
                            lines.append(f"  W{wid}: ${bal:,.2f}")
                        await self.send_message(
                            chat_id,
                            f"**Rebalance Complete**\n\n" + "\n".join(lines)
                        )
                    except Exception as e:
                        await self.send_message(
                            chat_id,
                            f"Deposit received but rebalance failed: {e}\n"
                            f"Run /balance-wallets manually."
                        )
                    return

            # Timeout — quietly stop
            self.logger.debug("Deposit watch loop timed out (30 min)")

        except asyncio.CancelledError:
            self.logger.debug("Deposit watch loop cancelled")
