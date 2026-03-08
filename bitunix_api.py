"""
Bitunix Futures REST API client.

Handles authentication (double SHA-256 signing), request building,
and all trading/position/account endpoints needed by the bot.
"""

import hashlib
import json
import time
import uuid
import logging
import threading
from typing import Optional, Dict, Any, List

import requests

log = logging.getLogger("BitunixAPI")

BASE_URL = "https://fapi.bitunix.com"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Signing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sort_query_params(params: dict) -> str:
    """Sort query params by key (ascending ASCII) and concat key+value pairs."""
    if not params:
        return ""
    return "".join(f"{k}{params[k]}" for k in sorted(params.keys()))


def _sign(api_key: str, secret_key: str, nonce: str, timestamp: str,
          query_params: str = "", body: str = "") -> str:
    """Compute Bitunix double-SHA256 signature.

    Step 1: digest = SHA256(nonce + timestamp + api_key + query_params + body)
    Step 2: sign   = SHA256(digest + secret_key)
    """
    digest = _sha256(nonce + timestamp + api_key + query_params + body)
    return _sha256(digest + secret_key)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Client
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BitunixClient:
    """Bitunix Futures API client with automatic request signing."""

    def __init__(self, api_key: str, secret_key: str, timeout: int = 15,
                 max_requests_per_sec: int = 10):
        self.api_key = api_key
        self.secret_key = secret_key
        self.timeout = timeout
        self._session = requests.Session()
        # Rate limiter: simple semaphore-based throttle
        self._rate_semaphore = threading.Semaphore(max_requests_per_sec)
        self._rate_lock = threading.Lock()
        self._request_timestamps: list = []

    # ── Low-level request ──

    def _throttle(self):
        """Simple sliding-window rate limiter."""
        with self._rate_lock:
            now = time.time()
            # Remove timestamps older than 1 second
            self._request_timestamps = [
                t for t in self._request_timestamps if now - t < 1.0
            ]
            if len(self._request_timestamps) >= 10:
                sleep_time = 1.0 - (now - self._request_timestamps[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
            self._request_timestamps.append(time.time())

    def _request(self, method: str, path: str,
                 params: Optional[dict] = None,
                 body: Optional[dict] = None) -> dict:
        """Send an authenticated request and return parsed JSON response."""
        self._throttle()

        nonce = uuid.uuid4().hex
        timestamp = str(int(time.time() * 1000))

        query_str = _sort_query_params(params) if params else ""
        body_str = json.dumps(body, separators=(",", ":")) if body else ""

        sig = _sign(self.api_key, self.secret_key, nonce, timestamp,
                     query_str, body_str)

        headers = {
            "api-key": self.api_key,
            "nonce": nonce,
            "timestamp": timestamp,
            "sign": sig,
            "Content-Type": "application/json",
            "language": "en",
        }

        url = BASE_URL + path

        if method == "GET":
            resp = self._session.get(url, params=params, headers=headers,
                                     timeout=self.timeout)
        else:
            # Send the exact same serialized body that was signed
            resp = self._session.post(url, data=body_str if body_str else None,
                                      headers=headers, timeout=self.timeout)

        # Check HTTP status before parsing JSON
        if resp.status_code == 429:
            log.warning(f"Bitunix rate limit hit on {path}")
            raise RuntimeError(f"Bitunix rate limit (429) on {path}")
        if resp.status_code >= 500:
            log.error(f"Bitunix server error on {path}: HTTP {resp.status_code}")
            raise RuntimeError(f"Bitunix server error (HTTP {resp.status_code})")

        # Validate JSON response
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            log.error(f"Invalid JSON from {path}: status={resp.status_code} body={resp.text[:200]}")
            raise RuntimeError(f"Bitunix API returned invalid JSON (HTTP {resp.status_code})")

        if data.get("code") != 0:
            log.error(f"API error {path}: code={data.get('code')} msg={data.get('msg')}")
            raise RuntimeError(f"Bitunix API error: {data.get('msg')} (code {data.get('code')})")
        return data

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: Optional[dict] = None) -> dict:
        return self._request("POST", path, body=body or {})

    # ── Account ──

    def get_account(self, margin_coin: str = "USDT") -> dict:
        """Get account balance info."""
        data = self._get("/api/v1/futures/account", {"marginCoin": margin_coin})
        result = data.get("data", {})
        # API may return a dict (single account) or a list
        if isinstance(result, list):
            return result[0] if result else {}
        return result

    def get_balance(self, margin_coin: str = "USDT") -> float:
        """Get available USDT balance."""
        acct = self.get_account(margin_coin)
        return float(acct.get("available", "0"))

    def change_leverage(self, symbol: str, leverage: int,
                        margin_coin: str = "USDT") -> dict:
        """Set leverage for a symbol."""
        return self._post("/api/v1/futures/account/change_leverage", {
            "marginCoin": margin_coin,
            "symbol": symbol,
            "leverage": str(leverage),
        })

    def get_leverage_margin_mode(self, symbol: str,
                                 margin_coin: str = "USDT") -> dict:
        """Get current leverage and margin mode for a symbol."""
        data = self._get("/api/v1/futures/account/get_leverage_margin_mode", {
            "marginCoin": margin_coin,
            "symbol": symbol,
        })
        return data.get("data", {})

    def change_margin_mode(self, symbol: str, margin_mode: str,
                           margin_coin: str = "USDT") -> dict:
        """Change margin mode (ISOLATION or CROSS)."""
        return self._post("/api/v1/futures/account/change_margin_mode", {
            "marginCoin": margin_coin,
            "symbol": symbol,
            "marginMode": margin_mode,
        })

    # ── Orders ──

    def place_order(self, symbol: str, side: str, qty: str,
                    order_type: str = "MARKET",
                    price: str = None,
                    trade_side: str = "OPEN",
                    position_id: str = None,
                    client_id: str = None,
                    reduce_only: bool = False,
                    tp_price: str = None,
                    sl_price: str = None,
                    tp_stop_type: str = "LAST_PRICE",
                    sl_stop_type: str = "LAST_PRICE") -> dict:
        """Place a futures order.

        Args:
            symbol: Trading pair (e.g. "BTCUSDT")
            side: "BUY" or "SELL"
            qty: Amount in base coin
            order_type: "MARKET" or "LIMIT"
            price: Required for LIMIT orders
            trade_side: "OPEN" or "CLOSE"
            position_id: Required when trade_side is "CLOSE"
            tp_price: Take profit trigger price
            sl_price: Stop loss trigger price
        """
        body = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "tradeSide": trade_side,
            "orderType": order_type,
        }
        if price:
            body["price"] = price
        if position_id:
            body["positionId"] = position_id
        if client_id:
            body["clientId"] = client_id
        if reduce_only:
            body["reduceOnly"] = True
        if tp_price:
            body["tpPrice"] = tp_price
            body["tpStopType"] = tp_stop_type
            body["tpOrderType"] = "MARKET"
        if sl_price:
            body["slPrice"] = sl_price
            body["slStopType"] = sl_stop_type
            body["slOrderType"] = "MARKET"
        if order_type == "LIMIT" and not body.get("effect"):
            body["effect"] = "GTC"

        data = self._post("/api/v1/futures/trade/place_order", body)
        return data.get("data", {})

    def cancel_orders(self, symbol: str, order_ids: List[str]) -> dict:
        """Cancel one or more orders."""
        return self._post("/api/v1/futures/trade/cancel_orders", {
            "symbol": symbol,
            "orderList": [{"orderId": oid} for oid in order_ids],
        })

    def cancel_all_orders(self, symbol: str = None) -> dict:
        """Cancel all pending orders (optionally for a symbol)."""
        body = {}
        if symbol:
            body["symbol"] = symbol
        return self._post("/api/v1/futures/trade/cancel_all_orders", body)

    def get_pending_orders(self, symbol: str = None) -> list:
        """Get all open/pending orders."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/api/v1/futures/trade/get_pending_orders", params or None)
        result = data.get("data", [])
        if isinstance(result, dict):
            return result.get("orderList", [])
        return result if isinstance(result, list) else []

    def get_order_detail(self, order_id: str) -> dict:
        """Get details of a specific order."""
        data = self._get("/api/v1/futures/trade/get_order_detail",
                         {"orderId": order_id})
        return data.get("data", {})

    def get_history_orders(self, symbol: str = None, limit: int = 50) -> list:
        """Get historical (filled/cancelled) orders."""
        params = {"limit": str(limit)}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/api/v1/futures/trade/get_history_orders", params)
        result = data.get("data", [])
        if isinstance(result, dict):
            return result.get("orderList", [])
        return result if isinstance(result, list) else []

    # ── Positions ──

    def get_pending_positions(self, symbol: str = None) -> list:
        """Get all open positions."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/api/v1/futures/position/get_pending_positions",
                         params or None)
        result = data.get("data", [])
        if isinstance(result, dict):
            return result.get("positionList", [])
        return result if isinstance(result, list) else []

    def get_history_positions(self, symbol: str = None, limit: int = 50) -> list:
        """Get closed position history."""
        params = {"limit": str(limit)}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/api/v1/futures/position/get_history_positions", params)
        result = data.get("data", [])
        if isinstance(result, dict):
            return result.get("positionList", [])
        return result if isinstance(result, list) else []

    def flash_close_position(self, position_id: str) -> dict:
        """Instantly close a position at market price."""
        data = self._post("/api/v1/futures/trade/flash_close_position", {
            "positionId": position_id,
        })
        return data.get("data", {})

    def close_all_positions(self) -> dict:
        """Close all open positions."""
        return self._post("/api/v1/futures/position/close_all_position", {})

    # ── TP/SL Orders ──

    def place_tpsl_order(self, symbol: str, position_id: str,
                         tp_price: str = None, sl_price: str = None,
                         tp_qty: str = None, sl_qty: str = None,
                         tp_stop_type: str = "LAST_PRICE",
                         sl_stop_type: str = "LAST_PRICE") -> dict:
        """Place a TP/SL order on a specific position (partial close)."""
        body = {
            "symbol": symbol,
            "positionId": position_id,
        }
        if tp_price:
            body["tpPrice"] = tp_price
            body["tpStopType"] = tp_stop_type
            body["tpOrderType"] = "MARKET"
            if tp_qty:
                body["tpQty"] = tp_qty
        if sl_price:
            body["slPrice"] = sl_price
            body["slStopType"] = sl_stop_type
            body["slOrderType"] = "MARKET"
            if sl_qty:
                body["slQty"] = sl_qty

        data = self._post("/api/v1/futures/tpsl/place_order", body)
        return data.get("data", {})

    def place_position_tpsl(self, symbol: str, position_id: str,
                            tp_price: str = None, sl_price: str = None,
                            tp_stop_type: str = "LAST_PRICE",
                            sl_stop_type: str = "LAST_PRICE") -> dict:
        """Place a full-position TP/SL (closes entire position when triggered).

        Only one active Position TP/SL per position.
        """
        body = {
            "symbol": symbol,
            "positionId": position_id,
        }
        if tp_price:
            body["tpPrice"] = tp_price
            body["tpStopType"] = tp_stop_type
        if sl_price:
            body["slPrice"] = sl_price
            body["slStopType"] = sl_stop_type

        data = self._post("/api/v1/futures/tpsl/position/place_order", body)
        return data.get("data", {})

    def modify_position_tpsl(self, symbol: str, position_id: str,
                             tp_price: str = None, sl_price: str = None,
                             tp_stop_type: str = "LAST_PRICE",
                             sl_stop_type: str = "LAST_PRICE") -> dict:
        """Modify an existing full-position TP/SL."""
        body = {
            "symbol": symbol,
            "positionId": position_id,
        }
        if tp_price:
            body["tpPrice"] = tp_price
            body["tpStopType"] = tp_stop_type
        if sl_price:
            body["slPrice"] = sl_price
            body["slStopType"] = sl_stop_type

        data = self._post("/api/v1/futures/tpsl/position/modify_order", body)
        return data.get("data", {})

    def get_pending_tpsl_orders(self, symbol: str = None) -> list:
        """Get active TP/SL orders."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/api/v1/futures/tpsl/get_pending_orders", params or None)
        result = data.get("data", [])
        if isinstance(result, dict):
            return result.get("orderList", [])
        return result if isinstance(result, list) else []

    def get_history_tpsl_orders(self, symbol: str = None, limit: int = 50) -> list:
        """Get historical (triggered/cancelled) TP/SL orders."""
        params = {"limit": str(limit)}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/api/v1/futures/tpsl/get_history_orders", params)
        result = data.get("data", [])
        if isinstance(result, dict):
            return result.get("orderList", [])
        return result if isinstance(result, list) else []

    def cancel_tpsl_order(self, symbol: str, order_id: str) -> dict:
        """Cancel a TP/SL order."""
        return self._post("/api/v1/futures/tpsl/cancel_order", {
            "symbol": symbol,
            "orderId": order_id,
        })

    # ── Market Data ──

    def get_ticker(self, symbol: str) -> dict:
        """Get current ticker for a symbol."""
        data = self._get("/api/v1/futures/market/tickers", {"symbol": symbol})
        tickers = data.get("data", [])
        if isinstance(tickers, list):
            # API may return all tickers — filter for the requested symbol
            for t in tickers:
                if t.get("symbol") == symbol:
                    return t
            # Fallback to first if only one returned
            if len(tickers) == 1:
                return tickers[0]
            return {}
        return tickers if isinstance(tickers, dict) else {}

    def get_tickers(self) -> list:
        """Get all tickers."""
        data = self._get("/api/v1/futures/market/tickers")
        return data.get("data", [])

    def get_trading_pairs(self, symbol: str = None) -> list:
        """Get trading pair details (tick size, lot size, etc.)."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/api/v1/futures/market/trading_pairs", params or None)
        return data.get("data", [])

    def get_current_price(self, symbol: str) -> float:
        """Get the last price for a symbol."""
        ticker = self.get_ticker(symbol)
        return float(ticker.get("lastPrice", ticker.get("last", "0")))
