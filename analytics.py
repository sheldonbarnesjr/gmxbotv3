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
import statistics
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from close import fetch_positions as chain_fetch_positions
from risk import calculate_unrealized_pnl, calculate_pnl_percentage


logger = logging.getLogger("GMXBot.analytics")

TRADE_HISTORY_FILE = "trade_history.json"


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


class AnalyticsMixin:
    """Mixin providing analytics & reporting methods for GMXBot."""

    def calculate_win_rate(self, symbol: str = None, n: int = None) -> Dict[str, Any]:
        """Calculate win rate statistics.

        Args:
            symbol: Filter trades by symbol (e.g., 'BTC'). None = all symbols.
            n: Limit to last N trades. None = all trades.

        Returns:
            Dict with keys: win_rate, wins, losses, total, avg_win, avg_loss, pnl
        """
        trades = self.trade_history

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

    def _record_trade(self, pos_obj, exit_reason: str = "manual"):
        """Record a closed position as a trade in history.

        Args:
            pos_obj: Position object with id, symbol, side, entry_price, size_usd, leverage, etc.
            exit_reason: Why the position closed ('manual', 'tp_hit', 'sl_triggered', 'liquidation_or_manual', 'override')
        """
        if pos_obj.closed_at is None:
            pos_obj.closed_at = time.time()

        exit_price = pos_obj.current_price if pos_obj.current_price > 0 else pos_obj.entry_price
        duration = pos_obj.duration_hours
        unrealized_pnl = pos_obj.unrealized_pnl or calculate_unrealized_pnl(
            pos_obj.side,
            pos_obj.entry_price,
            exit_price,
            pos_obj.size_usd,
        )
        pnl_pct = calculate_pnl_percentage(unrealized_pnl, pos_obj.size_usd, pos_obj.leverage)

        trade = TradeRecord(
            id=pos_obj.id,
            symbol=pos_obj.symbol,
            side=pos_obj.side,
            entry_price=pos_obj.entry_price,
            exit_price=exit_price,
            size_usd=pos_obj.size_usd,
            leverage=pos_obj.leverage,
            duration_hours=duration,
            pnl_usd=unrealized_pnl,
            pnl_percentage=pnl_pct,
            exit_reason=exit_reason,
            opened_at=pos_obj.opened_at,
            closed_at=pos_obj.closed_at,
        )
        self.trade_history.append(trade)
        self._save_trade_history()
        self.logger.info(
            f"Trade recorded: {trade.symbol} {trade.side} "
            f"PnL=${trade.pnl_usd:,.2f} ({trade.pnl_percentage:+.1f}%) [{exit_reason}]"
        )

    def _save_trade_history(self):
        """Persist trade history to disk as JSON."""
        try:
            data = [asdict(t) for t in self.trade_history]
            with open(TRADE_HISTORY_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save trade history: {e}")

    def _load_trade_history(self):
        """Load trade history from disk on startup."""
        if not os.path.exists(TRADE_HISTORY_FILE):
            return
        try:
            with open(TRADE_HISTORY_FILE, "r") as f:
                data = json.load(f)
            self.trade_history = [
                TradeRecord(**record) for record in data
            ]
            logger.info(f"Loaded {len(self.trade_history)} trade(s) from {TRADE_HISTORY_FILE}")
        except Exception as e:
            logger.warning(f"Failed to load trade history: {e}")

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
        """Telegram /winrate command handler.

        Args:
            chat_id: Telegram chat ID
            symbol: Filter by symbol (e.g., 'BTC'). None = all symbols.
            n: Limit to last N trades. None = all trades.

        Usage:
            /winrate — all-time win rate
            /winrate BTC — BTC only
            /winrate BTC 20 — last 20 BTC trades
        """
        stats = self.calculate_win_rate(symbol, n)
        if not stats or stats.get("total", 0) == 0:
            label = f" for {symbol}" if symbol else ""
            await self.send_message(chat_id, f"No closed trades recorded{label} yet.\n\nTrades are recorded when you use /close to manually close a position.")
            return

        title = "Win Rate"
        if symbol:
            title += f" — {symbol}"
        if n:
            title += f" (last {n})"

        msg = (
            f"**{title}**\n\n"
            f"Win Rate: {stats['win_rate']:.1f}% ({stats['wins']}/{stats['total']})\n"
            f"Net PnL: ${stats['pnl']:,.2f}\n"
            f"Avg Win: ${stats['avg_win']:,.2f}\n"
            f"Avg Loss: ${stats['avg_loss']:,.2f}"
        )
        await self.send_message(chat_id, msg)

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /pnl
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_pnl(self, chat_id: int):
        """Telegram /pnl command handler.

        Shows PnL summary for BTC, ETH, SOL by time periods:
          - Today (24h)
          - 30 Days
          - All Time
          - Open (unrealized)
        """
        PNL_SYMBOLS = {"BTC", "SOL", "ETH"}
        now = time.time()
        today_cutoff = now - 86400
        month_cutoff = now - 30 * 86400

        def pnl_stats(trades):
            if not trades:
                return {"pnl": 0.0, "trades": 0, "wins": 0}
            pnl = sum(t.pnl_usd for t in trades)
            wins = sum(1 for t in trades if t.pnl_usd > 0)
            return {"pnl": pnl, "trades": len(trades), "wins": wins}

        def format_section(label, symbol_stats):
            lines = [f"**{label}**"]
            total = 0.0
            for sym in ("BTC", "ETH", "SOL"):
                s = symbol_stats.get(sym, {"pnl": 0.0, "trades": 0, "wins": 0})
                sign = "+" if s["pnl"] >= 0 else ""
                wr = f"{s['wins']}/{s['trades']}" if s["trades"] else "—"
                lines.append(f"  {sym}: {sign}${s['pnl']:,.2f}  ({wr})")
                total += s["pnl"]
            sign = "+" if total >= 0 else ""
            lines.append(f"  Total: {sign}${total:,.2f}")
            return "\n".join(lines)

        relevant = [t for t in self.trade_history if t.symbol in PNL_SYMBOLS]
        today_stats = {sym: pnl_stats([t for t in relevant if t.symbol == sym and t.closed_at >= today_cutoff]) for sym in PNL_SYMBOLS}
        month_stats = {sym: pnl_stats([t for t in relevant if t.symbol == sym and t.closed_at >= month_cutoff]) for sym in PNL_SYMBOLS}
        alltime_stats = {sym: pnl_stats([t for t in relevant if t.symbol == sym]) for sym in PNL_SYMBOLS}

        open_pnl = {sym: 0.0 for sym in PNL_SYMBOLS}
        try:
            for _, acct in self._all_wallets():
                cps = await asyncio.to_thread(chain_fetch_positions, self.w3, acct.address)
                for cp in cps:
                    sym = cp.symbol.upper().split("/")[0]
                    if sym in PNL_SYMBOLS:
                        open_pnl[sym] = open_pnl.get(sym, 0.0) + cp.unrealized_pnl
        except Exception as e:
            self.logger.warning(f"/pnl: could not fetch chain positions: {e}")

        open_lines = ["**Open (Unrealized)**"]
        open_total = 0.0
        for sym in ("BTC", "ETH", "SOL"):
            pnl = open_pnl.get(sym, 0.0)
            sign = "+" if pnl >= 0 else ""
            open_lines.append(f"  {sym}: {sign}${pnl:,.2f}")
            open_total += pnl
        sign = "+" if open_total >= 0 else ""
        open_lines.append(f"  Total: {sign}${open_total:,.2f}")

        if not relevant:
            closed_section = "No closed trades recorded yet.\nTrades are saved when you use /close."
        else:
            closed_section = (
                format_section("Today (24h)", today_stats) + "\n\n"
                + format_section("30 Days", month_stats) + "\n\n"
                + format_section("All Time", alltime_stats)
            )

        msg = "**PnL Summary — BTC / ETH / SOL**\n\n" + closed_section + "\n\n" + "\n".join(open_lines)
        await self.send_message(chat_id, msg)

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /reset
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_reset(self, chat_id: int):
        """Telegram /reset command handler.

        Clears all trade history and resets PnL/win rate stats.
        """
        count = len(self.trade_history)
        self.trade_history.clear()
        self._save_trade_history()
        self.health_stats["trades_executed"] = 0
        self.health_stats["signals_processed"] = 0
        self.logger.info(f"Trade history reset: cleared {count} trade(s)")
        await self.send_message(
            chat_id,
            f"Trade history cleared ({count} trade records removed).\n"
            "PnL and win rate stats have been reset to zero."
        )

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
