import hashlib
import hmac
import time
import json
import logging
import requests
from typing import Optional

log = logging.getLogger(__name__)


class DeltaClient:
    BASE_URL = "https://api.india.delta.exchange"

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def _sign(self, method: str, path: str, qs: str = "", body: str = "") -> tuple:
        ts = str(int(time.time()))
        prehash = method + ts + path + qs + body
        sig = hmac.new(self.api_secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()
        return sig, ts

    def _auth_headers(self, method: str, path: str, qs: str = "", body: str = "") -> dict:
        sig, ts = self._sign(method, path, qs, body)
        return {"api-key": self.api_key, "signature": sig, "timestamp": ts}

    def _get(self, path: str, params: dict = None, auth: bool = True) -> dict:
        qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        url = f"{self.BASE_URL}{path}" + (f"?{qs}" if qs else "")
        # Delta Exchange requires '?' included in the prehash when query string exists
        path_for_sign = path + (f"?{qs}" if qs else "")
        headers = self._auth_headers("GET", path_for_sign) if auth else {}
        r = self.session.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload)
        headers = self._auth_headers("POST", path, "", body)
        r = self.session.post(f"{self.BASE_URL}{path}", headers=headers, data=body, timeout=10)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload)
        headers = self._auth_headers("DELETE", path, "", body)
        r = self.session.delete(f"{self.BASE_URL}{path}", headers=headers, data=body, timeout=10)
        r.raise_for_status()
        return r.json()

    # ── Market Data ──────────────────────────────────────────────────────────

    def get_candles(self, symbol: str, resolution: str, bars: int = 50) -> list[float]:
        """
        Returns a list of close prices (oldest → newest).
        resolution: '5' for 5-minute bars, 'D' for daily bars.
        """
        import time as _time
        now = int(_time.time())
        seconds_per_bar = 86400 if resolution == "D" else int(resolution) * 60
        from_ts = now - seconds_per_bar * (bars + 5)
        data = self._get("/v2/chart/history", {
            "symbol": symbol, "resolution": resolution,
            "from": from_ts, "to": now,
        }, auth=False)
        result = data.get("result", {})
        closes = result.get("c", [])
        return [float(c) for c in closes]

    def get_ticker(self, symbol: str) -> dict:
        data = self._get(f"/v2/tickers/{symbol}", auth=False)
        return data.get("result", {})

    def get_mark_price(self, symbol: str) -> float:
        ticker = self.get_ticker(symbol)
        return float(ticker.get("mark_price", 0))

    # ── Account ───────────────────────────────────────────────────────────────

    def get_wallet(self) -> list:
        return self._get("/v2/wallet/balances").get("result", [])

    def get_usdt_balance(self) -> float:
        for symbol in ("USDT", "USD"):
            for asset in self.get_wallet():
                if asset.get("asset_symbol") == symbol:
                    return float(asset.get("available_balance", 0))
        return 0.0

    def get_positions(self) -> list:
        return self._get("/v2/positions/margined").get("result", [])

    def get_position(self, product_id: int) -> Optional[dict]:
        for p in self.get_positions():
            if p.get("product_id") == product_id and float(p.get("size", 0)) != 0:
                return p
        return None

    def get_open_orders(self, product_id: int) -> list:
        return self._get("/v2/orders", {"product_id": product_id, "state": "open"}).get("result", [])

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_market_order(self, product_id: int, side: str, size: int,
                           reduce_only: bool = False) -> dict:
        payload = {
            "product_id": product_id,
            "side": side,
            "size": size,
            "order_type": "market_order",
            "reduce_only": reduce_only,
        }
        result = self._post("/v2/orders", payload)
        log.info("market_order response: %s", result)
        return result

    def place_limit_order(self, product_id: int, side: str, size: int,
                          price: float, reduce_only: bool = False) -> dict:
        payload = {
            "product_id": product_id,
            "side": side,
            "size": size,
            "order_type": "limit_order",
            "limit_price": str(round(price, 8)),
            "reduce_only": reduce_only,
            "time_in_force": "gtc",
        }
        result = self._post("/v2/orders", payload)
        log.info("limit_order response: %s", result)
        return result

    def place_stop_market_order(self, product_id: int, side: str, size: int,
                                stop_price: float, reduce_only: bool = True) -> dict:
        payload = {
            "product_id": product_id,
            "side": side,
            "size": size,
            "order_type": "market_order",
            "stop_order_type": "stop_loss_order",
            "stop_price": str(round(stop_price, 8)),
            "reduce_only": reduce_only,
            "close_on_trigger": True,
        }
        result = self._post("/v2/orders", payload)
        log.info("stop_order response: %s", result)
        return result

    def cancel_order(self, product_id: int, order_id: int) -> dict:
        return self._delete(f"/v2/orders/{order_id}", {"product_id": product_id})

    def cancel_all_orders(self, product_id: int) -> dict:
        return self._delete("/v2/orders/all", {
            "product_id": product_id,
            "cancel_limit_orders": True,
            "cancel_stop_orders": True,
        })

    def cancel_all_stop_orders(self, product_id: int) -> dict:
        """Cancel only stop/conditional orders, leaving limit orders (TP) intact."""
        return self._delete("/v2/orders/all", {
            "product_id": product_id,
            "cancel_limit_orders": False,
            "cancel_stop_orders": True,
        })

    def close_position(self, product_id: int, size: int, position_side: str) -> dict:
        close_side = "sell" if position_side == "buy" else "buy"
        return self.place_market_order(product_id, close_side, abs(size), reduce_only=True)
