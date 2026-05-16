import hashlib
import hmac
import time
import json
import requests
from typing import Dict, List, Optional
from config import API_KEY, API_SECRET, BASE_URL
import logging

logger = logging.getLogger(__name__)


class DeltaClient:
    def __init__(self):
        self.api_key    = API_KEY
        self.api_secret = API_SECRET
        self.base_url   = BASE_URL
        self.session    = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "CFX-CPR-Bot/1.0",
        })

    # ── Auth ──────────────────────────────────────────────────────────────
    def _sign(self, method: str, path: str, query: str = "", body: str = "") -> Dict[str, str]:
        ts  = str(int(time.time()))
        msg = method + ts + path + query + body
        sig = hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {"api-key": self.api_key, "signature": sig, "timestamp": ts}

    # ── HTTP helpers ──────────────────────────────────────────────────────
    def _get(self, path: str, params: Dict = None, auth: bool = False) -> Dict:
        qs = ""
        if params:
            qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        headers = self._sign("GET", path, qs) if auth else {}
        resp = self.session.get(self.base_url + path + qs, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: Dict = None) -> Dict:
        body_str = json.dumps(body or {})
        headers  = self._sign("POST", path, "", body_str)
        resp = self.session.post(self.base_url + path, headers=headers, data=body_str, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str, body: Dict = None) -> Dict:
        body_str = json.dumps(body or {})
        headers  = self._sign("DELETE", path, "", body_str)
        resp = self.session.delete(self.base_url + path, headers=headers, data=body_str, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Market data ───────────────────────────────────────────────────────
    def get_candles(self, symbol: str, resolution: str = "1h", limit: int = 200) -> List[Dict]:
        """Fetch OHLCV candles. resolution: 1m,5m,15m,30m,1h,4h,1d,1w"""
        import time as _t
        SECS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}
        step = SECS.get(resolution, 3600)
        now  = int(_t.time())
        start = now - step * (limit + 10)
        r = self._get("/v2/history/candles", {
            "symbol": symbol, "resolution": resolution,
            "start": start, "end": now, "limit": limit,
        })
        return r.get("result", []) if r.get("success") else []

    def get_products_by_type(self, contract_type: str, limit: int = 500) -> List[Dict]:
        """Get all products of a given contract_type."""
        r = self._get("/v2/products", {"contract_type": contract_type, "limit": limit})
        return r.get("result", []) if r.get("success") else []

    def get_options_for_underlying(self, underlying_asset_id: int,
                                   contract_type: str, limit: int = 500) -> List[Dict]:
        """Get option products for a specific underlying by asset_id."""
        r = self._get("/v2/products", {
            "contract_type": contract_type,
            "underlying_asset_id": underlying_asset_id,
            "limit": limit,
        })
        if r.get("success"):
            return r.get("result", [])
        # Fallback: get all and filter
        all_p = self.get_products_by_type(contract_type, limit)
        return [p for p in all_p
                if p.get("spot_index", {}).get("underlying_asset_id") == underlying_asset_id
                   or p.get("underlying_asset_id") == underlying_asset_id]

    def get_products(self, underlying: str, contract_type: str) -> List[Dict]:
        """Get option products for a named underlying asset (BTC/ETH)."""
        prefix = "C-" if contract_type == "call_options" else "P-"
        all_p  = self.get_products_by_type(contract_type, 1000)
        return [p for p in all_p if p.get("symbol", "").startswith(f"{prefix}{underlying}")]

    def get_ticker(self, product_id: int) -> Optional[Dict]:
        """Fetch all tickers and find the one matching product_id."""
        r = self._get("/v2/tickers")
        if r.get("success"):
            for t in r.get("result", []):
                if t.get("product_id") == product_id:
                    return t
        return None

    def get_mark_price(self, product_id: int) -> float:
        t = self.get_ticker(product_id)
        if t:
            return float(t.get("mark_price", 0) or 0)
        return 0.0

    # ── Account ───────────────────────────────────────────────────────────
    def get_wallet_balance(self) -> float:
        """Returns available USD balance (net equity from meta)."""
        r = self._get("/v2/wallet/balances", auth=True)
        # Use meta.net_equity as primary source
        meta = r.get("meta", {})
        if meta.get("net_equity"):
            return float(meta["net_equity"])
        # Fallback: sum USD asset
        for a in r.get("result", []):
            if a.get("asset_symbol") == "USD":
                return float(a.get("available_balance", 0))
        return 0.0

    def get_positions(self, underlying: str = None) -> List[Dict]:
        params = {}
        if underlying:
            params["underlying_asset_symbol"] = underlying
        r = self._get("/v2/positions", params, auth=True)
        return r.get("result", []) if r.get("success") else []

    def get_positions_for_product(self, product_id: int) -> List[Dict]:
        """Return open positions for a specific product_id (non-zero size).
        The API returns a single dict when queried by product_id, not a list.
        """
        r = self._get("/v2/positions", {"product_id": product_id}, auth=True)
        if r.get("success"):
            result = r.get("result", [])
            if isinstance(result, dict):
                return [result] if float(result.get("size", 0)) != 0 else []
            if isinstance(result, list):
                return [p for p in result if isinstance(p, dict) and float(p.get("size", 0)) != 0]
        return []

    # ── Orders ────────────────────────────────────────────────────────────
    def place_order(self, product_id: int, side: str, size: float,
                    order_type: str = "market_order",
                    limit_price: float = None,
                    client_order_id: str = None,
                    bracket_tp_price: float = None,
                    bracket_sl_price: float = None) -> Dict:
        body: Dict = {
            "product_id": product_id,
            "side":       side,
            "size":       int(size),
            "order_type": order_type,
        }
        if limit_price:
            body["limit_price"] = str(limit_price)
        if client_order_id:
            body["client_order_id"] = client_order_id[:32]

        # Native exchange bracket TP and SL (triggered by option mark price)
        if bracket_tp_price and bracket_tp_price > 0:
            body["bracket_take_profit_price"]       = str(bracket_tp_price)
            body["bracket_take_profit_limit_price"] = str(round(bracket_tp_price * 0.98, 4))
        if bracket_sl_price and bracket_sl_price > 0:
            body["bracket_stop_loss_price"]       = str(bracket_sl_price)
            body["bracket_stop_loss_limit_price"] = str(round(bracket_sl_price * 1.02, 4))

        r = self._post("/v2/orders", body)
        logger.info(f"place_order → {r.get('result', {}).get('id')} state={r.get('result', {}).get('state')}")
        return r

    def cancel_order(self, order_id: int, product_id: int) -> Dict:
        """Cancel a single order. Delta requires body {id, product_id} on DELETE /v2/orders."""
        return self._delete("/v2/orders", {"id": order_id, "product_id": product_id})

    def get_open_orders(self, product_id: int = None) -> List[Dict]:
        params: Dict = {"state": "open"}
        if product_id:
            params["product_id"] = product_id
        r = self._get("/v2/orders", params, auth=True)
        return r.get("result", []) if r.get("success") else []
