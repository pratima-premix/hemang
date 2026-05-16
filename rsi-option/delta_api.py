"""
Delta Exchange India API client.
Handles authentication (HMAC-SHA256), order placement, position queries,
option chain retrieval, and OHLCV candle data.
"""

import hashlib
import hmac
import json
import logging
import time

import requests

from config import DELTA_API_KEY, DELTA_API_SECRET, DELTA_BASE_URL

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({"User-Agent": "RSI-Option-Bot/1.0"})


# ── Auth ──────────────────────────────────────────────────────────────────────

def _make_headers(method: str, path: str, query_string: str = "", body: str = "") -> dict:
    """Build signed request headers required by Delta Exchange."""
    timestamp = str(int(time.time()))
    # Signature = HMAC-SHA256(secret, METHOD + timestamp + path + query_string + body)
    message = method.upper() + timestamp + path + query_string + body
    sig = hmac.new(
        DELTA_API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "api-key":      DELTA_API_KEY,
        "signature":    sig,
        "timestamp":    timestamp,
        "Content-Type": "application/json",
    }


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(path: str, params: dict = None) -> dict:
    query_string = ""
    url = DELTA_BASE_URL + path
    if params:
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        url += "?" + qs
        query_string = "?" + qs   # Delta Exchange includes "?" in the HMAC signature data
    headers = _make_headers("GET", path, query_string)
    resp = _session.get(url, headers=headers, timeout=15)
    data = resp.json()
    if not resp.ok:
        logger.error("GET %s → %s: %s", path, resp.status_code, data)
        resp.raise_for_status()
    return data


def _post(path: str, body: dict) -> dict:
    body_str = json.dumps(body, separators=(",", ":"))
    headers = _make_headers("POST", path, body=body_str)
    resp = _session.post(DELTA_BASE_URL + path, headers=headers, data=body_str, timeout=15)
    data = resp.json()
    if not resp.ok:
        logger.error("POST %s → %s: %s", path, resp.status_code, data)
        resp.raise_for_status()
    return data


def _delete(path: str, body: dict) -> dict:
    body_str = json.dumps(body, separators=(",", ":"))
    headers = _make_headers("DELETE", path, body=body_str)
    resp = _session.delete(DELTA_BASE_URL + path, headers=headers, data=body_str, timeout=15)
    data = resp.json()
    if not resp.ok:
        logger.error("DELETE %s → %s: %s", path, resp.status_code, data)
        resp.raise_for_status()
    return data


# ── Public API wrappers ───────────────────────────────────────────────────────

def get_wallet_balances() -> list:
    """Return list of all wallet balances."""
    resp = _get("/v2/wallet/balances")
    return resp.get("result", [])


def get_usdt_balance() -> float:
    """Return available USDT (or USD) balance."""
    for b in get_wallet_balances():
        if b.get("asset_symbol") in ("USDT", "USD"):
            return float(b.get("available_balance", 0) or 0)
    return 0.0


def get_positions(underlying: str = None) -> list:
    """Return open positions, optionally filtered by underlying asset symbol."""
    params = {}
    if underlying:
        params["underlying_asset_symbol"] = underlying
    resp = _get("/v2/positions/margined", params)
    result = resp.get("result", [])
    if isinstance(result, list):
        return result
    return [result] if result else []


def get_ticker(symbol: str) -> dict:
    """Return ticker data for a single product symbol."""
    resp = _get(f"/v2/tickers/{symbol}")
    return resp.get("result", {})


def get_option_chain(asset: str, expiry_date: str) -> list:
    """
    Return option chain tickers for a given asset and expiry.
    asset      : 'BTC' or 'ETH'
    expiry_date: 'DD-MM-YYYY'
    """
    params = {
        "contract_types":          "call_options,put_options",
        "underlying_asset_symbols": asset,
        "expiry_date":             expiry_date,
    }
    resp = _get("/v2/tickers", params)
    return resp.get("result", [])


# Resolution mapping: internal names → Delta chart/history format (matches TradingView)
_RES_MAP = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "1w": "W",
}


