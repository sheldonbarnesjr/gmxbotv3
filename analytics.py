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
from risk import calculate_unrealized_pnl, calculate_pnl_percentage


logger = logging.getLogger("GMXBot.analytics")

TRADE_HISTORY_FILE = "trade_history.json"
ONCHAIN_TRADES_FILE = "onchain_trades.json"


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
            wallet_id=getattr(pos_obj, 'wallet_id', 0),
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
        PNL_SYMBOLS = {"BTC", "SOL", "ETH"}
        market_to_sym = {}
        for sym, addr in self.cfg.markets.items():
            if sym in PNL_SYMBOLS:
                market_to_sym[addr.lower()] = sym

        # Fetch on-chain trades (merged with local store)
        all_stored = await self._fetch_and_store_trades()
        reset_ts = self._get_pnl_reset_ts()
        all_trades = [t for t in all_stored if t.get("timestamp", 0) >= reset_ts] if reset_ts else all_stored

        # Tag with symbol, exclude dust (< $1)
        trades = []
        for t in all_trades:
            if abs(t.get("pnl_usd", 0)) < 1:
                continue
            sym = market_to_sym.get((t.get("market_address") or "").lower())
            if sym:
                trades.append({"pnl_usd": t["pnl_usd"], "sym": sym})

        if symbol:
            trades = [t for t in trades if t["sym"] == symbol.upper()]

        if n and n > 0:
            trades = trades[-n:]

        if not trades:
            label = f" for {symbol}" if symbol else ""
            await self.send_message(chat_id, f"No closed trades{label} yet.")
            return

        wins = [t for t in trades if t["pnl_usd"] > 0]
        losses = [t for t in trades if t["pnl_usd"] < 0]
        total_pnl = sum(t["pnl_usd"] for t in trades)
        win_rate = len(wins) / len(trades) * 100
        avg_win = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0

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

    # ── PnL reset timestamp persistence ──

    PNL_RESET_FILE = "pnl_reset.json"

    def _get_pnl_reset_ts(self) -> int:
        """Load the PnL reset timestamp. Returns 0 if never reset."""
        try:
            with open(self.PNL_RESET_FILE, "r") as f:
                data = json.load(f)
                return int(data.get("reset_ts", 0))
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return 0

    def _set_pnl_reset_ts(self, ts: int):
        """Save the PnL reset timestamp."""
        with open(self.PNL_RESET_FILE, "w") as f:
            json.dump({"reset_ts": ts}, f)

    # ── On-chain trade local storage ──

    def _load_onchain_trades(self) -> List[Dict[str, Any]]:
        """Load locally-stored on-chain trades."""
        if not os.path.exists(ONCHAIN_TRADES_FILE):
            return []
        try:
            with open(ONCHAIN_TRADES_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return []

    def _save_onchain_trades(self, trades: List[Dict[str, Any]]):
        """Persist on-chain trades to disk."""
        try:
            with open(ONCHAIN_TRADES_FILE, "w") as f:
                json.dump(trades, f, indent=2)
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
        PNL_SYMBOLS = {"BTC", "SOL", "ETH"}
        ET = ZoneInfo("America/New_York")
        now = datetime.now(ET)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_cutoff = int(today_start.timestamp())
        reset_ts = self._get_pnl_reset_ts()

        # Build reverse map: market_address (lower) → symbol
        market_to_sym = {}
        for sym, addr in self.cfg.markets.items():
            if sym in PNL_SYMBOLS:
                market_to_sym[addr.lower()] = sym

        # ── Fetch on-chain trades (merged with local store) ──
        all_stored = await self._fetch_and_store_trades()
        # Apply reset timestamp filter
        all_trades = [t for t in all_stored if t.get("timestamp", 0) >= reset_ts] if reset_ts else all_stored

        def bucket_stats(trades_list):
            if not trades_list:
                return {"pnl": 0.0, "trades": 0, "wins": 0}
            pnl = sum(t["pnl_usd"] for t in trades_list)
            wins = sum(1 for t in trades_list if t["pnl_usd"] > 0)
            return {"pnl": pnl, "trades": len(trades_list), "wins": wins}

        # Tag each trade with symbol, exclude dust trades (< $1 PnL)
        tagged = []
        for t in all_trades:
            if abs(t.get("pnl_usd", 0)) < 1:
                continue
            sym = market_to_sym.get((t.get("market_address") or "").lower())
            if sym:
                entry = dict(t)  # copy to avoid mutating stored data
                entry["_sym"] = sym
                tagged.append(entry)

        now_ts = int(time.time())
        month_cutoff = now_ts - 30 * 86400

        today_stats = {sym: bucket_stats([t for t in tagged if t["_sym"] == sym and t["timestamp"] >= today_cutoff]) for sym in PNL_SYMBOLS}
        month_stats = {sym: bucket_stats([t for t in tagged if t["_sym"] == sym and t["timestamp"] >= month_cutoff]) for sym in PNL_SYMBOLS}
        alltime_stats = {sym: bucket_stats([t for t in tagged if t["_sym"] == sym]) for sym in PNL_SYMBOLS}

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
        await self.send_message(chat_id, msg)

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /reset
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_reset(self, chat_id: int):
        """Telegram /reset command handler.

        Saves a reset timestamp so /pnl only queries trades after this point.
        Also clears local trade history and health stats.
        """
        count = len(self.trade_history)
        self.trade_history.clear()
        self._save_trade_history()
        self.health_stats["trades_executed"] = 0
        self.health_stats["signals_processed"] = 0

        # Save reset timestamp — /pnl will only show trades after this
        reset_ts = int(time.time())
        self._set_pnl_reset_ts(reset_ts)

        self.logger.info(f"Trade history reset: cleared {count} trade(s), reset_ts={reset_ts}")
        await self.send_message(
            chat_id,
            f"PnL reset. Only trades from now onward will appear in /pnl.\n"
            f"(Cleared {count} local trade records)"
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

    # ──────────────────────────────────────────────────────────────────────
    # Telegram command: /pdf
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_pdf(self, chat_id: int):
        """Generate and send a PDF of all trade history (on-chain + local)."""
        await self.send_message(chat_id, "Fetching on-chain trades & generating PDF...")

        # Build market_address → symbol map
        PNL_SYMBOLS = {"BTC", "SOL", "ETH"}
        market_to_sym = {}
        for sym, addr in self.cfg.markets.items():
            if sym in PNL_SYMBOLS:
                market_to_sym[addr.lower()] = sym

        # Fetch on-chain trades (merged with local store)
        all_stored = await self._fetch_and_store_trades()
        reset_ts = self._get_pnl_reset_ts()
        on_chain = [t for t in all_stored if t.get("timestamp", 0) >= reset_ts] if reset_ts else all_stored

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
        """Build the PDF file with all trades (on-chain + local) and return its path."""
        ET = ZoneInfo("America/New_York")

        # ── Normalize on-chain trades into unified dicts ──
        unified = []
        seen_keys = set()
        for t in on_chain_trades:
            tx = t.get("tx_hash", "")
            li = t.get("log_index", 0)
            key = f"{tx}:{li}" if tx else ""
            sym = market_to_sym.get((t.get("market_address") or "").lower(), "???")
            side = "LONG" if t.get("is_long") else "SHORT"
            pnl = t.get("pnl_usd", 0.0)
            unified.append({
                "symbol": sym,
                "side": side,
                "size_usd": t.get("size_delta_usd", 0.0),
                "pnl_usd": pnl,
                "timestamp": t.get("timestamp", 0),
                "tx_hash": tx,
                "source": "chain",
            })
            if key:
                seen_keys.add(key)
            if tx:
                seen_keys.add(tx)  # also track bare tx_hash for local dedup

        # Add local trades that aren't already in on-chain set
        for t in self.trade_history:
            tx = getattr(t, "tx_hash", "") or getattr(t, "id", "")
            if tx in seen_keys:
                continue
            unified.append({
                "symbol": t.symbol,
                "side": t.side,
                "size_usd": t.size_usd,
                "pnl_usd": t.pnl_usd,
                "timestamp": t.closed_at,
                "tx_hash": tx,
                "source": "local",
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "leverage": t.leverage,
                "exit_reason": t.exit_reason,
                "pnl_percentage": t.pnl_percentage,
            })

        # Exclude dust trades (< $1 PnL) and sort newest first
        unified = [t for t in unified if abs(t["pnl_usd"]) >= 1]
        unified.sort(key=lambda x: x["timestamp"], reverse=True)

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        # ── Title ──
        pdf.set_font("Helvetica", "B", 18)
        pdf.cell(0, 12, "GMX V2 Trade History", new_x="LMARGIN", new_y="NEXT", align="C")
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

        # ── Trade List (newest first) ──
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

            # Green for wins, red for losses
            if pnl >= 0:
                pdf.set_text_color(0, 128, 0)
            else:
                pdf.set_text_color(200, 0, 0)

            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 6, f"#{i}  {t['symbol']} {t['side']}", new_x="LMARGIN", new_y="NEXT")

            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 9)

            # Show entry/exit if available (local trades), otherwise just size + PnL
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

            reason = t.get("exit_reason", "")
            reason_str = f"  |  Reason: {reason}" if reason else ""
            pdf.cell(0, 5, f"  Date: {date_str}{reason_str}", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, prefix="gmx_trades_")
        pdf.output(tmp.name)
        return tmp.name
