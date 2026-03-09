"""
Analytics Mixin for GMX V2 Trading Bot.

Contains analytics methods for:
  - Win rate calculations
  - Trade recording & history tracking
  - Health reporting
  - Daily summaries

AnalyticsMixin is designed to be mixed into GMXBot.

Expected host class attributes:
  cfg, logger, client, notify, send_message,
  positions, trade_history, health_stats, last_heartbeat,
  _all_wallets, _get_account, get_health_report (partially defined here),
  _record_trade (defined here), calculate_win_rate (defined here),
  send_daily_summary (defined here), daily_summary_loop (defined here)
"""

import json
import os
import time
import uuid
import asyncio
import logging
import tempfile
import statistics
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fpdf import FPDF

from close import fetch_positions as chain_fetch_positions
from history import fetch_recent_position_decreases
from risk import calculate_unrealized_pnl, calculate_pnl_percentage
from state_io import atomic_json_write, safe_json_read


logger = logging.getLogger("GMXBot.analytics")

TRADE_HISTORY_FILE = "trade_history.json"
ONCHAIN_TRADES_FILE = "onchain_trades.json"

# Only show trades from this date forward (UTC midnight).
# Change this date to reset your trade history starting point.
TRADE_START_DATE = "2026-03-07"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data structures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class TradeRecord:
    id: str
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    size_usd: float
    leverage: float
    duration_hours: float
    pnl_usd: float
    pnl_percentage: float
    exit_reason: str
    opened_at: float
    closed_at: float
    wallet_id: int = 0  # 0 = unknown (legacy records), 1-4 = wallet
    exchange: str = "gmx"  # "gmx" or "bitunix"