def get_ohlcv(symbol: str, resolution: str, start: int, end: int) -> list:
    """
    Return OHLCV candles using the same chart/history endpoint as TradingView.
    resolution: '5m', '1h', '1d', etc. (auto-mapped to Delta's '5', '60', 'D')
    Returns list of dicts: {'time', 'open', 'high', 'low', 'close', 'volume'}
    """
    res = _RES_MAP.get(resolution, resolution)
    resp = _get("/v2/chart/history", {
        "symbol": symbol, "resolution": res,
        "from": str(start), "to": str(end),
    })
    result = resp.get("result", {})

    times  = result.get("t", [])
    opens  = result.get("o", [])
    highs  = result.get("h", [])
    lows   = result.get("l", [])
    closes = result.get("c", [])
    vols   = result.get("v", [])

    return [
        {
            "time":   times[i],
            "open":   opens[i]  if i < len(opens)  else 0,
            "high":   highs[i]  if i < len(highs)  else 0,
            "low":    lows[i]   if i < len(lows)   else 0,
            "close":  closes[i] if i < len(closes) else 0,
            "volume": vols[i]   if i < len(vols)   else 0,
        }
        for i in range(len(times))
    ]


def get_product(symbol: str) -> dict:
    """Return full product spec (initial_margin, contract_value, tick_size, etc.)."""
    resp = _get(f"/v2/products/{symbol}")
    return resp.get("result", {})


def place_limit_order(product_symbol: str, side: str, size: int, limit_price: float,
                      reduce_only: bool = False) -> dict:
    """Place a limit order. side = 'buy' | 'sell'."""
    body = {
        "product_symbol": product_symbol,
        "size":           size,
        "side":           side,
        "order_type":     "limit_order",
        "limit_price":    str(limit_price),
    }
    if reduce_only:
        body["reduce_only"] = True
    resp = _post("/v2/orders", body)
    return resp.get("result", {})


def place_stop_limit_order(product_symbol: str, side: str, size: int,
                           stop_price: float, limit_price: float,
                           reduce_only: bool = False) -> dict:
    """Place a stop-limit order. Triggers a limit when stop_price is touched."""
    body = {
        "product_symbol": product_symbol,
        "size":           size,
        "side":           side,
        "order_type":     "stop_limit_order",
        "stop_price":     str(stop_price),
        "limit_price":    str(limit_price),
    }
    if reduce_only:
        body["reduce_only"] = True
    resp = _post("/v2/orders", body)
    return resp.get("result", {})


def place_market_order(product_symbol: str, side: str, size: int, reduce_only: bool = False) -> dict:
    """Place a market order. reduce_only=True for position-closing orders."""
    body = {
        "product_symbol": product_symbol,
        "size":           size,
        "side":           side,
        "order_type":     "market_order",
    }
    if reduce_only:
        body["reduce_only"] = True
    resp = _post("/v2/orders", body)
    return resp.get("result", {})


def get_order(order_id: int) -> dict:
    """Return a single order's current state."""
    resp = _get(f"/v2/orders/{order_id}")
    return resp.get("result", {})


def cancel_order(order_id: int, product_id: int) -> dict:
    """Cancel a specific open order by ID."""
    resp = _delete("/v2/orders", {"id": order_id, "product_id": product_id})
    return resp.get("result", {})


def cancel_all_open_orders(protected_order_ids: set = None) -> list:
    """
    Cancel open orders on startup, skipping any IDs in protected_order_ids.
    Protected orders are TP/SL bracket orders tied to monitored positions.
    """
    if protected_order_ids is None:
        protected_order_ids = set()
    try:
        orders = get_open_orders()
        cancelled = []
        for o in orders:
            if o.get("id") in protected_order_ids:
                logger.info("Startup: keeping bracket order %s (%s)", o["id"], o.get("product_symbol"))
                continue
            try:
                cancel_order(o["id"], o["product_id"])
                cancelled.append(o.get("product_symbol", o["id"]))
            except Exception as exc:
                logger.warning("Could not cancel order %s: %s", o.get("id"), exc)
        if cancelled:
            logger.info("Startup: cancelled %d dangling open order(s): %s", len(cancelled), cancelled)
        return cancelled
    except Exception as exc:
        logger.error("cancel_all_open_orders failed: %s", exc)
        return []


def get_open_orders(product_symbol: str = None) -> list:
    """Return all open orders, optionally filtered by product symbol."""
    params = {"state": "open"}
    if product_symbol:
        params["product_symbol"] = product_symbol
    resp = _get("/v2/orders", params)
    return resp.get("result", [])


def get_order_book(symbol: str, depth: int = 5) -> dict:
    """Return L2 order book. Result has 'buy' and 'sell' lists of [price, size]."""
    resp = _get(f"/v2/l2orderbook/{symbol}", {"depth": str(depth)})
    return resp.get("result", {})
