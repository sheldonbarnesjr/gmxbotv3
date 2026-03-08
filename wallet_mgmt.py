"""
Wallet Management Mixin for GMX V2 Trading Bot

Extracted methods from gmx.py and telegram.py for wallet operations:
- Portfolio value tracking (single and multi-wallet)
- Wallet selection for trade routing
- Wallet rebalancing logic
- ETH top-up and gas management
- Balance / gas monitoring Telegram commands
"""

import asyncio
import logging
import traceback
import time
import uuid
from typing import Optional, List, Tuple, Dict
from web3 import Web3
from eth_account import Account

import json
import os

from close import fetch_positions as chain_fetch_positions, fetch_current_price as close_fetch_current_price
from open import (
    ERC20_ABI,
    fetch_current_price,
    EXCHANGE_ROUTER_ABI,
    wait_receipt as open_wait_receipt,
    fetch_open_orders,
)
from state_io import atomic_json_write

BALANCE_SNAPSHOTS_FILE = "balance_snapshots.json"
MAX_SNAPSHOT_AGE_HOURS = 48


class WalletMixin:
    """Mixin class for wallet management functionality.

    Provides methods for:
    - Portfolio value calculation (free USDC, deployed collateral, PnL)
    - Multi-wallet balance queries and rebalancing
    - Wallet selection for trade routing (swing vs scalp)
    - ETH gas management and top-up
    - Telegram command handlers for balance, gas, and wallet operations
    """

    def _get_account(self, wallet_id: int) -> Account:
        """Get the Account object for a given wallet_id (1-4).

        Falls back to W1 if the requested wallet is not configured (with warning).
        """
        if wallet_id == 4 and self.account4:
            return self.account4
        if wallet_id == 3 and self.account3:
            return self.account3
        if wallet_id == 2 and self.account2:
            return self.account2
        if wallet_id not in (1, 2, 3, 4):
            self.logger.error(f"Invalid wallet_id={wallet_id} — falling back to W1")
        elif wallet_id != 1:
            self.logger.warning(f"Wallet W{wallet_id} not configured — falling back to W1")
        return self.account

    def _all_wallets(self) -> List[tuple]:
        """Return list of (wallet_id, account) for all configured wallets."""
        wallets = [(1, self.account)]
        if self.account2 and hasattr(self.account2, 'address'):
            wallets.append((2, self.account2))
        if self.account3 and hasattr(self.account3, 'address'):
            wallets.append((3, self.account3))
        if self.account4 and hasattr(self.account4, 'address'):
            wallets.append((4, self.account4))
        return wallets

    def _scalp_wallets(self) -> List[tuple]:
        """Return list of (wallet_id, account) for scalp wallets (W2-W4)."""
        wallets = []
        if self.account2:
            wallets.append((2, self.account2))
        if self.account3:
            wallets.append((3, self.account3))
        if self.account4:
            wallets.append((4, self.account4))
        return wallets

    async def _pick_wallet(self, symbol: str, trade_type: str = "scalp", *, is_long: bool = True) -> tuple:
        """Pick the first available wallet for a new position.

        All wallets are equal — W1 is tried first (priority), then W2, W3, W4.
        A wallet is only "busy" if it has the same symbol AND same side open.

        Queries ON-CHAIN positions (not just internal tracking) so the bot
        is aware of positions opened manually or before a reboot.
        Returns (wallet_id, account) or (None, None) if all wallets busy."""
        market_addr = self.cfg.markets.get(symbol, "").lower()
        side_label = "LONG" if is_long else "SHORT"
        if not market_addr:
            return 1, self.account  # unknown symbol, default to W1

        # All wallets treated equally, W1 has priority
        wallets = self._all_wallets()

        skip_reasons = []
        for wid, acct in wallets:
            try:
                # Check in-memory positions first (catches pending/unconfirmed trades)
                inmem_matches = [
                    p for p in self.positions.values()
                    if p.symbol == symbol and (p.side == "LONG") == is_long and p.is_open and p.wallet_id == wid
                ]
                if inmem_matches:
                    reason = f"W{wid}: in-memory {symbol} {side_label} (pos {inmem_matches[0].id[:8]})"
                    self.logger.info(reason)
                    skip_reasons.append(reason)
                    continue

                # Then check on-chain (catches positions from before reboot or manual opens)
                chain_positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                matching = [
                    cp for cp in chain_positions
                    if cp.market.lower() == market_addr and cp.is_long == is_long
                ]
                if not matching:
                    self.logger.info(
                        f"W{wid} ({acct.address[:10]}...) free for {symbol} {side_label} — selected"
                    )
                    return wid, acct
                else:
                    reason = f"W{wid}: on-chain {symbol} {side_label} (size ${matching[0].size_usd:,.0f})"
                    self.logger.info(reason)
                    skip_reasons.append(reason)
            except Exception as e:
                reason = f"W{wid}: check failed — {e}"
                self.logger.warning(reason)
                skip_reasons.append(reason)
                continue

        # Store rejection details so caller can show them
        detail = "; ".join(skip_reasons) if skip_reasons else "unknown"
        self.logger.warning(f"No wallet for {symbol} {side_label}: {detail}")
        self._last_wallet_reject_reason = detail
        return None, None

    def get_portfolio_value(self) -> float:
        """Get USDC collateral token balance in USD (portfolio value) for wallet 1."""
        return self._get_portfolio_value_for(self.account)

    def _get_portfolio_value_for(self, acct: Account) -> float:
        """Get USDC collateral token balance in USD for a specific wallet."""
        try:
            token = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.cfg.collateral_token),
                abi=ERC20_ABI,
            )
            decimals = token.functions.decimals().call()
            balance_raw = token.functions.balanceOf(acct.address).call()
            balance = balance_raw / (10 ** decimals)
            return balance
        except Exception as e:
            self.logger.error(f"Error getting portfolio value: {e}")
            return 0.0

    async def _get_combined_usdc(self) -> float:
        """Get combined FREE USDC balance across all wallets (not deployed).
        All wallets act as one pool for sizing trades."""
        total_usdc = 0.0
        all_wallets = [acct for _, acct in self._all_wallets()]
        for acct in all_wallets:
            try:
                usdc = await asyncio.to_thread(self._get_portfolio_value_for, acct)
                total_usdc += usdc
            except Exception:
                pass
        return total_usdc

    async def _get_total_portfolio_value(self) -> float:
        """Get total portfolio value: free USDC + deployed collateral + unrealized PnL.

        This gives the true account value including capital locked in open positions.
        Used for position sizing so trades are portfolio_pct of TOTAL balance, not just free USDC.
        """
        # Free USDC across all wallets
        free_usdc = await self._get_combined_usdc()

        # Deployed collateral + PnL from on-chain positions
        deployed_value = 0.0
        all_wallets = [acct for _, acct in self._all_wallets()]
        for acct in all_wallets:
            try:
                positions = await asyncio.to_thread(
                    chain_fetch_positions, self.w3, acct.address
                )
                for pos in positions:
                    # Each position's value = collateral + unrealized PnL
                    deployed_value += pos.collateral_amount + pos.unrealized_pnl
            except Exception as e:
                self.logger.debug(f"Could not fetch positions for {acct.address[:10]}: {e}")

        total = free_usdc + deployed_value
        self.logger.debug(
            f"Portfolio: free=${free_usdc:.2f} + deployed=${deployed_value:.2f} = total=${total:.2f}"
        )
        return total

    # ──────────────────────────────────────────────────────────────────────
    # Balance snapshots (for 24h change tracking)
    # ──────────────────────────────────────────────────────────────────────

    def _save_balance_snapshot(self, total_portfolio: float):
        """Append a balance snapshot and prune entries older than 48h."""
        snapshots = self._load_balance_snapshots()
        snapshots.append({
            "timestamp": time.time(),
            "total_portfolio": total_portfolio,
        })
        cutoff = time.time() - (MAX_SNAPSHOT_AGE_HOURS * 3600)
        snapshots = [s for s in snapshots if s["timestamp"] >= cutoff]
        try:
            atomic_json_write(BALANCE_SNAPSHOTS_FILE, snapshots)
        except Exception as e:
            self.logger.warning(f"Failed to save balance snapshot: {e}")

    def _load_balance_snapshots(self) -> list:
        """Load balance snapshots from disk."""
        if not os.path.exists(BALANCE_SNAPSHOTS_FILE):
            return []
        try:
            with open(BALANCE_SNAPSHOTS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def _get_24h_balance_change(self, current_total: float):
        """Find snapshot closest to 24h ago and compute change.

        Returns (change_usd, change_pct, found).
        """
        snapshots = self._load_balance_snapshots()
        if not snapshots:
            return 0.0, 0.0, False

        target_ts = time.time() - 86400
        closest = min(snapshots, key=lambda s: abs(s["timestamp"] - target_ts))

        # Only use if within 6 hours of the 24h mark
        if abs(closest["timestamp"] - target_ts) > 6 * 3600:
            return 0.0, 0.0, False

        old_total = closest["total_portfolio"]
        if old_total == 0:
            return 0.0, 0.0, False

        change_usd = current_total - old_total
        change_pct = (change_usd / old_total) * 100
        return change_usd, change_pct, True

    async def _fund_wallet(self, target_wid: int, amount_needed: float):
        """Pull USDC from other wallets into the target wallet.

        Unlike _rebalance_wallets (which equalizes), this directly transfers
        the needed amount from wallets with available USDC into target_wid.
        """
        if self.cfg.dry_run:
            self.logger.info(f"[DRY_RUN] Would fund W{target_wid} with ${amount_needed:.2f}")
            return

        try:
            target_acct = self._get_account(target_wid)
            usdc_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.cfg.collateral_token),
                abi=ERC20_ABI,
            )
            decimals = await asyncio.to_thread(lambda: usdc_contract.functions.decimals().call())

            # Collect available balances from other wallets (sorted richest first)
            donors = []
            for wid, acct in self._all_wallets():
                if wid == target_wid:
                    continue
                bal = await asyncio.to_thread(self._get_portfolio_value_for, acct)
                if bal > 1.0:  # keep at least $1 dust in donor
                    donors.append((wid, acct, bal))
            donors.sort(key=lambda x: x[2], reverse=True)

            still_needed = amount_needed + 1.0  # +$1 buffer
            transferred = 0.0

            for donor_wid, donor_acct, donor_bal in donors:
                if still_needed <= 0:
                    break
                # Leave at least $1 in donor wallet
                available = donor_bal - 1.0
                send_amount = min(available, still_needed)
                if send_amount < 0.50:
                    continue

                raw_amount = round(send_amount * (10 ** decimals))
                transfer_data = usdc_contract.encode_abi(
                    "transfer",
                    [Web3.to_checksum_address(target_acct.address), raw_amount],
                )
                tx_hash = await asyncio.to_thread(
                    self._send_tx, self.cfg.collateral_token, transfer_data, 0, donor_acct
                )
                receipt = await asyncio.to_thread(open_wait_receipt, self.w3, tx_hash)

                if receipt.get("status") == 1:
                    transferred += send_amount
                    still_needed -= send_amount
                    self.logger.info(f"Funded W{target_wid}: ${send_amount:.2f} from W{donor_wid}")
                else:
                    self.logger.error(f"Fund transfer reverted: W{donor_wid} → W{target_wid}")

            if transferred > 0:
                await self.notify(f"💰 Auto-funded W{target_wid} with ${transferred:.2f} USDC from other wallets")
            else:
                await self.notify(f"⚠️ Could not fund W{target_wid} — no wallets have enough spare USDC")

        except Exception as e:
            self.logger.error(f"_fund_wallet error: {e}")
            await self.notify(f"⚠️ Auto-fund failed for W{target_wid}: {e}")

    async def _consolidate_to_wallet(self, target_wid: int = 1):
        """Move ALL free USDC from all other wallets into the target wallet."""
        if self.cfg.dry_run:
            self.logger.info(f"[DRY_RUN] Would consolidate all USDC to W{target_wid}")
            return []

        target_acct = self._get_account(target_wid)
        usdc_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.cfg.collateral_token),
            abi=ERC20_ABI,
        )
        decimals = await asyncio.to_thread(lambda: usdc_contract.functions.decimals().call())

        results = []
        for wid, acct in self._all_wallets():
            if wid == target_wid:
                continue
            try:
                _addr = acct.address
                bal_raw = await asyncio.to_thread(
                    lambda w=_addr: usdc_contract.functions.balanceOf(w).call()
                )
                bal = bal_raw / (10 ** decimals)
                if bal < 0.50:
                    results.append(f"W{wid}: ${bal:.2f} (skipped — dust)")
                    continue

                transfer_data = usdc_contract.encode_abi(
                    "transfer",
                    [Web3.to_checksum_address(target_acct.address), bal_raw],
                )
                tx_hash = await asyncio.to_thread(
                    self._send_tx, self.cfg.collateral_token, transfer_data, 0, acct
                )
                receipt = await asyncio.to_thread(open_wait_receipt, self.w3, tx_hash)

                if receipt.get("status") == 1:
                    results.append(f"W{wid} → W{target_wid}: ${bal:.2f}")
                else:
                    results.append(f"W{wid}: ${bal:.2f} FAILED (tx reverted)")
            except Exception as e:
                results.append(f"W{wid}: error — {e}")

        return results

    async def cmd_consolidate(self, chat_id: int):
        """Move all free USDC from W2-W4 into W1."""
        await self.send_message(chat_id, "Consolidating all USDC into W1...")

        results = await self._consolidate_to_wallet(1)

        # Show final W1 balance
        try:
            w1_bal = await asyncio.to_thread(self._get_portfolio_value_for, self.account)
            results.append(f"\nW1 balance: ${w1_bal:,.2f}")
        except Exception:
            pass

        msg = "**Consolidate → W1**\n\n" + "\n".join(results) if results else "No wallets to consolidate."
        await self.send_message(chat_id, msg)

    async def _rebalance_wallets(self):
        """Equalize USDC across all configured wallets (up to 4).

        Calculates the average balance, then each wallet above average sends
        its excess to the wallets below average. Handles 2, 3, or 4 wallets
        in a single pass with multiple transfers if needed.

        Called after positions open/close and hourly to keep wallets balanced."""
        wallets = self._all_wallets()
        if len(wallets) < 2:
            return  # Single wallet mode — nothing to balance

        if self.cfg.dry_run:
            self.logger.info("[DRY_RUN] Would rebalance wallets (skipped)")
            return

        try:
            # Fetch balances for all wallets
            balances = {}
            for wid, acct in wallets:
                bal = await asyncio.to_thread(self._get_portfolio_value_for, acct)
                balances[wid] = bal

            avg_bal = sum(balances.values()) / len(balances)

            # Find wallets above and below average
            above = {wid: bal - avg_bal for wid, bal in balances.items() if bal > avg_bal + 0.50}
            below = {wid: avg_bal - bal for wid, bal in balances.items() if bal < avg_bal - 0.50}

            if not above or not below:
                bal_str = ", ".join(f"W{wid}: ${bal:.2f}" for wid, bal in balances.items())
                self.logger.debug(f"Wallets balanced ({bal_str}, avg=${avg_bal:.2f})")
                return

            bal_str = ", ".join(f"W{wid}: ${bal:.2f}" for wid, bal in balances.items())
            self.logger.info(f"Rebalancing {len(wallets)} wallets (avg=${avg_bal:.2f}): {bal_str}")

            usdc_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.cfg.collateral_token),
                abi=ERC20_ABI,
            )
            decimals = await asyncio.to_thread(lambda: usdc_contract.functions.decimals().call())

            # Build transfer pairs: from each above-avg wallet to below-avg wallets
            transfers = []
            above_list = sorted(above.items(), key=lambda x: x[1], reverse=True)
            below_list = sorted(below.items(), key=lambda x: x[1], reverse=True)

            ai = 0  # index into above_list
            bi = 0  # index into below_list
            above_remaining = {wid: excess for wid, excess in above_list}
            below_remaining = {wid: deficit for wid, deficit in below_list}

            while ai < len(above_list) and bi < len(below_list):
                sender_wid = above_list[ai][0]
                receiver_wid = below_list[bi][0]

                send_amount = min(above_remaining[sender_wid], below_remaining[receiver_wid])

                if send_amount >= 0.50:  # minimum $0.50 transfer to avoid dust
                    transfers.append((sender_wid, receiver_wid, send_amount))
                    above_remaining[sender_wid] -= send_amount
                    below_remaining[receiver_wid] -= send_amount

                if above_remaining[sender_wid] < 0.50:
                    ai += 1
                if below_remaining[receiver_wid] < 0.50:
                    bi += 1

            if not transfers:
                self.logger.debug("No transfers needed after computing pairs")
                return

            # Execute transfers sequentially (each is a separate on-chain tx)
            transfer_log = []
            for sender_wid, receiver_wid, amount in transfers:
                sender_acct = self._get_account(sender_wid)
                receiver_acct = self._get_account(receiver_wid)
                raw_amount = round(amount * (10 ** decimals))

                transfer_data = usdc_contract.encode_abi(
                    "transfer",
                    [Web3.to_checksum_address(receiver_acct.address), raw_amount],
                )

                try:
                    tx_hash = await asyncio.to_thread(
                        self._send_tx,
                        self.cfg.collateral_token,
                        transfer_data,
                        0,
                        sender_acct,
                    )
                    receipt = await asyncio.to_thread(open_wait_receipt, self.w3, tx_hash)

                    if receipt.get("status") == 1:
                        transfer_log.append(f"W{sender_wid} → W{receiver_wid}: ${amount:.2f}")
                        self.logger.info(
                            f"Rebalance transfer OK: ${amount:.2f} W{sender_wid} → W{receiver_wid} (TX: {tx_hash})"
                        )
                    else:
                        self.logger.error(f"Rebalance tx reverted: W{sender_wid} → W{receiver_wid} (TX: {tx_hash})")
                        transfer_log.append(f"W{sender_wid} → W{receiver_wid}: FAILED")
                except Exception as e:
                    self.logger.error(f"Rebalance transfer error W{sender_wid} → W{receiver_wid}: {e}")
                    transfer_log.append(f"W{sender_wid} → W{receiver_wid}: ERROR")

            # Verify final balances
            new_bals = {}
            for wid, acct in wallets:
                new_bals[wid] = await asyncio.to_thread(self._get_portfolio_value_for, acct)
            new_str = " | ".join(f"W{wid}: ${bal:.2f}" for wid, bal in new_bals.items())

            self.logger.info(f"Rebalance complete: {new_str}")
            await self.notify(
                f"🔄 Wallets rebalanced ({len(transfers)} transfer(s))\n"
                + "\n".join(transfer_log) + "\n"
                + new_str
            )

        except Exception as e:
            self.logger.error(f"Wallet rebalance error: {e}")
            # Don't notify on rebalance failure — it's not critical

    async def rebalance_loop(self):
        """Check wallet balance every hour and auto-rebalance if needed."""
        while True:
            try:
                await asyncio.sleep(3600)  # 1 hour
                wallets = self._all_wallets()
                if len(wallets) < 2:
                    continue

                bals = {}
                for wid, acct in wallets:
                    bals[wid] = await asyncio.to_thread(self._get_portfolio_value_for, acct)

                max_bal = max(bals.values())
                min_bal = min(bals.values())
                diff = max_bal - min_bal

                bal_str = ", ".join(f"W{wid}: ${bal:.2f}" for wid, bal in bals.items())
                if diff > 1.0:
                    self.logger.info(f"Hourly rebalance check: {bal_str}, diff=${diff:.2f} — rebalancing")
                    await self._rebalance_wallets()
                else:
                    self.logger.debug(f"Hourly rebalance check: balanced ({bal_str})")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Rebalance loop error: {e}")

    async def gas_check_loop(self):
        """Check ETH gas balance every hour and auto top-up if needed."""
        while True:
            try:
                await asyncio.sleep(3600)  # 1 hour
                self.logger.debug("Hourly gas check running...")
                await self.topup_eth_if_needed()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Gas check loop error: {e}")

    async def topup_eth_if_needed(self):
        """Check ETH balance for all wallets and auto-topup if low.

        Swaps USDC → ETH via Uniswap V3 SwapRouter02 if any wallet's ETH
        balance drops below ~$5 worth.
        """
        MIN_ETH_USD = 5.0     # trigger topup below this
        TOPUP_USD = 5.0       # swap this much USDC → ETH

        UNISWAP_ROUTER = Web3.to_checksum_address("0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")
        WETH_ARBITRUM = Web3.to_checksum_address("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1")
        USDC_ARBITRUM = Web3.to_checksum_address("0xaf88d065e77c8cC2239327C5EDb3A432268e5831")
        POOL_FEE = 500

        # SwapRouter02 multicall signature: multicall(uint256 deadline, bytes[] data)
        UNISWAP_ABI = [
            {"name": "multicall", "type": "function", "stateMutability": "payable",
             "inputs": [{"name": "deadline", "type": "uint256"},
                        {"name": "data", "type": "bytes[]"}],
             "outputs": [{"name": "", "type": "bytes[]"}]},
            {"name": "exactInputSingle", "type": "function", "stateMutability": "payable",
             "inputs": [{"name": "params", "type": "tuple", "components": [
                 {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
                 {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"},
                 {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMinimum", "type": "uint256"},
                 {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
             "outputs": [{"name": "amountOut", "type": "uint256"}]},
            {"name": "unwrapWETH9", "type": "function", "stateMutability": "payable",
             "inputs": [{"name": "amountMinimum", "type": "uint256"}, {"name": "recipient", "type": "address"}],
             "outputs": []},
        ]

        if self.cfg.dry_run:
            return

        try:
            eth_price = await self.get_current_price("ETH")
            if not eth_price or eth_price <= 0:
                return

            for wid, acct in self._all_wallets():
                if not acct:
                    continue
                wallet_addr = acct.address
                eth_bal = await asyncio.to_thread(self.w3.eth.get_balance, wallet_addr)
                eth_usd = (eth_bal / 10**18) * eth_price

                if eth_usd >= MIN_ETH_USD:
                    continue

                self.logger.info(
                    f"W{wid} ETH low: ${eth_usd:.2f} (< ${MIN_ETH_USD}) — "
                    f"auto-swapping ${TOPUP_USD:.0f} USDC → ETH"
                )

                usdc_token = self.w3.eth.contract(address=USDC_ARBITRUM, abi=ERC20_ABI)
                # Bind wallet_addr early to avoid lambda closure bug
                _wallet = wallet_addr
                usdc_decimals = await asyncio.to_thread(lambda: usdc_token.functions.decimals().call())
                usdc_bal_raw = await asyncio.to_thread(lambda w=_wallet: usdc_token.functions.balanceOf(w).call())
                usdc_bal = usdc_bal_raw / (10 ** usdc_decimals)

                if usdc_bal < TOPUP_USD:
                    self.logger.warning(f"W{wid} insufficient USDC for auto-topup (${usdc_bal:.2f} < ${TOPUP_USD})")
                    continue

                usdc_amount_in = int(TOPUP_USD * (10 ** usdc_decimals))

                # Approve if needed
                allowance = await asyncio.to_thread(
                    lambda w=_wallet: usdc_token.functions.allowance(w, UNISWAP_ROUTER).call()
                )
                if allowance < usdc_amount_in:
                    approve_data = usdc_token.encode_abi("approve", [UNISWAP_ROUTER, 2**256 - 1])
                    approve_txh = await asyncio.to_thread(self._send_tx, USDC_ARBITRUM, approve_data, 0, acct)
                    await asyncio.to_thread(open_wait_receipt, self.w3, approve_txh)

                # Swap USDC → WETH → unwrap to ETH
                router = self.w3.eth.contract(address=UNISWAP_ROUTER, abi=UNISWAP_ABI)
                expected_eth = TOPUP_USD / eth_price
                min_eth_out = int(expected_eth * 0.95 * 10**18)  # 5% slippage
                swap_params = (USDC_ARBITRUM, WETH_ARBITRUM, POOL_FEE, UNISWAP_ROUTER, usdc_amount_in, min_eth_out, 0)
                swap_data = router.encode_abi("exactInputSingle", [swap_params])
                unwrap_data = router.encode_abi("unwrapWETH9", [min_eth_out, Web3.to_checksum_address(wallet_addr)])
                deadline = int(time.time()) + 300  # 5 min deadline
                call_data = router.encode_abi("multicall", [deadline, [swap_data, unwrap_data]])

                txh = await asyncio.to_thread(self._send_tx, UNISWAP_ROUTER, call_data, 0, acct)
                receipt = await asyncio.to_thread(open_wait_receipt, self.w3, txh)

                if receipt.get("status") == 1:
                    new_eth_bal = await asyncio.to_thread(self.w3.eth.get_balance, wallet_addr)
                    new_eth_usd = (new_eth_bal / 10**18) * eth_price
                    self.logger.info(f"W{wid} auto-topup OK: ${TOPUP_USD:.0f} USDC → ETH (now ${new_eth_usd:.2f})")
                else:
                    self.logger.error(f"W{wid} auto-topup swap reverted: {txh}")

        except Exception as e:
            self.logger.warning(f"topup_eth_if_needed error: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command handlers
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_balance(self, chat_id: int):
        """Display wallet balance summary: free USDC, deployed collateral, PnL.
        Auto-rebalances wallets before showing totals."""
        cfg = self.cfg
        try:
            # Auto-rebalance before showing totals
            await self._rebalance_wallets()

            total_usdc = 0.0
            total_deployed = 0.0
            wallet_lines = []
            wallet_roles = {1: "swing", 2: "scalp", 3: "scalp", 4: "scalp"}

            total_pnl = 0.0
            for wid, acct in self._all_wallets():
                usdc = await asyncio.to_thread(self._get_portfolio_value_for, acct)
                try:
                    positions = await asyncio.to_thread(chain_fetch_positions, self.w3, acct.address)
                    deployed = sum(p.collateral_amount for p in positions)
                    n_pos = len(positions)
                    total_pnl += sum(p.unrealized_pnl for p in positions)
                except Exception:
                    deployed = 0.0
                    n_pos = 0
                total_usdc += usdc
                total_deployed += deployed
                role = wallet_roles.get(wid, "scalp")
                label = f"W{wid} ({role})"
                addr = f"{acct.address[:10]}...{acct.address[-6:]}"
                dep_str = f"${deployed:,.2f}" if deployed > 0 else "$0.00"
                wallet_lines.append(
                    f"**{label}** {addr}\n"
                    f"  USDC: ${usdc:,.2f} | Deployed: {dep_str} | Positions: {n_pos}"
                )

            total_portfolio = total_usdc + total_deployed + total_pnl
            collateral_per_trade = total_portfolio * cfg.portfolio_pct
            pnl_sign = "+" if total_pnl >= 0 else ""

            # 24h change
            change_usd, change_pct, has_24h = self._get_24h_balance_change(total_portfolio)
            if has_24h:
                c_sign = "+" if change_usd >= 0 else ""
                change_str = f" ({c_sign}${change_usd:,.2f} / {c_sign}{change_pct:.1f}% in 24h)"
            else:
                change_str = ""

            # Bitunix balance
            bx_section = ""
            bx_total = 0.0
            bx_client = getattr(self, "bitunix_client", None)
            ex_mode = getattr(self, "exchange_mode", "gmx")
            if bx_client and ex_mode in ("bitunix", "mirror"):
                try:
                    from bitunix_executor import get_bitunix_balance, get_bitunix_positions
                    bx_bal = await asyncio.to_thread(get_bitunix_balance, bx_client)
                    bx_positions = await asyncio.to_thread(get_bitunix_positions, bx_client)
                    bx_deployed = 0.0
                    bx_pnl = 0.0
                    for bp in bx_positions:
                        bx_deployed += float(bp.get("margin", 0))
                        bx_pnl += float(bp.get("unrealizedPNL", 0))
                    bx_total = bx_bal + bx_deployed + bx_pnl
                    bx_collateral = bx_total * cfg.portfolio_pct
                    bx_pnl_sign = "+" if bx_pnl >= 0 else ""
                    bx_pnl_pct = f" ({bx_pnl / bx_deployed * 100:+.1f}%)" if bx_deployed > 0 else ""
                    bx_section = (
                        "\n\n**Bitunix (CEX)**\n"
                        f"Available USDT: ${bx_bal:,.2f}\n"
                        f"Deployed Margin: ${bx_deployed:,.2f} | Positions: {len(bx_positions)}\n"
                        f"Unrealized PnL: {bx_pnl_sign}${bx_pnl:,.2f}{bx_pnl_pct}\n"
                        f"**Bitunix Total: ${bx_total:,.2f}**\n"
                        f"Collateral/trade: ${bx_collateral:,.2f} ({cfg.portfolio_pct:.0%} of ${bx_total:,.2f})"
                    )
                except Exception as e:
                    bx_section = f"\n\n**Bitunix (CEX)**\nError fetching balance: {e}"

            # Combined total across both exchanges
            combined_section = ""
            if bx_total > 0:
                grand_total = total_portfolio + bx_total
                combined_section = f"\n\n**Combined Total (GMX + Bitunix): ${grand_total:,.2f}**"

            pnl_pct_str = f" ({total_pnl / total_deployed * 100:+.1f}%)" if total_deployed > 0 else ""
            msg = (
                "**GMX (On-Chain)**\n\n"
                + "\n".join(wallet_lines)
                + "\n\n**Combined GMX**\n"
                f"Free USDC: ${total_usdc:,.2f}\n"
                f"Deployed: ${total_deployed:,.2f}\n"
                f"Unrealized PnL: {pnl_sign}${total_pnl:,.2f}{pnl_pct_str}\n"
                f"**GMX Total: ${total_portfolio:,.2f}**{change_str}\n"
                f"Collateral/trade: ${collateral_per_trade:,.2f} ({cfg.portfolio_pct:.0%} of ${total_portfolio:,.2f})"
                + bx_section
                + combined_section
            )
            await self.send_message(chat_id, msg)
        except Exception as e:
            await self.send_message(chat_id, f"Error: {e}")

    async def cmd_gas(self, chat_id: int):
        """Display ETH gas balances for all wallets."""
        try:
            eth_price = await self.get_current_price("ETH")
            if not eth_price:
                try:
                    eth_price = await asyncio.to_thread(close_fetch_current_price, "ETH", self.w3)
                except Exception:
                    eth_price = 0.0

            wallet_roles = {1: "swing", 2: "scalp", 3: "scalp", 4: "scalp"}
            lines = ["**Gas Balances (ETH)**\n"]
            total_eth = 0.0

            for wid, acct in self._all_wallets():
                try:
                    eth_bal = await asyncio.to_thread(self.w3.eth.get_balance, acct.address)
                    eth_amount = eth_bal / 10**18
                    eth_usd = eth_amount * eth_price if eth_price else 0
                    total_eth += eth_amount
                    role = wallet_roles.get(wid, "scalp")
                    lines.append(f"W{wid} ({role}): {eth_amount:.6f} ETH (${eth_usd:.2f})")
                except Exception as e:
                    lines.append(f"❌ W{wid}: error ({e})")

            total_usd = total_eth * eth_price if eth_price else 0
            lines.append(f"\nTotal: {total_eth:.6f} ETH (${total_usd:.2f})")
            lines.append("\nAuto top-up is active (swaps USDC → ETH when balance < $5).")

            await self.send_message(chat_id, "\n".join(lines))
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching gas balances: {e}")

    async def cmd_balance_wallets(self, chat_id: int):
        """Rebalance USDC across wallets."""
        wallets = self._all_wallets()
        if len(wallets) < 2:
            await self.send_message(chat_id, "Single wallet mode — nothing to rebalance.")
            return

        before_bals = {}
        for wid, acct in wallets:
            before_bals[wid] = await asyncio.to_thread(self._get_portfolio_value_for, acct)
        before_str = "\n".join(f"  W{wid}: ${bal:,.2f}" for wid, bal in before_bals.items())
        diff = max(before_bals.values()) - min(before_bals.values())

        await self.send_message(chat_id, f"Before:\n{before_str}\n  Spread: ${diff:,.2f}\n\nRebalancing...")
        await self._rebalance_wallets()

        after_bals = {}
        for wid, acct in wallets:
            after_bals[wid] = await asyncio.to_thread(self._get_portfolio_value_for, acct)
        after_str = "\n".join(f"  W{wid}: ${bal:,.2f}" for wid, bal in after_bals.items())
        new_diff = max(after_bals.values()) - min(after_bals.values())

        if new_diff < diff:
            await self.send_message(chat_id, f"After:\n{after_str}\n  Spread: ${new_diff:,.2f}\n\n✅ Wallets rebalanced")
        elif diff < 1.0:
            await self.send_message(chat_id, f"Wallets already balanced (spread ${diff:,.2f} < $1.00)")
        else:
            await self.send_message(chat_id, f"After:\n{after_str}\n\n⚠️ Rebalance may have failed — check logs")

    async def cmd_topup(self, chat_id: int, arg: Optional[str] = None):
        """Manual ETH top-up command. Delegates to the bot's topup logic."""
        # This calls the topup method on GMXBot which handles the Uniswap swap logic
        # We just handle the /topup display and argument parsing here
        cfg = self.cfg

        UNISWAP_ROUTER = Web3.to_checksum_address("0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")
        WETH_ARBITRUM = Web3.to_checksum_address("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1")
        USDC_ARBITRUM = Web3.to_checksum_address("0xaf88d065e77c8cC2239327C5EDb3A432268e5831")
        POOL_FEE = 500

        # SwapRouter02 multicall signature: multicall(uint256 deadline, bytes[] data)
        UNISWAP_ABI = [
            {"name": "multicall", "type": "function", "stateMutability": "payable",
             "inputs": [{"name": "deadline", "type": "uint256"},
                        {"name": "data", "type": "bytes[]"}],
             "outputs": [{"name": "", "type": "bytes[]"}]},
            {"name": "exactInputSingle", "type": "function", "stateMutability": "payable",
             "inputs": [{"name": "params", "type": "tuple", "components": [
                 {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
                 {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"},
                 {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMinimum", "type": "uint256"},
                 {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
             "outputs": [{"name": "amountOut", "type": "uint256"}]},
            {"name": "unwrapWETH9", "type": "function", "stateMutability": "payable",
             "inputs": [{"name": "amountMinimum", "type": "uint256"}, {"name": "recipient", "type": "address"}],
             "outputs": []},
        ]

        all_wallets = self._all_wallets()

        eth_price = await self.get_current_price("ETH")
        if not eth_price:
            try:
                eth_price = await asyncio.to_thread(close_fetch_current_price, "ETH", self.w3)
            except Exception:
                eth_price = 0.0

        if not arg:
            lines = ["**ETH Balances (Gas)**\n"]
            for wid, acct in all_wallets:
                try:
                    eth_bal = await asyncio.to_thread(self.w3.eth.get_balance, acct.address)
                    eth_amount = eth_bal / 10**18
                    eth_usd = eth_amount * eth_price if eth_price else 0
                    lines.append(f"W{wid}: {eth_amount:.6f} ETH (~${eth_usd:.2f})")
                except Exception as e:
                    lines.append(f"W{wid}: error fetching balance ({e})")
            lines.append("\nUsage: /topup <1|2|3|4|all> [amount_usd]\nDefault: $5 USDC → ETH")
            await self.send_message(chat_id, "\n".join(lines))
            return

        parts = arg.strip().split()
        target = parts[0].lower()
        topup_usd = 5.0

        if len(parts) >= 2:
            try:
                topup_usd = float(parts[1].replace("$", ""))
            except ValueError:
                await self.send_message(chat_id, f"Invalid amount: {parts[1]}\nUsage: /topup <1|2|3|4|all> [amount_usd]")
                return

        if topup_usd < 1 or topup_usd > 100:
            await self.send_message(chat_id, "Amount must be between $1 and $100")
            return

        wallet_map = {
            "1": (1, self.account), "w1": (1, self.account),
            "2": (2, self.account2), "w2": (2, self.account2),
            "3": (3, self.account3), "w3": (3, self.account3),
            "4": (4, self.account4), "w4": (4, self.account4),
        }
        if target == "all":
            targets = all_wallets
        elif target in wallet_map:
            wid, acct = wallet_map[target]
            if not acct:
                await self.send_message(chat_id, f"Wallet {target} not configured")
                return
            targets = [(wid, acct)]
        else:
            await self.send_message(chat_id, f"Unknown target: {target}\nUsage: /topup <1|2|3|4|all> [amount_usd]")
            return

        if cfg.dry_run:
            wallet_names = ", ".join(f"W{wid}" for wid, _ in targets)
            await self.send_message(chat_id, f"[DRY RUN] Would swap ${topup_usd:.0f} USDC → ETH for {wallet_names}")
            return

        results = []
        for wid, acct in targets:
            label = f"W{wid}"
            try:
                _wallet = acct.address  # bind early to avoid lambda closure bug
                usdc_token = self.w3.eth.contract(address=USDC_ARBITRUM, abi=ERC20_ABI)
                usdc_decimals = await asyncio.to_thread(lambda: usdc_token.functions.decimals().call())
                usdc_bal_raw = await asyncio.to_thread(lambda w=_wallet: usdc_token.functions.balanceOf(w).call())
                usdc_bal = usdc_bal_raw / (10 ** usdc_decimals)

                if usdc_bal < topup_usd:
                    results.append(f"{label}: insufficient USDC (${usdc_bal:.2f} < ${topup_usd:.0f})")
                    continue

                usdc_amount_in = int(topup_usd * (10 ** usdc_decimals))

                allowance = await asyncio.to_thread(
                    lambda w=_wallet: usdc_token.functions.allowance(w, UNISWAP_ROUTER).call()
                )
                if allowance < usdc_amount_in:
                    approve_data = usdc_token.encode_abi("approve", [UNISWAP_ROUTER, 2**256 - 1])
                    approve_txh = await asyncio.to_thread(self._send_tx, USDC_ARBITRUM, approve_data, 0, acct)
                    await asyncio.to_thread(open_wait_receipt, self.w3, approve_txh)

                router = self.w3.eth.contract(address=UNISWAP_ROUTER, abi=UNISWAP_ABI)
                min_eth_out = 0
                if eth_price and eth_price > 0:
                    expected_eth = topup_usd / eth_price
                    min_eth_out = int(expected_eth * 0.97 * 10**18)  # 3% slippage
                swap_params = (USDC_ARBITRUM, WETH_ARBITRUM, POOL_FEE, UNISWAP_ROUTER, usdc_amount_in, min_eth_out, 0)
                swap_data = router.encode_abi("exactInputSingle", [swap_params])
                unwrap_min = min_eth_out if min_eth_out > 0 else 0
                unwrap_data = router.encode_abi("unwrapWETH9", [unwrap_min, Web3.to_checksum_address(_wallet)])
                deadline = int(time.time()) + 300  # 5 min deadline
                call_data = router.encode_abi("multicall", [deadline, [swap_data, unwrap_data]])

                txh = await asyncio.to_thread(self._send_tx, UNISWAP_ROUTER, call_data, 0, acct)
                receipt = await asyncio.to_thread(open_wait_receipt, self.w3, txh)

                if receipt.get("status") == 1:
                    new_eth_bal = await asyncio.to_thread(self.w3.eth.get_balance, _wallet)
                    new_eth_usd = (new_eth_bal / 10**18) * (eth_price or 0)
                    results.append(f"{label}: swapped ${topup_usd:.0f} USDC → ETH (balance: ${new_eth_usd:.2f})")
                else:
                    results.append(f"{label}: swap TX reverted ({txh[:18]}...)")

            except Exception as e:
                results.append(f"{label}: error — {e}")

        await self.send_message(chat_id, "**ETH Top-Up**\n\n" + "\n".join(results))

    # /withdraw and /deposit are in withdraw_mixin.py (WithdrawMixin)
