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
from dataclasses import dataclass, asdict, fields
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fpdf import FPDF

from close import fetch_positions as chain_fetch_positions
from history import fetch_recent_position_decreases
from risk import calculate_unrealized_pnl, calculate_pnl_percentage
from state_io import atomic_json_write, safe_json_read


logger = logging.getLogger("GMXBot.analytics")

TRADE_HISTORY_FILE = "json/trade_history.json"
ONCHAIN_TRADES_FILE = "json/onchain_trades.json"

# Only show trades from this date forward (UTC midnight).
# Change this date to reset your trade history starting point.
TRADE_START_DATE = "2026-03-09T01:00"


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
    tp_hits: int = 0  # number of TP orders hit before close
    tp_details: list = None  # list of {"price": float, "pct": float, "pnl": float}
    sl_details: dict = None  # {"price": float, "pct": float, "pnl": float}
    unfilled_targets: list = None  # list of {"price": float} — TP orders that never hit


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
        trades = list(self.trade_history)

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
        trades = list(self.trade_history)
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
        """Rebuild trade history using centralized trade_rebuilder.

        Delegates to rebuild_all_trades() which queries on-chain RPC + Bitunix API,
        overwrites onchain_trades.json and trade_history.json with fresh data.
        """
        from trade_rebuilder import rebuild_all_trades

        self.trade_history = await rebuild_all_trades(
            self.w3,
            self._all_wallets(),
            self.cfg.markets,
            bitunix_client=getattr(self, 'bitunix_client', None),
            open_positions=self.positions,
        )
        self.logger.info(f"Trade rebuild: {len(self.trade_history)} total trade(s)")

    async def cmd_performance(self, chat_id: int):
        """Send platform performance comparison to admin."""
        msg = self.get_platform_comparison()
        await self.send_message(chat_id, msg)

    async def calculate_win_rate_onchain(self, symbol: str = None, n: int = None) -> Dict[str, Any]:
        """Calculate win rate from trade history (fee-inclusive PnL).

        Uses rebuild_all_trades() for fresh data from on-chain + Bitunix API.

        Args:
            symbol: Filter trades by symbol (e.g., 'BTC'). None = all symbols.
            n: Limit to last N trades. None = all trades.

        Returns:
            Dict with keys: win_rate, wins, losses, total, avg_win, avg_loss, pnl
        """
        from trade_rebuilder import rebuild_all_trades

        all_trades = await rebuild_all_trades(
            self.w3, self._all_wallets(), self.cfg.markets,
            bitunix_client=getattr(self, 'bitunix_client', None),
            open_positions=self.positions,
        )
        self.trade_history = all_trades

        trades = list(all_trades)
        if symbol:
            trades = [t for t in trades if t.symbol == symbol.upper()]
        if n and n > 0:
            trades = trades[-n:]

        if not trades:
            return {"win_rate": 0, "wins": 0, "losses": 0, "total": 0, "avg_win": 0, "avg_loss": 0, "pnl": 0}

        wins = [t for t in trades if t.pnl_usd > 0]
        losses = [t for t in trades if t.pnl_usd < 0]
        total_pnl = sum(t.pnl_usd for t in trades)

        return {
            "win_rate": len(wins) / len(trades) * 100,
            "wins": len(wins),
            "losses": len(losses),
            "total": len(trades),
            "avg_win": sum(t.pnl_usd for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t.pnl_usd for t in losses) / len(losses) if losses else 0,
            "pnl": total_pnl,
        }

    async def _record_trade(self, pos_obj, exit_reason: str = "manual"):
        """Record a closed position as a trade in history.

        Acquires _rebuild_lock to prevent race conditions with rebuild_all_trades().

        Fetches actual exit price and PnL from on-chain PositionDecrease
        events when available, falling back to internal data if the RPC
        call fails or no events are found.

        Args:
            pos_obj: Position object with id, symbol, side, entry_price, size_usd, leverage, etc.
            exit_reason: Why the position closed ('manual', 'tp_hit', 'sl_triggered', 'liquidation_or_manual', 'override')
        """
        from trade_rebuilder import _rebuild_lock
        async with _rebuild_lock:
            await self._record_trade_inner(pos_obj, exit_reason)

    async def _record_trade_inner(self, pos_obj, exit_reason: str = "manual"):
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
            # GMX V2: execution_price = entry price; derive actual fill from basePnl
            ld_ep = last_decrease.get("execution_price", 0)
            ld_sd = last_decrease.get("size_delta_usd", 0)
            ld_bp = last_decrease.get("pnl_usd", 0)
            if ld_ep > 0 and ld_sd > 0:
                ratio = ld_bp / ld_sd
                exit_price = ld_ep * (1 + ratio) if pos_obj.side == "LONG" else ld_ep * (1 - ratio)
            elif ld_ep > 0:
                exit_price = ld_ep
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

        # Count TP hits from verified_decreases (order_type 5 = limit/TP)
        # For backwards compat: if order_type is None but matched_tp_price exists, treat as TP
        def _is_tp(d):
            ot = d.get("order_type")
            if ot == 5:
                return True
            if ot is None and d.get("matched_tp_price"):
                return True
            return False
        tp_hit_count = sum(1 for d in verified if _is_tp(d)) if verified else 0
        # Derive fill prices from basePnl (GMX V2: execution_price = entry price)
        def _fill_price_live(entry, base_pnl, size_delta, long):
            if entry <= 0 or size_delta <= 0:
                return entry
            ratio = base_pnl / size_delta
            return entry * (1 + ratio) if long else entry * (1 - ratio)

        is_long = pos_obj.side == "LONG"
        live_entry = pos_obj.entry_price

        tp_details = []
        if verified:
            for d in verified:
                if _is_tp(d):
                    tp_size = d.get("size_delta_usd", 0)
                    tp_base = d.get("pnl_usd", d.get("net_pnl_usd", 0))
                    tp_fill = _fill_price_live(live_entry, tp_base, tp_size, is_long)
                    tp_pnl = d.get("net_pnl_usd", tp_base)
                    pct_closed = (tp_size / full_size * 100) if full_size > 0 else 0
                    tp_details.append({"price": tp_fill, "pct": pct_closed, "pnl": tp_pnl})

        # SL details: derive actual fill price from basePnl
        sl_details = None
        if verified:
            sl_events = [d for d in verified if not _is_tp(d) and d.get("size_delta_usd", 0) > 0]
            if sl_events:
                sl_ev = sl_events[-1]
                sl_fill = _fill_price_live(live_entry, sl_ev.get("pnl_usd", 0), sl_ev.get("size_delta_usd", 0), is_long)
                sl_size = sum(d.get("size_delta_usd", 0) for d in sl_events)
                sl_pnl = sum(d.get("net_pnl_usd", d.get("pnl_usd", 0)) for d in sl_events)
                sl_pct = (sl_size / full_size * 100) if full_size > 0 else 0
                sl_details = {"price": sl_fill, "pct": sl_pct, "pnl": sl_pnl}

        # Detect unfilled targets: compare position's TP levels vs filled TPs
        unfilled_targets = None
        all_tp_levels = getattr(pos_obj, 'take_profits', None) or []
        if all_tp_levels:
            filled_prices = set()
            for d in verified:
                if _is_tp(d):
                    tp_base = d.get("pnl_usd", d.get("net_pnl_usd", 0))
                    tp_fill = _fill_price_live(live_entry, tp_base, d.get("size_delta_usd", 0), is_long)
                    filled_prices.add(round(tp_fill, 2))
                    if d.get("trigger_price"):
                        filled_prices.add(round(d["trigger_price"], 2))
                    if d.get("matched_tp_price"):
                        filled_prices.add(round(d["matched_tp_price"], 2))
            unfilled = []
            for tp_level in all_tp_levels:
                if round(tp_level.price, 2) not in filled_prices:
                    unfilled.append({"price": tp_level.price})
            if unfilled:
                if is_long:
                    unfilled.sort(key=lambda x: x["price"])
                else:
                    unfilled.sort(key=lambda x: x["price"], reverse=True)
                unfilled_targets = unfilled

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
            tp_hits=tp_hit_count,
            tp_details=tp_details,
            sl_details=sl_details,
            unfilled_targets=unfilled_targets,
        )
        self.trade_history.append(trade)
        self._save_trade_history()
        self.logger.info(
            f"Trade recorded: {trade.symbol} {trade.side} [{trade.exchange.upper()}] "
            f"PnL=${trade.pnl_usd:,.2f} ({trade.pnl_percentage:+.1f}%) [{exit_reason}]"
        )

        # Save balance snapshot on trade close for chart accuracy
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._snapshot_after_trade())
        except RuntimeError:
            pass

    async def _snapshot_after_trade(self):
        """Save a balance snapshot after a trade closes."""
        try:
            total = await self._get_total_portfolio_value()
            self._save_balance_snapshot(total)
        except Exception as e:
            self.logger.debug(f"Post-trade snapshot failed: {e}")

    def _save_trade_history(self):
        """Persist trade history to disk as JSON (atomic write with backup)."""
        try:
            data = [asdict(t) for t in self.trade_history]
            atomic_json_write(TRADE_HISTORY_FILE, data)
        except Exception as e:
            logger.error(f"CRITICAL: Failed to save trade history: {e}", exc_info=True)

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
            valid_fields = {f.name for f in fields(TradeRecord)}
            for record in data:
                try:
                    filtered = {k: v for k, v in record.items() if k in valid_fields}
                    self.trade_history.append(TradeRecord(**filtered))
                except Exception as e:
                    skipped += 1
                    logger.warning(
                        f"Skipping corrupt trade record "
                        f"(id={record.get('id','?')}, exchange={record.get('exchange','?')}): {e}"
                    )
            gmx_count = sum(1 for t in self.trade_history if t.exchange == 'gmx')
            bx_count = sum(1 for t in self.trade_history if t.exchange == 'bitunix')
            if skipped:
                logger.warning(f"Skipped {skipped} corrupt trade record(s)")
            logger.info(
                f"Loaded {len(self.trade_history)} trade(s) from {TRADE_HISTORY_FILE} "
                f"({gmx_count} GMX, {bx_count} Bitunix)"
            )

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
        from trade_rebuilder import rebuild_all_trades
        try:
            self.trade_history = await rebuild_all_trades(
                self.w3, self._all_wallets(), self.cfg.markets,
                bitunix_client=getattr(self, 'bitunix_client', None),
                open_positions=self.positions,
            )
        except Exception as e:
            self.logger.warning(f"Winrate rebuild failed: {e}")

        trades = list(self.trade_history)

        if symbol:
            trades = [t for t in trades if t.symbol == symbol.upper()]

        if n and n > 0:
            trades = trades[-n:]

        if not trades:
            label = f" for {symbol}" if symbol else ""
            await self.send_message(chat_id, f"No closed trades{label} yet.")
            return

        wins = [t for t in trades if t.pnl_usd > 0]
        losses = [t for t in trades if t.pnl_usd <= 0]
        total_pnl = sum(t.pnl_usd for t in trades)
        win_rate = len(wins) / len(trades) * 100
        avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0

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

        # Exclude currently-open positions (by market, direction, AND opened_at)
        open_keys = set()
        for pos in self.positions.values():
            if pos.is_open and pos.market_addr:
                is_long = pos.side == "LONG"
                opened_at = int(getattr(pos, 'opened_at', 0) or 0)
                open_keys.add((pos.market_addr.lower(), is_long, opened_at))

        result = []
        for (market, is_long), events in groups.items():
            sym = market_to_sym.get(market)
            if not sym:
                continue

            def _net(e):
                return e.get("net_pnl_usd", e.get("pnl_usd", 0))

            # Split by opened_at to check per-position
            _pos_groups = defaultdict(list)
            for e in events:
                _pos_groups[e.get("opened_at", 0)].append(e)

            filtered_events = []
            for oa, evts in _pos_groups.items():
                if any(m == market and il == is_long and abs(o - oa) < 60
                       for m, il, o in open_keys):
                    continue
                filtered_events.extend(evts)

            if not filtered_events:
                continue
            events = filtered_events

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

    # _fetch_bitunix_trade_history and _fetch_and_store_trades moved to trade_rebuilder.py

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

        # ── Fetch all trades via centralized rebuilder ──
        from trade_rebuilder import rebuild_all_trades
        all_trades = await rebuild_all_trades(
            self.w3, self._all_wallets(), self.cfg.markets,
            bitunix_client=getattr(self, 'bitunix_client', None),
            open_positions=self.positions,
        )
        self.trade_history = all_trades

        # Convert to simple dicts for bucketing
        grouped = []
        for t in all_trades:
            if abs(t.pnl_usd) < 1:
                continue
            grouped.append({"sym": t.symbol, "pnl": t.pnl_usd, "timestamp": t.closed_at})

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
            for sym in ("BTC", "ETH"):
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
        for sym in ("BTC", "ETH"):
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
        bx_trades = [t for t in self.trade_history if getattr(t, 'exchange', 'gmx') == 'bitunix']
        bx_open = [p for p in self.positions.values() if p.is_open and getattr(p, 'exchange', 'gmx') == 'bitunix']
        if bx_trades or bx_open:
            bx_today = [t for t in bx_trades if t.closed_at >= today_cutoff]
            bx_month = [t for t in bx_trades if t.closed_at >= month_cutoff]
            bx_upnl = sum(p.unrealized_pnl or 0 for p in bx_open)
            # Include realized PnL from partial TP hits on still-open positions
            bx_open_realized = sum(p.realized_pnl or 0 for p in bx_open if p.realized_pnl)

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
            if bx_open_realized:
                msg += f"\n  Open TP realized: {_sign(bx_open_realized)}${bx_open_realized:,.2f}"

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
        """Generate and send a PDF with PnL summary + full trade history.

        Uses centralized trade_rebuilder for fresh data from on-chain + Bitunix API.
        """
        await self.send_message(chat_id, "Fetching trades from chain & exchange API...")

        from trade_rebuilder import rebuild_all_trades
        trades = await rebuild_all_trades(
            self.w3, self._all_wallets(), self.cfg.markets,
            bitunix_client=getattr(self, 'bitunix_client', None),
            open_positions=self.positions,
        )
        self.trade_history = trades

        if not trades:
            await self.send_message(chat_id, "No trades to export.")
            return

        try:
            pdf_path = await asyncio.to_thread(
                self._generate_trade_pdf, trades
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

    def _generate_trade_pdf(self, trades: list) -> str:
        """Build the PDF file with 3-column PnL summary + 3-column trade grid.

        Args:
            trades: list of TradeRecord objects (from rebuild_all_trades)
        """
        ET = ZoneInfo("America/New_York")
        LMARGIN = 10
        PAGE_W = 210  # A4 width in mm
        RMARGIN = 10
        USABLE_W = PAGE_W - LMARGIN - RMARGIN  # 190mm
        COL_W = USABLE_W / 3  # ~63.3mm per column
        LINE_H = 5

        # ── Build unified trade list from TradeRecords ──
        unified = []
        for t in trades:
            unified.append({
                "symbol": t.symbol,
                "side": t.side,
                "size_usd": t.size_usd,
                "pnl_usd": t.pnl_usd,
                "timestamp": t.closed_at,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "leverage": t.leverage,
                "pnl_percentage": t.pnl_percentage,
                "exchange": getattr(t, "exchange", "gmx"),
                "tp_hits": getattr(t, "tp_hits", 0),
                "tp_details": getattr(t, "tp_details", None) or [],
                "sl_details": getattr(t, "sl_details", None),
                "unfilled_targets": getattr(t, "unfilled_targets", None) or [],
                "duration_hours": t.duration_hours,
                "opened_at": t.opened_at,
            })

        unified.sort(key=lambda x: x["timestamp"], reverse=True)

        # ── Time buckets ──
        now = datetime.now(ET)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_cutoff = int(today_start.timestamp())
        now_ts = int(time.time())
        month_cutoff = now_ts - 30 * 86400

        def _bucket(trades_list):
            if not trades_list:
                return {"pnl": 0.0, "cnt": 0, "wins": 0}
            pnl = sum(t["pnl_usd"] for t in trades_list)
            w = sum(1 for t in trades_list if t["pnl_usd"] > 0)
            return {"pnl": pnl, "cnt": len(trades_list), "wins": w}

        def _s(v):
            return "+" if v >= 0 else "-"

        buckets = []
        for label, cutoff in [("Today", today_cutoff), ("30 Days", month_cutoff), ("All Time", 0)]:
            b = [t for t in unified if t["timestamp"] >= cutoff]
            stats = _bucket(b)
            sym_stats = {}
            for sym in ("BTC", "ETH"):
                sym_stats[sym] = _bucket([t for t in b if t["symbol"] == sym])
            losses = stats["cnt"] - stats["wins"]
            wr = f"{stats['wins'] / stats['cnt'] * 100:.0f}%" if stats["cnt"] else "-"
            buckets.append({"label": label, "stats": stats, "sym": sym_stats,
                            "wr": wr, "losses": losses})

        # ── PDF setup ──
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        # ── Title ──
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, "Trade Report", new_x="LMARGIN", new_y="NEXT", align="C")
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, f"Generated: {now.strftime('%b %d, %Y %I:%M %p ET')}",
                 new_x="LMARGIN", new_y="NEXT", align="C")
        pdf.ln(4)

        # ── 3-Column Summary: Today | 30 Days | All Time ──
        top_y = pdf.get_y()
        SUMMARY_H = 6 + LINE_H * 5 + 2  # total height of summary box
        SGAP = 3  # gap between summary columns

        for col_idx, bk in enumerate(buckets):
            x = LMARGIN + col_idx * COL_W + (SGAP / 2)
            box_w = COL_W - SGAP
            y = top_y
            s = bk["stats"]
            pnl_sign = _s(s["pnl"])

            # Light background box
            pdf.set_fill_color(245, 245, 248)
            pdf.set_draw_color(220, 220, 220)
            pdf.rect(x, y, box_w, SUMMARY_H, style="DF")

            cx = x + 2  # inner padding

            # Column header
            pdf.set_xy(cx, y + 1)
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(30, 30, 30)
            pdf.cell(box_w - 4, 5, bk["label"])
            y += 7

            # Stats
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(50, 50, 50)
            lines = [
                f"Trades: {s['cnt']}  ({s['wins']}W / {bk['losses']}L)",
                f"Win Rate: {bk['wr']}",
                f"Net PnL: {pnl_sign}${abs(s['pnl']):,.2f}",
            ]
            for sym in ("BTC", "ETH"):
                ss = bk["sym"][sym]
                wr = f"{ss['wins']}/{ss['cnt']}" if ss["cnt"] else "-"
                lines.append(f"{sym}: {_s(ss['pnl'])}${abs(ss['pnl']):,.2f}  ({wr})")

            for line in lines:
                pdf.set_xy(cx, y)
                pdf.cell(box_w - 4, LINE_H, line)
                y += LINE_H

        # Move below summary
        pdf.set_text_color(0, 0, 0)
        pdf.set_y(top_y + SUMMARY_H + 5)

        # ── 3-Column Trade Grid (newest to oldest) ──
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 7, f"Trades ({len(unified)})", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

        PAD = 2  # inner padding inside each card
        GAP = 3  # gap between cards (horizontal and vertical)
        CARD_INNER_W = COL_W - GAP  # card width minus gap
        TP_LINE_H = 3.5  # height per TP/SL line
        row_y = pdf.get_y()

        def _card_h(trade):
            """Calculate dynamic card height based on TP/SL/unfilled lines."""
            base = PAD * 2 + 4 * 5  # header + pnl + size + entry + date
            tp_count = len(trade.get("tp_details", []))
            sl_count = 1 if trade.get("sl_details") else 0
            unfilled_count = len(trade.get("unfilled_targets", []))
            return base + TP_LINE_H * (tp_count + sl_count + unfilled_count)

        i = 0
        while i < len(unified):
            # Calculate max card height for this row of up to 3 cards
            row_cards = unified[i:i+3]
            max_h = max(_card_h(t) for t in row_cards)

            if i > 0:
                row_y += max_h + GAP
            if row_y + max_h > pdf.h - 15:
                pdf.add_page()
                row_y = pdf.get_y()

            for col, t in enumerate(row_cards):
                card_h = max_h  # use row max for uniform height

                x = LMARGIN + col * COL_W + (GAP / 2)
                y = row_y

                pnl = t["pnl_usd"]
                pnl_sign = _s(pnl)
                pct = t.get("pnl_percentage", 0)
                lev = t.get("leverage", 0)
                exch = t.get("exchange", "gmx").upper()
                ts = t["timestamp"]
                date_str = datetime.fromtimestamp(ts, tz=ET).strftime("%b %d, %I:%M %p") if ts else "Unknown"
                entry_p = t.get("entry_price", 0)
                exit_p = t.get("exit_price", 0)

                # Card border
                pdf.set_draw_color(220, 220, 220)
                pdf.rect(x, y, CARD_INNER_W, card_h)

                # Color bar on left edge (green=win, red=loss)
                if pnl >= 0:
                    pdf.set_fill_color(0, 160, 0)
                else:
                    pdf.set_fill_color(210, 0, 0)
                pdf.rect(x, y, 1.2, card_h, style="F")

                cx = x + PAD + 1  # content x (after color bar + padding)
                cw = CARD_INNER_W - PAD * 2 - 1  # content width
                cy = y + PAD  # content y

                # Header: #N SYM SIDE EXCHANGE
                pdf.set_xy(cx, cy)
                pdf.set_text_color(30, 30, 30)
                pdf.set_font("Helvetica", "B", 8)
                pdf.cell(cw, 4, f"#{i+col+1} {t['symbol']} {t['side']} {exch}")
                cy += 4

                # PnL with %
                pdf.set_xy(cx, cy)
                if pnl >= 0:
                    pdf.set_text_color(0, 140, 0)
                else:
                    pdf.set_text_color(200, 0, 0)
                pdf.set_font("Helvetica", "B", 7.5)
                if pct != 0:
                    pct_sign = "+" if pct >= 0 else ""
                    pdf.cell(cw, 4, f"PnL: {pnl_sign}${abs(pnl):,.2f} ({pct_sign}{pct:.1f}%)")
                else:
                    pdf.cell(cw, 4, f"PnL: {pnl_sign}${abs(pnl):,.2f}")
                cy += 4

                # Size + Collateral
                pdf.set_text_color(60, 60, 60)
                pdf.set_font("Helvetica", "", 7)
                pdf.set_xy(cx, cy)
                if lev and lev > 0:
                    collateral = t["size_usd"] / lev
                    pdf.cell(cw, 4, f"Size: ${t['size_usd']:,.2f} @ {lev:.0f}x  (${collateral:,.2f})")
                else:
                    pdf.cell(cw, 4, f"Size: ${t['size_usd']:,.2f}")
                cy += 4

                # Entry price
                if entry_p:
                    pdf.set_xy(cx, cy)
                    pdf.cell(cw, 4, f"Entry: ${entry_p:,.2f}")
                    cy += 4

                # Target lines (with green checkmark)
                tp_dets = t.get("tp_details", [])
                for j, tp in enumerate(tp_dets, 1):
                    pdf.set_xy(cx, cy)
                    pdf.set_font("ZapfDingbats", "", 6)
                    pdf.set_text_color(0, 160, 0)
                    pdf.cell(3, TP_LINE_H, "4")
                    pdf.set_font("Helvetica", "", 6.5)
                    pdf.set_text_color(60, 60, 60)
                    p_str = f"Target {j}: ${tp['price']:,.2f}"
                    if "pct" in tp:
                        p_str += f" (closed {tp['pct']:.0f}%)"
                    if "pnl" in tp:
                        tp_pnl = tp["pnl"]
                        p_str += f" {'+' if tp_pnl >= 0 else '-'}${abs(tp_pnl):,.2f}"
                    pdf.cell(cw - 3, TP_LINE_H, p_str)
                    cy += TP_LINE_H

                # Trailing SL line (with green checkmark)
                sl_det = t.get("sl_details")
                if sl_det:
                    pdf.set_xy(cx, cy)
                    pdf.set_font("ZapfDingbats", "", 6)
                    pdf.set_text_color(0, 160, 0)
                    pdf.cell(3, TP_LINE_H, "4")  # green checkmark
                    sl_pnl = sl_det.get("pnl", 0)
                    label = "Trailing SL"
                    pdf.set_font("Helvetica", "", 6.5)
                    pdf.set_text_color(60, 60, 60)
                    s_str = f"{label}: ${sl_det['price']:,.2f}"
                    if sl_det.get("pct") and sl_det["pct"] > 0:
                        s_str += f" (closed {sl_det['pct']:.0f}%)"
                    s_str += f" {'+' if sl_pnl >= 0 else '-'}${abs(sl_pnl):,.2f}"
                    pdf.cell(cw - 3, TP_LINE_H, s_str)
                    cy += TP_LINE_H

                # Unfilled targets (red X)
                unfilled = t.get("unfilled_targets", [])
                tp_count = len(tp_dets)
                for k, uf in enumerate(unfilled, tp_count + 1):
                    pdf.set_xy(cx, cy)
                    pdf.set_font("ZapfDingbats", "", 6)
                    pdf.set_text_color(210, 0, 0)
                    pdf.cell(3, TP_LINE_H, "8")  # red X
                    pdf.set_font("Helvetica", "", 6.5)
                    pdf.set_text_color(150, 150, 150)
                    pdf.cell(cw - 3, TP_LINE_H, f"Target {k}: ${uf['price']:,.2f} (Never Hit)")
                    cy += TP_LINE_H

                # Date + Duration (one line)
                open_ts = t.get("opened_at", 0)
                open_str = datetime.fromtimestamp(open_ts, tz=ET).strftime("%m/%d %I:%M%p") if open_ts and open_ts > 1_000_000_000 else "?"
                close_str = datetime.fromtimestamp(ts, tz=ET).strftime("%m/%d %I:%M%p") if ts and ts > 1_000_000_000 else "?"
                dur_h = t.get("duration_hours", 0)
                if dur_h >= 24:
                    dur_str = f"{dur_h / 24:.1f}d"
                elif dur_h >= 1:
                    dur_str = f"{dur_h:.1f}h"
                else:
                    dur_str = f"{max(dur_h * 60, 1):.0f}m"
                pdf.set_text_color(130, 130, 130)
                date_line = f"{open_str} - {close_str}  ({dur_str})"
                pdf.set_font("Helvetica", "", 5.5)
                pdf.set_xy(cx, cy)
                pdf.cell(cw, 4, date_line, align="C")

            i += len(row_cards)

        pdf.set_text_color(0, 0, 0)
        pdf.ln(4)

        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, prefix="gmx_trades_")
        pdf.output(tmp.name)
        return tmp.name
