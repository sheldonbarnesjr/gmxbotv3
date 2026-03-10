"""
Price Feeds Mixin for GMX V2 Trading Bot.

This mixin provides price fetching and caching methods that GMXBot inherits.
It manages price data from Chainlink feeds and GMX, with periodic updates
and cache management.

Host class (GMXBot) must provide:
  - self.cfg: Config object with price_update_interval, price_max_age_s
  - self.price_cache: Dict[str, PriceData] for caching prices
  - self.health_stats: Dict with "price_updates" counter
  - self.w3: Web3 instance
  - self.logger: logging.Logger instance
  - self._all_wallets(): method returning iterator of (wallet_id, account)
"""

import asyncio
import logging
from typing import Optional, Dict

from config import ALLOWED_SYMBOLS, CHAINLINK_FEEDS
from open import fetch_current_price
from close import (
    fetch_positions as chain_fetch_positions,
    fetch_current_price as close_fetch_current_price,
)


class PriceFeedsMixin:
    """Mixin for price fetching and caching methods."""

    async def price_update_loop(self):
        """Continuously update prices for tracked symbols."""
        while True:
            try:
                await self.update_all_prices()
                await asyncio.sleep(self.cfg.price_update_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Price update loop error: {e}")
                await asyncio.sleep(self.cfg.price_update_interval)

    async def update_all_prices(self):
        """Fetch fresh prices for all tracked symbols."""
        for symbol in ALLOWED_SYMBOLS:
            try:
                price = await self.fetch_price(symbol)
                if price:
                    self.price_cache[symbol] = PriceData(
                        symbol=symbol,
                        price=price,
                        max_age_s=self.cfg.price_max_age_s,
                    )
                    self.health_stats["price_updates"] += 1
            except Exception as e:
                self.logger.warning(f"Failed to update {symbol} price: {e}")

        # Check for stale prices — warn if ALL symbols are stale
        all_stale = all(
            symbol not in self.price_cache or not self.price_cache[symbol].is_fresh
            for symbol in ALLOWED_SYMBOLS
        )
        if all_stale and self.price_cache:
            ages = {s: f"{self.price_cache[s].age_seconds:.0f}s" for s in self.price_cache}
            self.logger.warning(f"All prices stale: {ages}")

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price from cache or fetch if stale."""
        if symbol in self.price_cache and self.price_cache[symbol].is_fresh:
            return self.price_cache[symbol].price

        price = await self.fetch_price(symbol)
        if price:
            self.price_cache[symbol] = PriceData(
                symbol=symbol,
                price=price,
                max_age_s=self.cfg.price_max_age_s,
            )
        return price

    async def fetch_price(self, symbol: str) -> Optional[float]:
        """Fetch price from on-chain Chainlink feeds or GMX."""
        try:
            return await asyncio.to_thread(fetch_current_price, symbol, self.w3)
        except Exception as e:
            self.logger.warning(f"Failed to fetch {symbol} price: {e}")
            return None

    async def cmd_prices(self, chat_id: int):
        """Send live prices to chat with GMX and Chainlink comparison."""
        try:
            lines = ["**Live Prices**\n"]
            gmx_prices = {}
            for _, acct in self._all_wallets():
                try:
                    positions = await asyncio.to_thread(chain_fetch_positions, self.w3, acct.address)
                    for cp in positions:
                        sym = cp.symbol.upper().split("/")[0]
                        if cp.current_price and cp.current_price > 0:
                            gmx_prices[sym] = cp.current_price
                except Exception:
                    pass

            chainlink_prices = {}
            for symbol in CHAINLINK_FEEDS:
                try:
                    price = await asyncio.to_thread(fetch_current_price, symbol, self.w3)
                    if price and price > 0:
                        chainlink_prices[symbol] = price
                except Exception:
                    pass

            all_symbols = sorted(set(list(gmx_prices.keys()) + list(chainlink_prices.keys())))
            if not all_symbols:
                await self.send_message(chat_id, "No prices available.")
                return

            for sym in all_symbols:
                gmx_p = gmx_prices.get(sym)
                cl_p = chainlink_prices.get(sym)
                if gmx_p and cl_p:
                    diff = abs(gmx_p - cl_p) / cl_p * 100
                    diff_str = f" (Δ {diff:.2f}%)" if diff > 0.05 else ""
                    lines.append(f"**{sym}**\n  GMX: ${gmx_p:,.2f} | Chainlink: ${cl_p:,.2f}{diff_str}")
                elif gmx_p:
                    lines.append(f"**{sym}**\n  GMX: ${gmx_p:,.2f}")
                elif cl_p:
                    lines.append(f"**{sym}**\n  Chainlink: ${cl_p:,.2f}")

            cached_syms = [s for s in self.price_cache if s not in all_symbols]
            if cached_syms:
                lines.append("\n_Cached:_")
                for sym in sorted(cached_syms):
                    pd = self.price_cache[sym]
                    age = pd.age_seconds
                    age_str = f"{int(age)}s ago" if age < 120 else f"{int(age/60)}m ago"
                    lines.append(f"  {sym}: ${pd.price:,.2f} ({age_str})")

            await self.send_message(chat_id, "\n".join(lines))
        except Exception as e:
            self.logger.error(f"cmd_prices error: {e}")
            await self.send_message(chat_id, f"Error fetching prices: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# PriceData dataclass (used by mixin)
# ──────────────────────────────────────────────────────────────────────────────

import time
from dataclasses import dataclass, field


@dataclass
class PriceData:
    """Cached price with freshness tracking."""
    symbol: str
    price: float
    timestamp: float = field(default_factory=time.time)
    max_age_s: int = 15  # configurable max age

    @property
    def is_fresh(self) -> bool:
        """Check if price is still within max age."""
        return time.time() - self.timestamp < self.max_age_s

    @property
    def age_seconds(self) -> float:
        """Return age of price in seconds."""
        return time.time() - self.timestamp
