"""
Core strategy module.
Handles: IST time helpers, expiry selection, strike calculation,
IV Rank, option selection, margin-based sizing, and trade execution
with fill confirmation.
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import (
    STRIKE_PCT, CAPITAL_SPLIT, IV_RANK_MIN,
    IV_HISTORY_FILE, POSITIONS_FILE,
    EXPIRY_CUTOFF_SAME_DAY_H, EXPIRY_CUTOFF_SAME_DAY_M,
    EXPIRY_CUTOFF_NEXT_DAY_H, EXPIRY_CUTOFF_NEXT_DAY_M,
    IST_OFFSET_HOURS, IST_OFFSET_MINS,
    TP_PCT, SL_MULT, MIN_LOTS,
)
from delta_api import (
    get_ticker, get_option_chain, get_usdt_balance, get_product,
    place_limit_order, place_stop_limit_order, get_order, cancel_order,
    get_order_book,
)

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=IST_OFFSET_HOURS, minutes=IST_OFFSET_MINS))

# How long to wait for an entry order to fill before cancelling
FILL_TIMEOUT_SECS  = 120   # 2 minutes
FILL_POLL_INTERVAL = 10    # check every 10 seconds


# ── Time helpers ──────────────────────────────────────────────────────────────

def get_ist_now() -> datetime:
    return datetime.now(IST)


def get_expiry_sequence(ist_now: datetime) -> list:
    """
    Return candidate expiry dates [primary, fallback] as 'DD-MM-YYYY'.
    Before 11:30 AM → D+0 ; 11:30–15:30 → D+1 ; after 15:30 → D+1/D+2
    """
    mins = ist_now.hour * 60 + ist_now.minute
    same = EXPIRY_CUTOFF_SAME_DAY_H * 60 + EXPIRY_CUTOFF_SAME_DAY_M
    nxt  = EXPIRY_CUTOFF_NEXT_DAY_H * 60 + EXPIRY_CUTOFF_NEXT_DAY_M

    today = ist_now.date()
    d0 = today.strftime("%d-%m-%Y")
    d1 = (today + timedelta(days=1)).strftime("%d-%m-%Y")
    d2 = (today + timedelta(days=2)).strftime("%d-%m-%Y")

    if mins < same:
        return [d0, d1]
    elif mins < nxt:
        return [d1, d2]
    else:
        return [d1, d2]


# ── Spot price ────────────────────────────────────────────────────────────────

def get_spot_price(asset: str) -> Optional[float]:
    perp = {"BTC": "BTCUSD", "ETH": "ETHUSD"}
    try:
        return float(get_ticker(perp[asset]).get("mark_price", 0) or 0)
    except Exception as exc:
        logger.error("Spot price fetch failed for %s: %s", asset, exc)
        return None


# ── Strike calculation ────────────────────────────────────────────────────────

def calculate_strike(spot: float, asset: str, option_type: str) -> float:
    """Round OTM strike to nearest clean grid (BTC: $100, ETH: $10)."""
    pct  = STRIKE_PCT[asset][option_type.upper()]
    raw  = spot * (1 - pct / 100) if option_type.upper() == "PUT" else spot * (1 + pct / 100)
    grid = 100 if asset == "BTC" else 10
    return round(raw / grid) * grid


# ── Tick-size rounding ────────────────────────────────────────────────────────

def round_to_tick(price: float, tick_size: float) -> float:
    """Round a price DOWN to the nearest valid tick (for limit sell orders)."""
    if tick_size <= 0:
        return price
    decimals = max(0, -int(math.floor(math.log10(tick_size))))
    return round(math.floor(price / tick_size) * tick_size, decimals)


# ── IV Rank ───────────────────────────────────────────────────────────────────

def _load_iv_history() -> dict:
    try:
        if os.path.exists(IV_HISTORY_FILE):
            with open(IV_HISTORY_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_iv_history(history: dict):
    with open(IV_HISTORY_FILE, "w") as f:
        json.dump(history, f)


def _update_iv_history(asset: str, iv: float):
    history = _load_iv_history()
    if asset not in history:
        history[asset] = []
    today = get_ist_now().strftime("%Y-%m-%d")
    history[asset] = [e for e in history[asset] if e["date"] != today]
    history[asset].append({"date": today, "iv": iv})
    history[asset] = sorted(history[asset], key=lambda x: x["date"])[-365:]
    _save_iv_history(history)


def calculate_iv_rank(asset: str, current_iv: float) -> float:
    """IV Rank = (current - 52w_low) / (52w_high - 52w_low) × 100."""
    _update_iv_history(asset, current_iv)
    ivs = [e["iv"] for e in _load_iv_history().get(asset, [])]
    if len(ivs) < 10:
        return current_iv * 100 if current_iv <= 2.0 else current_iv
    lo, hi = min(ivs), max(ivs)
    return 50.0 if hi == lo else (current_iv - lo) / (hi - lo) * 100


# ── Option selection ──────────────────────────────────────────────────────────

def _check_order_book_liquidity(symbol: str) -> dict:
    """
    Fetch order book and return liquidity metrics.
    Returns {'bid_depth': float, 'ask_depth': float, 'liquid': bool}.
    'liquid' is True when total bid size across top 5 levels >= 5 contracts.
    """
    try:
        book = get_order_book(symbol, depth=5)
        bids = book.get("buy", [])
        asks = book.get("sell", [])
        bid_depth = sum(float(row[1]) for row in bids if len(row) >= 2)
        ask_depth = sum(float(row[1]) for row in asks if len(row) >= 2)
        liquid = bid_depth >= 5
        logger.info("[LIQ] %s | bid_depth: %.0f | ask_depth: %.0f | liquid: %s",
                    symbol, bid_depth, ask_depth, liquid)
        return {"bid_depth": bid_depth, "ask_depth": ask_depth, "liquid": liquid}
    except Exception as exc:
        logger.warning("[LIQ] Order book fetch failed for %s: %s — assuming liquid", symbol, exc)
        return {"bid_depth": 0, "ask_depth": 0, "liquid": True}


def find_best_option(asset: str, option_type: str, target_strike: float, expiry_date: str) -> Optional[dict]:
    """
    Scan the live option chain, pick the most liquid strike closest to target_strike
    (ATM when STRIKE_PCT=0), and enrich the result with product-level margin/sizing data.

    Liquidity filter (applied in order):
      1. Prefer options with an active bid (best_bid > 0).
      2. Among those, pick the strike nearest to target_strike.
      3. Verify order book depth >= 5 contracts on the buy side; warn if not.
    """
    try:
        chain = get_option_chain(asset, expiry_date)
        if not chain:
            logger.warning("Empty option chain for %s %s", asset, expiry_date)
            return None

        otype_filter = "call_options" if option_type.upper() == "CALL" else "put_options"
        opts = [o for o in chain if o.get("contract_type") == otype_filter]
        if not opts:
            logger.warning("No %s options for %s %s", option_type, asset, expiry_date)
            return None

        # Prefer options where a live bid exists (real liquidity)
        liquid_opts = [
            o for o in opts
            if float(o.get("quotes", {}).get("best_bid", 0) or 0) > 0
        ]
        pool = liquid_opts if liquid_opts else opts
        if not liquid_opts:
            logger.warning("[LIQ] No %s options with active bid for %s %s — using all", option_type, asset, expiry_date)

        best = min(pool, key=lambda o: abs(float(o.get("strike_price", 0)) - target_strike))

        quotes         = best.get("quotes", {})
        mark           = float(best.get("mark_price", 0) or 0)
        bid            = float(quotes.get("best_bid", 0) or 0)
        ask            = float(quotes.get("best_ask", 0) or mark)
        mid_price      = (bid + ask) / 2 if bid > 0 and ask > 0 else mark
        ask_iv         = float(quotes.get("ask_iv", 0) or 0)
        greeks         = best.get("greeks", {})
        delta_val      = abs(float(greeks.get("delta", 0.18) or 0.18))
        contract_value = float(best.get("contract_value", 0) or 0)
        spot_price     = float(best.get("spot_price", 0) or 0)

        if mid_price <= 0:
            logger.warning("Zero mid-price for %s", best.get("symbol"))
            return None

        # Log spread quality
        if bid > 0 and ask > 0:
            spread_pct = (ask - bid) / mid_price * 100
            logger.info("[LIQ] %s | bid: %.4f | ask: %.4f | spread: %.1f%%",
                        best.get("symbol"), bid, ask, spread_pct)
            if spread_pct > 60:
                logger.warning("[LIQ] Wide spread %.1f%% on %s — fill may be difficult", spread_pct, best.get("symbol"))

        # Verify order book depth
        liq = _check_order_book_liquidity(best.get("symbol", ""))
        if not liq["liquid"]:
            logger.warning("[LIQ] Low bid depth (%.0f contracts) on %s — proceeding with caution", liq["bid_depth"], best.get("symbol"))

        # Fetch product spec for tick_size and initial_margin (margin per lot)
        symbol = best.get("symbol", "")
        try:
            product = get_product(symbol)
            tick_size      = float(product.get("tick_size", 0.1) or 0.1)
            initial_margin = float(product.get("initial_margin", 1.0) or 1.0)
        except Exception:
            tick_size      = 0.1 if asset == "BTC" else 0.01
            initial_margin = 0.5 if asset == "BTC" else 1.0

        return {
            "symbol":         symbol,
            "product_id":     best.get("product_id") or best.get("id"),
            "strike":         float(best.get("strike_price", target_strike)),
            "mark_price":     mark,
            "mid_price":      mid_price,
            "bid":            bid,
            "ask":            ask,
            "iv":             ask_iv,
            "delta":          delta_val,
            "expiry_date":    expiry_date,
            "contract_value": contract_value,  # e.g. 0.001 BTC per contract
            "spot_price":     spot_price,
            "tick_size":      tick_size,        # minimum price increment
            "initial_margin": initial_margin,   # margin % (e.g. 0.5 = 0.5% of notional)
        }

    except Exception as exc:
        logger.error("find_best_option error (%s %s %s): %s", asset, option_type, expiry_date, exc)
        return None


# ── Position sizing ───────────────────────────────────────────────────────────

def calculate_contracts(allocated_capital: float, total_equity: float,
                        asset: str, option: dict) -> Optional[int]:
    """
    Uses max available leverage, capped by the 2% risk rule.

    Two limits are computed and the LOWER is taken:

      1. Risk limit  → floor( risk_budget / sl_per_contract )
         Ensures max loss stays ≤ 2% of total equity per asset allocation.

      2. Margin limit → floor( allocated_capital / margin_per_contract )
         Ensures we don't exceed the margin available for this asset.

    Returns None if even 1 contract violates both limits.
    """
    from config import RISK_PCT, SL_MULT, CAPITAL_SPLIT

    # ── Margin limit (exchange constraint) ──────────────────────────────
    initial_margin_pct = option["initial_margin"] / 100   # e.g. 0.5% → 0.005
    contract_value     = option["contract_value"]          # e.g. 0.001 BTC
    spot_price         = option["spot_price"]

    if contract_value <= 0 or spot_price <= 0 or initial_margin_pct <= 0:
        logger.error("Invalid option data for sizing: cv=%s sp=%s im=%s",
                     contract_value, spot_price, initial_margin_pct)
        return None

    notional_per_lot  = contract_value * spot_price
    margin_per_lot    = initial_margin_pct * notional_per_lot
    margin_limit      = int(math.floor(allocated_capital / margin_per_lot)) if margin_per_lot > 0 else 0

    # ── Risk limit ───────────────────────────────────────────────────────
    # Option prices from Delta API are in USD/underlying (USD/BTC or USD/ETH).
    # Multiply by contract_value to get actual USD at risk per lot.
    risk_budget       = total_equity * CAPITAL_SPLIT[asset] * RISK_PCT / 100
    sl_per_lot        = SL_MULT * option["mid_price"] * contract_value   # USD per lot
    risk_limit        = int(math.floor(risk_budget / sl_per_lot)) if sl_per_lot > 0 else 0

    # Take the lower of the two ceilings, with a hard minimum when margin allows
    contracts = min(margin_limit, risk_limit)

    if contracts < MIN_LOTS and margin_limit >= MIN_LOTS:
        # Risk budget is too tight but exchange margin allows it — take minimum lots
        logger.warning(
            "Sizing [%s]: risk_limit=%d < MIN_LOTS=%d, but margin_limit=%d allows it — "
            "taking %d lot(s). SL/lot=$%.4f on $%.2f equity.",
            asset, risk_limit, MIN_LOTS, margin_limit, MIN_LOTS, sl_per_lot, total_equity,
        )
        contracts = MIN_LOTS

    logger.info(
        "Sizing [%s] | equity: $%.2f | alloc: $%.2f | "
        "margin/lot: $%.4f → margin_limit: %d | "
        "risk_budget: $%.2f | sl/lot(USD): $%.6f → risk_limit: %d | "
        "final: %d contracts",
        asset, total_equity, allocated_capital,
        margin_per_lot, margin_limit,
        risk_budget, sl_per_lot, risk_limit,
        contracts,
    )

    if contracts < 1:
        if margin_limit < 1:
            logger.warning(
                "SKIP [%s]: margin too small — need $%.4f/lot, have $%.2f allocated.",
                asset, margin_per_lot, allocated_capital,
            )
        else:
            logger.warning(
                "SKIP [%s]: risk too tight — SL=$%.4f/lot vs budget=$%.2f.",
                asset, sl_per_lot, risk_budget,
            )
        return None

    return contracts


# ── Fill confirmation ─────────────────────────────────────────────────────────

def wait_for_fill(order_id: int, product_id: int, timeout: int = FILL_TIMEOUT_SECS) -> Optional[dict]:
    """
    Poll the order status until it is fully filled or timeout is reached.
    Returns {'fill_qty': int, 'avg_price': float} on success, None on timeout/cancel.
    On timeout: cancels the order before returning None.
    """
    deadline = time.time() + timeout
    logger.info("Waiting for fill on order %s (timeout %ds)…", order_id, timeout)

    while time.time() < deadline:
        time.sleep(FILL_POLL_INTERVAL)
        try:
            order = get_order(order_id)
            state     = order.get("state", "")
            filled    = int(order.get("size", 0)) - int(order.get("unfilled_size", 0))
            avg_price = float(order.get("average_fill_price") or 0)

            logger.info("Order %s | state: %s | filled: %s | avg_price: %s",
                        order_id, state, filled, avg_price)

            if state == "closed" or (filled > 0 and int(order.get("unfilled_size", 1)) == 0):
                return {"fill_qty": filled, "avg_price": avg_price}

            if state in ("cancelled", "rejected"):
                logger.warning("Order %s was %s by exchange.", order_id, state)
                return None

        except Exception as exc:
            logger.error("Error polling order %s: %s", order_id, exc)

    # Timeout — cancel the unfilled order
    logger.warning("Order %s not filled after %ds — cancelling.", order_id, timeout)
    try:
        cancel_order(order_id, product_id)
        logger.info("Order %s cancelled.", order_id)
    except Exception as exc:
        logger.error("Failed to cancel timed-out order %s: %s", order_id, exc)
    return None


# ── Position guard ────────────────────────────────────────────────────────────

def has_open_position(asset: str) -> bool:
    try:
        if not os.path.exists(POSITIONS_FILE):
            return False
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
        return any(p.get("asset") == asset for p in positions)
    except Exception:
        return False


def get_position_signal(asset: str) -> Optional[str]:
    """Return the 'signal' field (SELL_PUT / SELL_CALL) of the open position for asset, or None."""
    try:
        if not os.path.exists(POSITIONS_FILE):
            return None
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
        for p in positions:
            if p.get("asset") == asset:
                return p.get("signal")
        return None
    except Exception:
        return None


# ── Trade execution ───────────────────────────────────────────────────────────

def execute_trade(asset: str, signal: str) -> Optional[dict]:
    """
    Full trade lifecycle:
      1. Guards: time window, existing position
      2. Spot price → calculate OTM strike
      3. Scan expiry sequence → find best option on chain
      4. IV Rank filter
      5. Margin-based sizing (100% of allocated capital)
      6. Round limit price to tick_size
      7. Place limit sell → wait for confirmed fill (or cancel on timeout)
      8. Return filled trade dict for position_manager to track
    """
    ist_now = get_ist_now()
    logger.info("=== execute_trade: %s %s @ %s IST ===", asset, signal, ist_now.strftime("%H:%M:%S"))

    if has_open_position(asset):
        logger.info("Skip: open position already exists for %s", asset)
        return None

    spot = get_spot_price(asset)
    if not spot or spot <= 0:
        logger.error("Cannot get spot price for %s", asset)
        return None

    option_type   = "PUT" if signal == "SELL_PUT" else "CALL"
    target_strike = calculate_strike(spot, asset, option_type)

    # Try expiry dates in priority order
    option = None
    for exp in get_expiry_sequence(ist_now):
        option = find_best_option(asset, option_type, target_strike, exp)
        if option:
            break

    if not option:
        logger.error("No viable option found for %s %s near strike %.0f", asset, option_type, target_strike)
        return None

    # IV Rank filter
    iv_rank = calculate_iv_rank(asset, option["iv"])
    logger.info("[%s] IV: %.4f | IV Rank: %.1f%%", asset, option["iv"], iv_rank)
    if iv_rank < IV_RANK_MIN:
        logger.warning("IV Rank %.1f%% < %.1f%% minimum — SKIP", iv_rank, IV_RANK_MIN)
        return None

    # Account equity and sizing
    equity = get_usdt_balance()
    if equity <= 0:
        logger.error("Zero or unavailable account balance")
        return None

    allocated = equity * CAPITAL_SPLIT[asset]
    contracts = calculate_contracts(allocated, equity, asset, option)
    if contracts is None:
        return None

    # Limit price = best bid rounded DOWN to nearest tick (we are the seller)
    tick_size   = option["tick_size"]
    raw_price   = option["bid"] if option["bid"] > 0 else option["mid_price"] * 0.98
    limit_price = round_to_tick(raw_price, tick_size)

    if limit_price <= 0:
        logger.error("Computed limit price is zero or negative — skip")
        return None

    logger.info(
        "ORDER → SELL %s %s | qty: %d | limit: %s | strike: %.0f | expiry: %s | IV Rank: %.1f%%",
        option_type, option["symbol"], contracts, limit_price,
        option["strike"], option["expiry_date"], iv_rank,
    )

    # Place limit sell order
    try:
        order = place_limit_order(
            product_symbol=option["symbol"],
            side="sell",
            size=contracts,
            limit_price=limit_price,
        )
    except Exception as exc:
        logger.error("Order placement failed: %s", exc)
        return None

    order_id   = order.get("id")
    product_id = order.get("product_id") or option.get("product_id")

    if not order_id:
        logger.error("No order ID returned from exchange: %s", order)
        return None

    # Wait until the order is confirmed filled (or cancel if it times out)
    fill = wait_for_fill(order_id, product_id)
    if fill is None:
        logger.warning("Entry order not filled — position NOT opened for %s", asset)
        return None

    entry_premium = fill["avg_price"] if fill["avg_price"] > 0 else limit_price
    filled_qty    = fill["fill_qty"] if fill["fill_qty"] > 0 else contracts

    tp_price = round_to_tick(entry_premium * TP_PCT,  tick_size)
    sl_price = round_to_tick(entry_premium * SL_MULT, tick_size)

    # Place exchange-side TP and SL orders so protection survives a bot restart
    tp_order_id = None
    sl_order_id = None

    try:
        tp_ord = place_limit_order(
            product_symbol=option["symbol"],
            side="buy",
            size=filled_qty,
            limit_price=tp_price,
            reduce_only=True,
        )
        tp_order_id = tp_ord.get("id")
        logger.info("TP limit-buy placed: id=%s @ $%.4f", tp_order_id, tp_price)
    except Exception as exc:
        logger.warning("Could not place exchange TP order: %s — software monitor will handle TP", exc)

    try:
        # SL stop-limit: stop triggers at sl_price, limit 5 ticks above to ensure fill
        sl_limit = round_to_tick(sl_price * 1.05, tick_size)
        sl_ord = place_stop_limit_order(
            product_symbol=option["symbol"],
            side="buy",
            size=filled_qty,
            stop_price=sl_price,
            limit_price=sl_limit,
            reduce_only=True,
        )
        sl_order_id = sl_ord.get("id")
        logger.info("SL stop-limit placed: id=%s trigger=$%.4f limit=$%.4f", sl_order_id, sl_price, sl_limit)
    except Exception as exc:
        logger.warning("Could not place exchange SL order: %s — software monitor will handle SL", exc)

    trade = {
        "asset":         asset,
        "signal":        signal,
        "option_type":   option_type,
        "symbol":        option["symbol"],
        "product_id":    product_id,
        "strike":        option["strike"],
        "expiry_date":   option["expiry_date"],
        "entry_premium":  entry_premium,
        "tp_price":       tp_price,
        "sl_price":       sl_price,
        "contracts":      filled_qty,
        "order_id":       order_id,
        "tp_order_id":    tp_order_id,
        "sl_order_id":    sl_order_id,
        "spot_at_entry":  spot,
        "iv_rank":        round(iv_rank, 2),
        "tick_size":      tick_size,
        "contract_value": option["contract_value"],
        "side":           "sell",
        "timestamp":      ist_now.isoformat(),
    }
    logger.info("Position opened: %s", json.dumps(trade, indent=2))
    return trade