class AnalyticsMixin:
    """Mixin providing analytics & reporting methods for GMXBot."""

    def calculate_win_rate(self, symbol: str = None, n: int = None) -> Dict[str, Any]:
        """Calculate win rate from internal trade history (sync fallback).

        For accurate fee-inclusive metrics, use calculate_win_rate_onchain() instead.

        Args:
            symbol: Filter trades by symbol (e.g., 'BTC'). None = all symbols.
            n: Limit to last N trades. None = all trades.

        Returns:
            Dict with keys: win_rate, wins, losses, total, avg_win, avg_loss, pnl
        """
        # Exclude dust trades (< $1 PnL)
        trades = [t for t in self.trade_history if abs(t.pnl_usd) >= 1]

        if symbol:
            trades = [t for t in trades if t.symbol == symbol]

        if n and n > 0:
            trades = trades[-n:]

        if not trades:
            return {"win_rate": 0, "wins": 0, "losses": 0, "total": 0, "avg_win": 0, "avg_loss": 0, "pnl": 0}

        wins = [t for t in trades if t.pnl_usd > 0]
        losses = [t for t in trades if t.pnl_usd < 0]
        total_pnl = sum(t.pnl_usd for t in trades)

        return {
            "win_rate": len(wins) / len(trades) * 100 if trades else 0,
            "wins": len(wins),
            "losses": len(losses),
            "total": len(trades),
            "avg_win": sum(t.pnl_usd for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t.pnl_usd for t in losses) / len(losses) if losses else 0,
            "pnl": total_pnl,
        }

    def calculate_platform_stats(self, exchange: str = None) -> Dict[str, Any]:
        """Calculate performance stats optionally filtered by exchange platform."""
        trades = [t for t in self.trade_history if abs(t.pnl_usd) >= 1]
        if exchange:
            trades = [t for t in trades if getattr(t, 'exchange', 'gmx') == exchange]

        if not trades:
            return {"win_rate": 0, "wins": 0, "losses": 0, "total": 0,
                    "avg_win": 0, "avg_loss": 0, "pnl": 0, "best": 0, "worst": 0}

        wins = [t for t in trades if t.pnl_usd > 0]
        losses = [t for t in trades if t.pnl_usd < 0]
        total_pnl = sum(t.pnl_usd for t in trades)

        return {
            "win_rate": len(wins) / len(trades) * 100 if trades else 0,
            "wins": len(wins),
            "losses": len(losses),
            "total": len(trades),
            "avg_win": sum(t.pnl_usd for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t.pnl_usd for t in losses) / len(losses) if losses else 0,
            "pnl": total_pnl,
            "best": max((t.pnl_usd for t in trades), default=0),
            "worst": min((t.pnl_usd for t in trades), default=0),
        }

    def get_platform_comparison(self) -> str:
        """Generate a formatted platform performance comparison message."""
        gmx = self.calculate_platform_stats("gmx")
        bx = self.calculate_platform_stats("bitunix")
        combined = self.calculate_platform_stats()

        gmx_open = [p for p in self.positions.values() if p.is_open and p.exchange == "gmx"]
        bx_open = [p for p in self.positions.values() if p.is_open and p.exchange == "bitunix"]
        gmx_exposure = sum(p.size_usd for p in gmx_open)
        bx_exposure = sum(p.size_usd for p in bx_open)
        gmx_upnl = sum(p.unrealized_pnl for p in gmx_open)
        bx_upnl = sum(p.unrealized_pnl for p in bx_open)

        def _fmt_stats(label: str, stats: dict, n_open: int, exposure: float, upnl: float) -> str:
            if stats["total"] == 0 and n_open == 0:
                return f"**{label}:** No trades\n"
            wr = f"{stats['win_rate']:.1f}%" if stats["total"] else "N/A"
            pnl_sign = "+" if stats["pnl"] >= 0 else ""
            upnl_sign = "+" if upnl >= 0 else ""
            lines = [f"**{label}**"]
            if n_open > 0:
                lines.append(f"  Open: {n_open} (${exposure:,.0f} exposure)")
                lines.append(f"  Unrealized: {upnl_sign}${upnl:,.2f}")
            if stats["total"] > 0:
                lines.append(f"  Closed: {stats['total']} ({stats['wins']}W / {stats['losses']}L)")
                lines.append(f"  Win Rate: {wr}")
                lines.append(f"  Realized PnL: {pnl_sign}${stats['pnl']:,.2f}")
                lines.append(f"  Avg Win: ${stats['avg_win']:,.2f}")
                lines.append(f"  Avg Loss: ${stats['avg_loss']:,.2f}")
                lines.append(f"  Best: ${stats['best']:+,.2f}")
                lines.append(f"  Worst: ${stats['worst']:+,.2f}")
            return "\n".join(lines) + "\n"

        msg = "**Platform Performance**\n\n"
        msg += _fmt_stats("GMX (On-Chain)", gmx, len(gmx_open), gmx_exposure, gmx_upnl)
        msg += "\n"
        msg += _fmt_stats("Bitunix (CEX)", bx, len(bx_open), bx_exposure, bx_upnl)

        if combined["total"] > 0:
            total_upnl = gmx_upnl + bx_upnl
            total_sign = "+" if (combined["pnl"] + total_upnl) >= 0 else ""
            msg += (
                f"\n**Combined**\n"
                f"  Total Trades: {combined['total']}\n"
                f"  Win Rate: {combined['win_rate']:.1f}%\n"
                f"  Realized: {'+'if combined['pnl']>=0 else ''}${combined['pnl']:,.2f}\n"
                f"  Unrealized: {'+'if total_upnl>=0 else ''}${total_upnl:,.2f}\n"
                f"  Net: {total_sign}${combined['pnl'] + total_upnl:,.2f}"
            )

        return msg

    async def _rebuild_trade_history_from_chain(self):
        """Clear and rebuild trade history from on-chain PositionDecrease events.

        Groups all decrease events by (market, direction) into single aggregated
        trades so that multiple TP hits for the same position appear as one trade.
        Excludes currently-open positions (their events are still in-flight).
        Called on every startup to ensure a clean, accurate state.
        """
        from collections import defaultdict
        from history import fetch_trade_history

        # Clear local caches
        self.trade_history = []
        self._save_trade_history()
        self._save_onchain_trades([])

        # Fetch fresh on-chain events across all wallets
        on_chain = await self._fetch_and_store_trades()
        if not on_chain:
            self.logger.info("Trade rebuild: no on-chain events found")
            return

        # Build market → symbol map
        market_to_sym = {}
        for sym, addr in self.cfg.markets.items():
            market_to_sym[addr.lower()] = sym

        # Identify currently-open positions so we can exclude their events
        open_keys = set()
        for pos in self.positions.values():
            if pos.is_open and pos.market_addr:
                is_long = pos.side == "LONG"
                open_keys.add((pos.market_addr.lower(), is_long))

        # Group events by (market_address, is_long)
        # On GMX each account has at most one position per (market, direction)
        # at a time, so all decreases in the same group belong to the same trade.
        groups = defaultdict(list)
        for t in on_chain:
            key = (t.get("market_address", "").lower(), t.get("is_long", True))
            groups[key].append(t)

        for (market, is_long), events in groups.items():
            # Skip events belonging to still-open positions
            if (market, is_long) in open_keys:
                continue

            sym = market_to_sym.get(market)
            if not sym:
                continue

            side = "LONG" if is_long else "SHORT"

            def _net(e):
                return e.get("net_pnl_usd", e.get("pnl_usd", 0))

            total_pnl = sum(_net(e) for e in events)
            total_size = sum(e.get("size_delta_usd", 0) for e in events)

            if abs(total_pnl) < 1:
                continue  # skip dust

            events_sorted = sorted(events, key=lambda e: e.get("timestamp", 0))
            last_event = events_sorted[-1]
            first_event = events_sorted[0]
            exit_price = last_event.get("execution_price", 0)
            entry_price = first_event.get("execution_price", 0)

            duration_hours = max(
                (last_event.get("timestamp", 0) - first_event.get("timestamp", 0)) / 3600,
                0,
            )
            pnl_pct = (total_pnl / total_size * 100) if total_size > 0 else 0

            trade = TradeRecord(
                id=f"rebuild_{sym}_{side}_{int(first_event.get('timestamp', 0))}",
                symbol=sym,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                size_usd=total_size,
                leverage=0,
                duration_hours=duration_hours,
                pnl_usd=total_pnl,
                pnl_percentage=pnl_pct,
                exit_reason="on-chain",
                opened_at=first_event.get("timestamp", 0),
                closed_at=last_event.get("timestamp", 0),
                exchange="gmx",
            )
            self.trade_history.append(trade)

        self.trade_history.sort(key=lambda t: t.closed_at)
        self._save_trade_history()
        self.logger.info(
            f"Trade rebuild: {len(self.trade_history)} grouped trade(s) "
            f"from {len(on_chain)} on-chain event(s)"
        )

    async def cmd_performance(self, chat_id: int):
        """Send platform performance comparison to admin."""
        msg = self.get_platform_comparison()
        await self.send_message(chat_id, msg)

    async def calculate_win_rate_onchain(self, symbol: str = None, n: int = None) -> Dict[str, Any]:
        """Calculate win rate from on-chain PositionDecrease events (fee-inclusive PnL).

        Uses _fetch_and_store_trades() which queries on-chain event logs and
        merges with locally stored history. PnL values include borrowing fees,
        funding fees, price impact, and position fees.

        Args:
            symbol: Filter trades by symbol (e.g., 'BTC'). None = all symbols.
            n: Limit to last N trades. None = all trades.

        Returns:
            Dict with keys: win_rate, wins, losses, total, avg_win, avg_loss, pnl
        """
        all_trades = await self._fetch_and_store_trades()

        market_to_sym = {}
        for sym, addr in self.cfg.markets.items():
            market_to_sym[addr.lower()] = sym

        # Group by position (market + direction) so TPs count as one trade
        trades = self._group_onchain_trades(all_trades, market_to_sym)

        if symbol:
            trades = [t for t in trades if t["sym"] == symbol.upper()]
        if n and n > 0:
            trades = trades[-n:]

        if not trades:
            return {"win_rate": 0, "wins": 0, "losses": 0, "total": 0, "avg_win": 0, "avg_loss": 0, "pnl": 0}

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        total_pnl = sum(t["pnl"] for t in trades)

        return {
            "win_rate": len(wins) / len(trades) * 100,
            "wins": len(wins),
            "losses": len(losses),
            "total": len(trades),
            "avg_win": sum(t["pnl"] for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t["pnl"] for t in losses) / len(losses) if losses else 0,
            "pnl": total_pnl,
        }

    async def _record_trade(self, pos_obj, exit_reason: str = "manual"):
        """Record a closed position as a trade in history.

        Fetches actual exit price and PnL from on-chain PositionDecrease
        events when available, falling back to internal data if the RPC
        call fails or no events are found.

        Args:
            pos_obj: Position object with id, symbol, side, entry_price, size_usd, leverage, etc.
            exit_reason: Why the position closed ('manual', 'tp_hit', 'sl_triggered', 'liquidation_or_manual', 'override')
        """
        if pos_obj.closed_at is None:
            pos_obj.closed_at = time.time()

        # Default exit price
        exit_price = pos_obj.current_price if pos_obj.current_price > 0 else pos_obj.entry_price

        # Prefer verified_decreases (complete TP/SL hit history) over re-fetching from chain.
        # The chain re-fetch has a 30-min window and may miss earlier TP fills.
        verified = getattr(pos_obj, 'verified_decreases', None) or []
        if verified:
            pnl_usd = sum(d.get("net_pnl_usd", 0) for d in verified)
            last_decrease = max(verified, key=lambda d: d.get("timestamp", 0))
            if last_decrease.get("execution_price", 0) > 0:
                exit_price = last_decrease["execution_price"]
            # Add remaining position PnL if not fully closed by TPs
            total_decreased = sum(d.get("size_delta_usd", 0) for d in verified)
            base_size = pos_obj.original_size_usd if pos_obj.original_size_usd > 0 else pos_obj.size_usd
            remaining_size = max(base_size - total_decreased, 0.0)
            if remaining_size > 0 and exit_price > 0 and pos_obj.entry_price > 0:
                remaining_pnl = calculate_unrealized_pnl(
                    pos_obj.side, pos_obj.entry_price, exit_price, remaining_size
                )
                pnl_usd += remaining_pnl
            self.logger.info(
                f"Trade PnL from verified_decreases: ${pnl_usd:,.2f} "
                f"({len(verified)} event(s)), exit=${exit_price:,.2f}"
            )
        else:
            # No verified_decreases — use position's computed PnL
            if pos_obj.unrealized_pnl is not None and pos_obj.unrealized_pnl != 0.0:
                pnl_usd = pos_obj.unrealized_pnl
            else:
                pnl_usd = calculate_unrealized_pnl(
                    pos_obj.side,
                    pos_obj.entry_price,
                    exit_price,
                    pos_obj.size_usd,
                )

        duration = pos_obj.duration_hours
        full_size = pos_obj.original_size_usd if pos_obj.original_size_usd > 0 else pos_obj.size_usd
        pnl_pct = calculate_pnl_percentage(pnl_usd, full_size, pos_obj.leverage)

        trade = TradeRecord(
            id=pos_obj.id,
            symbol=pos_obj.symbol,
            side=pos_obj.side,
            entry_price=pos_obj.entry_price,
            exit_price=exit_price,
            size_usd=full_size,
            leverage=pos_obj.leverage,
            duration_hours=duration,
            pnl_usd=pnl_usd,
            pnl_percentage=pnl_pct,
            exit_reason=exit_reason,
            opened_at=pos_obj.opened_at,
            closed_at=pos_obj.closed_at,
            wallet_id=getattr(pos_obj, 'wallet_id', 0),
            exchange=getattr(pos_obj, 'exchange', 'gmx'),
        )
        self.trade_history.append(trade)
        self._save_trade_history()
        self.logger.info(
            f"Trade recorded: {trade.symbol} {trade.side} [{trade.exchange.upper()}] "
            f"PnL=${trade.pnl_usd:,.2f} ({trade.pnl_percentage:+.1f}%) [{exit_reason}]"
        )

    def _save_trade_history(self):
        """Persist trade history to disk as JSON (atomic write with backup)."""
        try:
            data = [asdict(t) for t in self.trade_history]
            atomic_json_write(TRADE_HISTORY_FILE, data)
        except Exception as e:
            logger.warning(f"Failed to save trade history: {e}")

    async def cmd_reset(self, chat_id: int):
        """Wipe local trade history cache. Hidden command."""
        self.trade_history = []
        self._save_trade_history()
        self._save_onchain_trades([])
        logger.info("Trade history cache cleared by admin")
        await self.send_message(chat_id, f"Local cache cleared. Trades only count from {TRADE_START_DATE} onward.")

    def _load_trade_history(self):
        """Load trade history from disk on startup (with .bak fallback)."""
        data = safe_json_read(TRADE_HISTORY_FILE, default=[])
        if data:
            self.trade_history = []
            skipped = 0
            for record in data:
                try:
                    self.trade_history.append(TradeRecord(**record))
                except Exception as e:
                    skipped += 1
                    logger.warning(f"Skipping corrupt trade record: {e}")
            if skipped:
                logger.warning(f"Skipped {skipped} corrupt trade record(s)")
            logger.info(f"Loaded {len(self.trade_history)} trade(s) from {TRADE_HISTORY_FILE}")

    def get_health_report(self) -> Dict[str, Any]:
        """Get bot health status.

        Returns:
            Dict with keys: uptime_seconds, is_halted, halt_reason, open_positions,
            total_positions, signals_processed, trades_executed, errors, price_updates, last_heartbeat
        """
        open_positions = sum(1 for p in self.positions.values() if p.is_open)
        return {
            "uptime_seconds": time.time() - self.health_stats["uptime_start"],
            "is_halted": self.is_halted,
            "halt_reason": self.halt_reason,
            "open_positions": open_positions,
            "total_positions": len(self.positions),
            "signals_processed": self.health_stats["signals_processed"],
            "trades_executed": self.health_stats["trades_executed"],
            "errors": self.health_stats["errors"],
            "price_updates": self.health_stats["price_updates"],
            "last_heartbeat": self.last_heartbeat,
        }

    # NOTE: daily_summary_loop() and send_daily_summary() are defined in
    # CoreTelegramMixin (telegram.py) with richer formatting (wallet balances,
    # symbol breakdown, open positions). They are NOT duplicated here to avoid
    # MRO conflicts where this simpler version would shadow the richer one.

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /winrate
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_winrate(self, chat_id: int, symbol: Optional[str], n: Optional[int]):
        """Telegram /winrate command handler using on-chain trade data.

        Args:
            chat_id: Telegram chat ID
            symbol: Filter by symbol (e.g., 'BTC'). None = all symbols.
            n: Limit to last N trades. None = all trades.

        Usage:
            /winrate — all-time win rate
            /winrate BTC — BTC only
            /winrate BTC 20 — last 20 BTC trades
        """
        PNL_SYMBOLS = set(self.cfg.markets.keys()) if self.cfg.markets else {"BTC", "SOL", "ETH"}
        market_to_sym = {}
        for sym, addr in self.cfg.markets.items():
            if sym in PNL_SYMBOLS:
                market_to_sym[addr.lower()] = sym

        # Fetch on-chain trades and group by position (market + direction)
        all_trades = await self._fetch_and_store_trades()
        trades = self._group_onchain_trades(all_trades, market_to_sym)

        if symbol:
            trades = [t for t in trades if t["sym"] == symbol.upper()]

        if n and n > 0:
            trades = trades[-n:]

        if not trades:
            label = f" for {symbol}" if symbol else ""
            await self.send_message(chat_id, f"No closed trades{label} yet.")
            return

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        total_pnl = sum(t["pnl"] for t in trades)
        win_rate = len(wins) / len(trades) * 100
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0

        title = "Win Rate"
        if symbol:
            title += f" — {symbol.upper()}"
        if n:
            title += f" (last {n})"

        pnl_sign = "+" if total_pnl >= 0 else ""
        msg = (
            f"**{title}**\n\n"
            f"Win Rate: {win_rate:.1f}% ({len(wins)}/{len(trades)})\n"
            f"Net PnL: {pnl_sign}${total_pnl:,.2f}\n"
            f"Avg Win: +${avg_win:,.2f}\n"
            f"Avg Loss: ${avg_loss:,.2f}"
        )
        await self.send_message(chat_id, msg)

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /pnl
    # ──────────────────────────────────────────────────────────────────────

    # ── On-chain trade grouping ──

    def _group_onchain_trades(self, on_chain: List[Dict[str, Any]], market_to_sym: dict) -> List[Dict[str, Any]]:
        """Group on-chain PositionDecrease events by (market, direction) into aggregated trades.

        Returns a list of dicts with keys: sym, pnl, timestamp, market_address, is_long.
        Each entry represents one logical trade (all decreases for the same position combined).
        """
        from collections import defaultdict

        groups = defaultdict(list)
        for t in on_chain:
            key = (t.get("market_address", "").lower(), t.get("is_long", True))
            groups[key].append(t)

        # Exclude currently-open positions
        open_keys = set()
        for pos in self.positions.values():
            if pos.is_open and pos.market_addr:
                is_long = pos.side == "LONG"
                open_keys.add((pos.market_addr.lower(), is_long))

        result = []
        for (market, is_long), events in groups.items():
            if (market, is_long) in open_keys:
                continue
            sym = market_to_sym.get(market)
            if not sym:
                continue

            def _net(e):
                return e.get("net_pnl_usd", e.get("pnl_usd", 0))

            total_pnl = sum(_net(e) for e in events)
            if abs(total_pnl) < 1:
                continue
            last_ts = max(e.get("timestamp", 0) for e in events)
            result.append({"sym": sym, "pnl": total_pnl, "timestamp": last_ts,
                           "market_address": market, "is_long": is_long})

        result.sort(key=lambda x: x["timestamp"])
        return result

    # ── On-chain trade local storage ──

    def _load_onchain_trades(self) -> List[Dict[str, Any]]:
        """Load locally-stored on-chain trades (with .bak fallback)."""
        return safe_json_read(ONCHAIN_TRADES_FILE, default=[])

    def _save_onchain_trades(self, trades: List[Dict[str, Any]]):
        """Persist on-chain trades to disk (atomic write with backup)."""
        try:
            atomic_json_write(ONCHAIN_TRADES_FILE, trades)
        except Exception as e:
            logger.warning(f"Failed to save onchain trades: {e}")

    async def _fetch_and_store_trades(self) -> List[Dict[str, Any]]:
        """Fetch on-chain trades, merge with local store, save, and return all.

        Fresh RPC data covers the last 30 days. Local store keeps everything
        older than that so trades are never lost.
        """
        from history import fetch_trade_history

        # Fetch fresh on-chain trades across all wallets
        fresh = []
        try:
            for wid, acct in self._all_wallets():
                trades = await asyncio.to_thread(
                    fetch_trade_history, self.w3, acct.address
                )
                fresh.extend(trades)
        except Exception as e:
            self.logger.warning(f"On-chain trade fetch failed: {e}")

        # Load existing local store
        stored = self._load_onchain_trades()

        # Merge: use tx_hash:log_index as unique key.
        # Fresh data (with log_index) replaces old stored data (without log_index).
        by_key = {}
        stored_tx_only = set()  # track old-format entries to remove when fresh arrives
        for t in stored:
            tx = t.get("tx_hash", "")
            li = t.get("log_index")
            if tx and li is not None:
                by_key[f"{tx}:{li}"] = t
            elif tx:
                by_key[tx] = t
                stored_tx_only.add(tx)
        for t in fresh:
            tx = t.get("tx_hash", "")
            li = t.get("log_index", 0)
            if tx:
                # Remove old-format entry if fresh has log_index
                if tx in stored_tx_only and tx in by_key:
                    del by_key[tx]
                    stored_tx_only.discard(tx)
                by_key[f"{tx}:{li}"] = t

        merged = sorted(by_key.values(), key=lambda x: x.get("timestamp", 0))

        # Filter out trades before TRADE_START_DATE
        from datetime import datetime as _dt
        try:
            start_ts = int(_dt.strptime(TRADE_START_DATE, "%Y-%m-%d").timestamp())
            merged = [t for t in merged if t.get("timestamp", 0) >= start_ts]
        except Exception:
            pass

        self._save_onchain_trades(merged)
        self.logger.info(f"On-chain trades: {len(fresh)} fetched, {len(merged)} total stored")
        return merged

    async def cmd_pnl(self, chat_id: int):
        """Telegram /pnl command handler.

        Queries on-chain event logs for trade history across all wallets
        (combined), plus current open position unrealized PnL from chain.

        Shows:
          - Today: realized + unrealized + combined total
          - 30 Days / All Time: realized with win rate
        """
        PNL_SYMBOLS = set(self.cfg.markets.keys()) if self.cfg.markets else {"BTC", "SOL", "ETH"}
        ET = ZoneInfo("America/New_York")
        now = datetime.now(ET)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_cutoff = int(today_start.timestamp())

        # Build reverse map: market_address (lower) → symbol
        market_to_sym = {}
        for sym, addr in self.cfg.markets.items():
            if sym in PNL_SYMBOLS:
                market_to_sym[addr.lower()] = sym

        # ── Fetch on-chain trades, grouped by position (market + direction) ──
        all_trades = await self._fetch_and_store_trades()
        grouped = self._group_onchain_trades(all_trades, market_to_sym)

        def bucket_stats(trades_list):
            if not trades_list:
                return {"pnl": 0.0, "trades": 0, "wins": 0}
            pnl = sum(t["pnl"] for t in trades_list)
            wins = sum(1 for t in trades_list if t["pnl"] > 0)
            return {"pnl": pnl, "trades": len(trades_list), "wins": wins}

        now_ts = int(time.time())
        month_cutoff = now_ts - 30 * 86400

        today_stats = {sym: bucket_stats([t for t in grouped if t["sym"] == sym and t["timestamp"] >= today_cutoff]) for sym in PNL_SYMBOLS}
        month_stats = {sym: bucket_stats([t for t in grouped if t["sym"] == sym and t["timestamp"] >= month_cutoff]) for sym in PNL_SYMBOLS}
        alltime_stats = {sym: bucket_stats([t for t in grouped if t["sym"] == sym]) for sym in PNL_SYMBOLS}

        # ── Open positions: unrealized from chain ──
        open_unrealized = {sym: 0.0 for sym in PNL_SYMBOLS}
        try:
            for wid, acct in self._all_wallets():
                cps = await asyncio.to_thread(chain_fetch_positions, self.w3, acct.address)
                for cp in cps:
                    sym = cp.symbol.upper().split("/")[0]
                    if sym in PNL_SYMBOLS:
                        open_unrealized[sym] += cp.unrealized_pnl
        except Exception as e:
            self.logger.warning(f"/pnl: could not fetch chain positions: {e}")

        # ── Format helpers ──
        def _sign(v):
            return "+" if v >= 0 else ""

        def format_section(label, symbol_stats):
            lines = [f"**{label}**"]
            total_pnl = 0.0
            total_trades = 0
            total_wins = 0
            for sym in ("BTC", "ETH", "SOL"):
                s = symbol_stats.get(sym, {"pnl": 0.0, "trades": 0, "wins": 0})
                wr = f"{s['wins']}/{s['trades']}" if s["trades"] else "—"
                lines.append(f"  {sym}: {_sign(s['pnl'])}${s['pnl']:,.2f}  ({wr})")
                total_pnl += s["pnl"]
                total_trades += s["trades"]
                total_wins += s["wins"]
            wr_total = f"{total_wins}/{total_trades}" if total_trades else "—"
            winrate = f"{total_wins / total_trades * 100:.0f}%" if total_trades else "—"
            lines.append(f"  Total: {_sign(total_pnl)}${total_pnl:,.2f}  ({wr_total} | {winrate} WR)")
            return "\n".join(lines)

        # ── Build Today section with realized + unrealized ──
        today_realized = sum(s["pnl"] for s in today_stats.values())
        today_unrealized = sum(open_unrealized.values())
        today_combined = today_realized + today_unrealized
        today_trades = sum(s["trades"] for s in today_stats.values())
        today_wins = sum(s["wins"] for s in today_stats.values())
        today_wr = f"{today_wins / today_trades * 100:.0f}%" if today_trades else "—"

        today_lines = ["**Today**"]
        for sym in ("BTC", "ETH", "SOL"):
            s = today_stats.get(sym, {"pnl": 0.0, "trades": 0, "wins": 0})
            unr = open_unrealized.get(sym, 0.0)
            wr = f"{s['wins']}/{s['trades']}" if s["trades"] else "—"
            line = f"  {sym}: {_sign(s['pnl'])}${s['pnl']:,.2f}  ({wr})"
            if unr != 0:
                line += f"  |  open: {_sign(unr)}${unr:,.2f}"
            today_lines.append(line)
        today_lines.append(f"  Realized:   {_sign(today_realized)}${today_realized:,.2f}  ({today_wins}/{today_trades} | {today_wr} WR)")
        today_lines.append(f"  Unrealized: {_sign(today_unrealized)}${today_unrealized:,.2f}")
        today_lines.append(f"  **Total:    {_sign(today_combined)}${today_combined:,.2f}**")

        # ── Build message ──
        msg = "**PnL Summary — BTC / ETH / SOL**\n\n"
        msg += "\n".join(today_lines)
        msg += "\n\n" + format_section("30 Days", month_stats)
        msg += "\n\n" + format_section("All Time", alltime_stats)

        # ── Bitunix platform breakdown (from internal trade history) ──
        bx_trades = [t for t in self.trade_history if getattr(t, 'exchange', 'gmx') == 'bitunix' and abs(t.pnl_usd) >= 1]
        bx_open = [p for p in self.positions.values() if p.is_open and getattr(p, 'exchange', 'gmx') == 'bitunix']
        if bx_trades or bx_open:
            bx_today = [t for t in bx_trades if t.closed_at >= today_cutoff]
            bx_month = [t for t in bx_trades if t.closed_at >= month_cutoff]
            bx_upnl = sum(p.unrealized_pnl or 0 for p in bx_open)

            def _bx_bucket(trades_list):
                if not trades_list:
                    return {"pnl": 0.0, "trades": 0, "wins": 0}
                pnl = sum(t.pnl_usd for t in trades_list)
                wins = sum(1 for t in trades_list if t.pnl_usd > 0)
                return {"pnl": pnl, "trades": len(trades_list), "wins": wins}

            def _bx_line(label, stats, unrealized=None):
                wr = f"{stats['wins']}/{stats['trades']}" if stats["trades"] else "—"
                wr_pct = f" | {stats['wins']/stats['trades']*100:.0f}% WR" if stats["trades"] else ""
                line = f"  {label}: {_sign(stats['pnl'])}${stats['pnl']:,.2f}  ({wr}{wr_pct})"
                if unrealized is not None and unrealized != 0:
                    line += f"  |  open: {_sign(unrealized)}${unrealized:,.2f}"
                return line

            msg += "\n\n**Bitunix PnL**"
            if bx_open:
                msg += f" ({len(bx_open)} open)"
            msg += "\n"
            msg += _bx_line("Today", _bx_bucket(bx_today), bx_upnl) + "\n"
            msg += _bx_line("30 Days", _bx_bucket(bx_month)) + "\n"
            msg += _bx_line("All Time", _bx_bucket(bx_trades))

        await self.send_message(chat_id, msg)

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /health
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_health(self, chat_id: int):
        """Telegram /health command handler.

        Shows system health status: uptime, heartbeat, positions, stats.
        """
        h = self.get_health_report()
        uptime_hours = h["uptime_seconds"] / 3600 if h["uptime_seconds"] else 0
        heartbeat_age = time.time() - self.last_heartbeat

        msg = (
            "**System Health**\n\n"
            f"Status: {'HALTED' if h['is_halted'] else 'ACTIVE'}\n"
            f"Uptime: {uptime_hours:.1f}h\n"
            f"Heartbeat: {heartbeat_age:.0f}s ago\n"
            f"Positions: {h['open_positions']}/{h['total_positions']}\n"
            f"Price updates: {h['price_updates']}\n"
            f"Signals: {h['signals_processed']}\n"
            f"Trades: {h['trades_executed']}\n"
            f"Errors: {h['errors']}"
        )
        await self.send_message(chat_id, msg)

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /pdf
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_pdf(self, chat_id: int):
        """Generate and send a PDF with PnL summary + full trade history."""
        await self.send_message(chat_id, "Fetching on-chain trades & generating PDF...")

        # Build market_address → symbol map
        PNL_SYMBOLS = set(self.cfg.markets.keys()) if self.cfg.markets else {"BTC", "SOL", "ETH"}
        market_to_sym = {}
        for sym, addr in self.cfg.markets.items():
            if sym in PNL_SYMBOLS:
                market_to_sym[addr.lower()] = sym

        # Fetch on-chain trades (merged with local store)
        on_chain = await self._fetch_and_store_trades()

        if not on_chain and not self.trade_history:
            await self.send_message(chat_id, "No trades to export.")
            return

        try:
            pdf_path = await asyncio.to_thread(
                self._generate_trade_pdf, on_chain, market_to_sym
            )
            bot_api_chats = getattr(self, '_bot_api_chats', set())
            if chat_id in bot_api_chats:
                import bot_api
                await bot_api.send_admin_pdf(
                    self.cfg.telegram_bot_token, str(chat_id), pdf_path,
                    caption="Trade History Report",
                )
            else:
                await self.client.send_file(chat_id, pdf_path, caption="Trade History Report")
            os.remove(pdf_path)
        except Exception as e:
            self.logger.error(f"PDF generation failed: {e}")
            await self.send_message(chat_id, f"PDF generation failed: {e}")

    def _generate_trade_pdf(self, on_chain_trades: list, market_to_sym: dict) -> str:
        """Build the PDF file with closed trade PnL summary + trade list."""
        ET = ZoneInfo("America/New_York")

        # ── Use only fully-closed positions (trade_history), not partial TP hits ──
        unified = []
        for t in self.trade_history:
            unified.append({
                "symbol": t.symbol,
                "side": t.side,
                "size_usd": t.size_usd,
                "pnl_usd": t.pnl_usd,
                "timestamp": t.closed_at,
                "tx_hash": getattr(t, "tx_hash", "") or getattr(t, "id", ""),
                "source": "local",
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "leverage": t.leverage,
                "exit_reason": t.exit_reason,
                "pnl_percentage": t.pnl_percentage,
                "exchange": getattr(t, "exchange", "gmx"),
            })

        # Exclude dust trades (< $1 PnL) and sort newest first
        unified = [t for t in unified if abs(t["pnl_usd"]) >= 1 and t.get("exchange", "gmx") == "gmx"]
        unified.sort(key=lambda x: x["timestamp"], reverse=True)

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        # ── Title ──
        pdf.set_font("Helvetica", "B", 18)
        pdf.cell(0, 12, "Closed Trades", new_x="LMARGIN", new_y="NEXT", align="C")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(
            0, 6,
            f"Generated: {datetime.now(ET).strftime('%b %d, %Y %I:%M %p ET')}",
            new_x="LMARGIN", new_y="NEXT", align="C",
        )
        pdf.ln(6)

        # ── Summary ──
        total_pnl = sum(t["pnl_usd"] for t in unified)
        wins = [t for t in unified if t["pnl_usd"] > 0]
        losses = [t for t in unified if t["pnl_usd"] < 0]
        win_rate = (len(wins) / len(unified) * 100) if unified else 0
        avg_win = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0

        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Summary", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)

        pnl_sign = "+" if total_pnl >= 0 else ""
        summary_lines = [
            f"Total Trades: {len(unified)}",
            f"Win Rate: {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)",
            f"Net PnL: {pnl_sign}${total_pnl:,.2f}",
            f"Avg Win: +${avg_win:,.2f}" if avg_win >= 0 else f"Avg Win: ${avg_win:,.2f}",
            f"Avg Loss: ${avg_loss:,.2f}",
        ]
        for line in summary_lines:
            pdf.cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")

        pdf.ln(4)
        pdf.set_draw_color(200, 200, 200)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(4)

        # ── PnL Breakdown by Symbol (Today / 30d / All Time) ──
        now = datetime.now(ET)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_cutoff = int(today_start.timestamp())
        now_ts = int(time.time())
        month_cutoff = now_ts - 30 * 86400

        def _net(t):
            return t.get("pnl_usd", 0)

        def _bucket(trades_list):
            if not trades_list:
                return 0.0, 0, 0
            pnl = sum(_net(t) for t in trades_list)
            w = sum(1 for t in trades_list if _net(t) > 0)
            return pnl, len(trades_list), w

        def _s(v):
            return "+" if v >= 0 else ""

        for label, cutoff in [("Today", today_cutoff), ("30 Days", month_cutoff), ("All Time", 0)]:
            bucket = [t for t in unified if t["timestamp"] >= cutoff]
            if not bucket and label != "Today":
                continue
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 7, label, new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 9)
            t_pnl, t_trades, t_wins = 0.0, 0, 0
            for sym in ("BTC", "ETH", "SOL"):
                sym_trades = [t for t in bucket if t["symbol"] == sym]
                pnl_s, cnt, w = _bucket(sym_trades)
                t_pnl += pnl_s
                t_trades += cnt
                t_wins += w
                wr = f"{w}/{cnt}" if cnt else "-"
                pdf.cell(0, 5, f"  {sym}: {_s(pnl_s)}${pnl_s:,.2f}  ({wr})", new_x="LMARGIN", new_y="NEXT")
            wr_pct = f"{t_wins / t_trades * 100:.0f}%" if t_trades else "-"
            pdf.cell(0, 5, f"  Total: {_s(t_pnl)}${t_pnl:,.2f}  ({t_wins}/{t_trades} | {wr_pct} WR)", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

        pdf.set_draw_color(200, 200, 200)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(4)

        # ── Trade List (GMX only, newest first) ──
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, f"Trades ({len(unified)})", new_x="LMARGIN", new_y="NEXT")

        for i, t in enumerate(unified, 1):
            ts = t["timestamp"]
            if ts:
                trade_dt = datetime.fromtimestamp(ts, tz=ET)
                date_str = trade_dt.strftime("%b %d, %Y %I:%M %p")
            else:
                date_str = "Unknown"

            pnl = t["pnl_usd"]
            pnl_sign = "+" if pnl >= 0 else ""

            if pnl >= 0:
                pdf.set_text_color(0, 128, 0)
            else:
                pdf.set_text_color(200, 0, 0)

            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 6, f"#{i}  {t['symbol']} {t['side']}", new_x="LMARGIN", new_y="NEXT")

            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 9)

            if t.get("entry_price") and t.get("exit_price"):
                pdf.cell(0, 5, f"  Entry: ${t['entry_price']:,.2f}  |  Exit: ${t['exit_price']:,.2f}", new_x="LMARGIN", new_y="NEXT")
                lev = t.get("leverage", 0)
                pct = t.get("pnl_percentage", 0)
                pct_sign = "+" if pct >= 0 else ""
                pdf.cell(
                    0, 5,
                    f"  Size: ${t['size_usd']:,.2f} @ {lev:.0f}x  |  "
                    f"PnL: {pnl_sign}${pnl:,.2f} ({pct_sign}{pct:.1f}%)",
                    new_x="LMARGIN", new_y="NEXT",
                )
            else:
                pdf.cell(
                    0, 5,
                    f"  Size: ${t['size_usd']:,.2f}  |  PnL: {pnl_sign}${pnl:,.2f}",
                    new_x="LMARGIN", new_y="NEXT",
                )

            pdf.cell(0, 5, f"  Date: {date_str}", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, prefix="gmx_trades_")
        pdf.output(tmp.name)
        return tmp.name
