import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from delta_client import DeltaClient
from config import (TP_DECAY_PCT, BRACKET_SL_MULT, MAX_HOLD_CANDLES,
                    EXPIRY_TARGET_DAYS, TRAIL_THRESHOLD_PCT, PRE_EXPIRY_CLOSE_MINS)

logger = logging.getLogger(__name__)


def _parse_settlement(p: Dict) -> Optional[datetime]:
    """Parse settlement_time from a product dict → UTC-aware datetime."""
    val = p.get("settlement_time", "")
    if not val:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class OrderManager:
    def __init__(self, client: DeltaClient):
        self.client    = client
        self.positions: Dict[str, Dict] = {}   # asset -> position record

    # ── D+1 expiry selection ──────────────────────────────────────────────
    def find_option_product(self, asset: str, strike: float, opt: str) -> Optional[Dict]:
        """
        Find the option contract for `asset` with:
          1. Strike closest to `strike`
          2. Expiry = D+1 (tomorrow). Falls back D+2 … D+7 if no D+1 exists.
        """
        ctype    = "call_options" if opt == "call" else "put_options"
        products = self.client.get_products(asset, ctype)
        if not products:
            logger.error(f"{asset}: no {ctype} products found")
            return None

        now = datetime.now(timezone.utc)

        # ── Step 1: keep only live, future-expiring products ──────────────
        live = []
        for p in products:
            if p.get("state") != "live":
                continue
            exp = _parse_settlement(p)
            if exp and exp > now:
                live.append((p, exp))

        if not live:
            logger.error(f"{asset}: no live future-expiry {ctype} found")
            return None

        # ── Step 2: rank by preferred expiry day (D+1, then D+2 … D+7) ───
        for days_out in range(EXPIRY_TARGET_DAYS, EXPIRY_TARGET_DAYS + 7):
            target_date = (now + timedelta(days=days_out)).date()
            day_products = [(p, exp) for p, exp in live if exp.date() == target_date]
            if day_products:
                # ── Step 3: among this expiry, find nearest strike ─────────
                day_products.sort(key=lambda x: abs(float(x[0].get("strike_price", 0)) - strike))
                chosen, exp_dt = day_products[0]
                logger.info(
                    f"{asset}: selected {chosen['symbol']} "
                    f"(strike={chosen['strike_price']} expiry={exp_dt.date()} D+{days_out})"
                )
                return chosen

        # Last resort: nearest expiry overall, nearest strike
        live.sort(key=lambda x: (x[1], abs(float(x[0].get("strike_price", 0)) - strike)))
        chosen = live[0][0]
        logger.warning(f"{asset}: no D+1–D+7 expiry found; using {chosen['symbol']}")
        return chosen

    # ── Entry with native bracket TP + SL ────────────────────────────────
    def place_option_order(self, asset: str, signal: Dict, qty: float, ind: Dict):
        action = signal["action"]
        strike = signal["strike"]
        opt    = "call" if action == "SELL_CALL" else "put"

        product = self.find_option_product(asset, strike, opt)
        if not product:
            logger.error(f"{asset}: cannot place order — no product found")
            return

        pid     = int(product["id"])
        premium = self.client.get_mark_price(pid)

        if premium <= 0:
            logger.error(f"{asset}: mark price is 0 for {product.get('symbol')}, skipping")
            return

        # Native exchange bracket prices (option premium levels, not underlying)
        tp_price = round(premium * TP_DECAY_PCT,   2)   # 50 % decay → take profit
        sl_price = round(premium * BRACKET_SL_MULT, 2)  # 3x premium  → hard stop

        label = f"CFX{asset[:1]}{opt[:1].upper()}{int(datetime.now(timezone.utc).timestamp())}"[:32]

        logger.info(
            f"{asset}: placing {action} | {product['symbol']} | "
            f"strike={strike} qty={qty} "
            f"entry_prem={premium:.4f} TP={tp_price:.4f} SL_bkt={sl_price:.4f} "
            f"SL_underlying={signal['sl_price']:.2f}"
        )

        try:
            r = self.client.place_order(
                product_id          = pid,
                side                = "sell",
                size                = qty,
                order_type          = "market_order",
                client_order_id     = label,
                bracket_tp_price    = tp_price,
                bracket_sl_price    = sl_price,
            )
            if r.get("success"):
                self.positions[asset] = {
                    "product_id":      pid,
                    "symbol":          product["symbol"],
                    "side":            action,
                    "strike":          strike,
                    "qty":             qty,
                    "entry_premium":   premium,
                    "tp_premium":      tp_price,
                    "sl_price":        signal["sl_price"],  # underlying SL (bot-level)
                    "bracket_sl":      sl_price,            # option-price SL (exchange-level)
                    "order_id":        r["result"].get("id"),
                    "entry_time":      datetime.now(timezone.utc).isoformat(),
                    "candles_held":    0,
                    "expiry":          product.get("settlement_time", "")[:10],
                }
                logger.info(
                    f"{asset}: ORDER OK id={r['result'].get('id')} | "
                    f"bracket TP={tp_price} SL={sl_price} placed on exchange"
                )
            else:
                logger.error(f"{asset}: order FAILED — {r}")
        except Exception as e:
            logger.exception(f"{asset}: order exception: {e}")

    # ── Hourly bot-level check ────────────────────────────────────────────
    def check_sl_tp(self, asset: str, spot: float):
        """
        Called every hourly tick for open positions.

        Exchange handles (real-time, always active):
          - Bracket TP : option price decays to 50 % of entry premium
          - Bracket SL : option price reaches 3× entry premium

        Bot handles each hourly tick:
          1. Underlying-price ATR SL  → immediate close
          2. Pre-expiry cutoff (1h before settlement) → immediate close
          3. At 4-candle mark → trail decision:
               - Premium ≤ 70% of entry (≥30% decayed) → trail to expiry
               - Otherwise → close now
          4. Sync check: detect if exchange already closed via bracket
        """
        pos = self.positions.get(asset)
        if not pos:
            return

        pos["candles_held"] = pos.get("candles_held", 0) + 1
        now = datetime.now(timezone.utc)

        # ── 1. Underlying-price SL (strategy ATR-based) ───────────────────
        ul_sl = pos["sl_price"]
        if pos["side"] == "SELL_CALL" and spot >= ul_sl:
            logger.info(f"{asset}: underlying SL hit — spot={spot:.2f} >= sl={ul_sl:.2f}")
            self._close(asset, "UNDERLYING_SL_HIT")
            return
        if pos["side"] == "SELL_PUT" and spot <= ul_sl:
            logger.info(f"{asset}: underlying SL hit — spot={spot:.2f} <= sl={ul_sl:.2f}")
            self._close(asset, "UNDERLYING_SL_HIT")
            return

        # ── 2. Pre-expiry hard cutoff ─────────────────────────────────────
        expiry_str = pos.get("expiry", "")
        if expiry_str:
            try:
                # settlement_time stored as "2026-05-15" (date part only)
                expiry_dt = datetime.strptime(expiry_str, "%Y-%m-%d").replace(
                    hour=12, minute=0, second=0, tzinfo=timezone.utc
                )
                mins_to_expiry = (expiry_dt - now).total_seconds() / 60
                if mins_to_expiry <= PRE_EXPIRY_CLOSE_MINS:
                    logger.info(
                        f"{asset}: pre-expiry close — {mins_to_expiry:.0f} min to expiry "
                        f"(<= {PRE_EXPIRY_CLOSE_MINS} min cutoff)"
                    )
                    self._close(asset, "PRE_EXPIRY")
                    return
            except ValueError:
                pass

        # ── 3. 4-candle trail decision ────────────────────────────────────
        trailing = pos.get("trailing", False)
        if not trailing and pos["candles_held"] >= MAX_HOLD_CANDLES:
            curr_prem = self.client.get_mark_price(pos["product_id"])
            entry_prem = pos["entry_premium"]
            if entry_prem > 0 and curr_prem > 0:
                decay_ratio = curr_prem / entry_prem   # <1 means decayed (profit)
                if decay_ratio <= TRAIL_THRESHOLD_PCT:
                    # Profitable enough — trail to expiry
                    logger.info(
                        f"{asset}: trailing to expiry — premium decayed "
                        f"{(1-decay_ratio)*100:.1f}% "
                        f"(entry={entry_prem:.4f} now={curr_prem:.4f}). "
                        f"Bracket TP/SL active on exchange."
                    )
                    pos["trailing"] = True
                else:
                    # Not profitable enough at 4h — close now
                    logger.info(
                        f"{asset}: 4-candle close — only {(1-decay_ratio)*100:.1f}% decay "
                        f"(need ≥{(1-TRAIL_THRESHOLD_PCT)*100:.0f}% to trail). Closing."
                    )
                    self._close(asset, "TIME_EXIT")
                    return
            else:
                # Can't read premium — safe close
                logger.warning(f"{asset}: cannot read premium at 4h, closing safely")
                self._close(asset, "TIME_EXIT_NO_PREMIUM")
                return

        # ── 4. Sync: detect bracket TP/SL already closed on exchange ──────
        self._sync_position(asset)

    # ── Sync: detect if exchange already closed the position ─────────────
    def _sync_position(self, asset: str):
        """Check if the exchange already closed via bracket TP/SL."""
        pos = self.positions.get(asset)
        if not pos:
            return
        pid    = pos["product_id"]
        orders = self.client.get_open_orders(pid)
        # If no open orders remain for this product, bracket orders were filled
        # (TP or SL hit on exchange). Remove position from tracking.
        if not orders:
            # Double-check via positions endpoint
            exchange_positions = self.client.get_positions_for_product(pid)
            if not exchange_positions:
                logger.info(
                    f"{asset}: bracket order already closed position on exchange "
                    f"(TP/SL hit). Removing from tracking."
                )
                del self.positions[asset]

    # ── Force-exit on momentum fade / kill switch ─────────────────────────
    def close_position(self, asset: str, reason: str):
        self._close(asset, reason)

    def close_all(self):
        for asset in list(self.positions.keys()):
            self._close(asset, "KILL_SWITCH")

    def _close(self, asset: str, reason: str):
        """Cancel all open bracket orders, then close the position at market."""
        pos = self.positions.get(asset)
        if not pos:
            return
        pid = pos["product_id"]
        qty = pos["qty"]

        # Verify position still exists on exchange before placing closing order.
        # If exchange already closed it (bracket TP/SL), placing a buy would
        # create an accidental long position.
        exchange_pos = self.client.get_positions_for_product(pid)
        if not exchange_pos:
            logger.info(f"{asset}: position already closed on exchange — skipping close order ({reason})")
            del self.positions[asset]
            return

        # Step 1: cancel ALL open orders for this product (bracket TP/SL children)
        self._cancel_all_orders(pid)

        # Step 2: close the short position by buying back at market
        logger.info(f"{asset}: closing {pos['symbol']} reason={reason}")
        try:
            r = self.client.place_order(
                product_id      = pid,
                side            = "buy",
                size            = qty,
                order_type      = "market_order",
                client_order_id = f"CLOSE{asset[:1]}{int(datetime.now(timezone.utc).timestamp())}"[:32],
            )
            if r.get("success"):
                logger.info(f"{asset}: closed OK — {reason}")
                del self.positions[asset]
            else:
                logger.error(f"{asset}: close FAILED {r}")
        except Exception as e:
            logger.exception(f"{asset}: close exception: {e}")

    def _cancel_all_orders(self, product_id: int):
        """Cancel every open order for a product (bracket children etc.)."""
        try:
            orders = self.client.get_open_orders(product_id)
            for o in orders:
                oid = o.get("id")
                if oid:
                    result = self.client.cancel_order(oid, product_id)
                    state  = result.get("result", {}).get("state", "?")
                    logger.info(f"Cancelled order {oid} → {state}")
        except Exception as e:
            logger.warning(f"cancel_all_orders error (product={product_id}): {e}")

    def sync_on_startup(self, assets: list):
        """On bot start, query exchange for any open positions and register them.
        Prevents duplicate entries after a restart while a trade is still open."""
        import requests, time as _t, hmac, hashlib
        try:
            ts  = str(int(_t.time()))
            path = '/v2/positions/margined'
            msg = 'GET' + ts + path
            sig = hmac.new(self.client.api_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
            headers = {'api-key': self.client.api_key, 'signature': sig, 'timestamp': ts}
            resp = requests.get(self.client.base_url + path, headers=headers, timeout=15)
            data = resp.json()
            for p in data.get('result', []):
                size = float(p.get('size', 0))
                if size == 0:
                    continue
                sym = p.get('product_symbol', '')
                # Match to our assets
                asset = None
                for a in assets:
                    if f'-{a}-' in sym:
                        asset = a
                        break
                if not asset or asset in self.positions:
                    continue
                side = 'SELL_CALL' if sym.startswith('C-') else 'SELL_PUT'
                entry = float(p.get('entry_price') or 0)
                pid   = int(p.get('product_id', 0))
                qty   = abs(int(size))
                # Parse expiry from symbol e.g. C-ETH-2300-160526 → 2026-05-16
                try:
                    parts = sym.split('-')
                    d = parts[-1]  # e.g. 160526
                    expiry = f"20{d[4:6]}-{d[2:4]}-{d[0:2]}"
                except Exception:
                    expiry = ''
                self.positions[asset] = {
                    'product_id':    pid,
                    'symbol':        sym,
                    'side':          side,
                    'strike':        float(sym.split('-')[2]) if len(sym.split('-')) > 2 else 0,
                    'qty':           qty,
                    'entry_premium': entry,
                    'tp_premium':    round(entry * 0.5, 4),
                    'sl_price':      999999 if side == 'SELL_CALL' else 0,
                    'bracket_sl':    round(entry * 3.0, 4),
                    'order_id':      None,
                    'entry_time':    datetime.now(timezone.utc).isoformat(),
                    'candles_held':  0,
                    'trailing':      True,  # let exchange brackets manage it
                    'expiry':        expiry,
                }
                logger.info(f"STARTUP SYNC: recovered {sym} size={size} entry={entry} for {asset}")
        except Exception as e:
            logger.warning(f"sync_on_startup error: {e}")

    def has_position(self, asset: str) -> bool:
        return asset in self.positions

    def get_position(self, asset: str) -> Optional[Dict]:
        return self.positions.get(asset)
