"""
Position Manager.
Runs a background monitor loop (every 60 s) that checks every open position
against TP / SL / emergency / time-exit rules and closes when triggered.
Positions are persisted in open_positions.json so they survive restarts.
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import (
    MONITOR_INTERVAL, IST_OFFSET_HOURS, IST_OFFSET_MINS,
    POSITIONS_FILE, TRADES_LOG_FILE, TP_PCT, SL_MULT, EMERGENCY_PCT,
    CLOSE_BEFORE_EXPIRY_MINS, DISABLE_SL, DISABLE_EMERGENCY,
)
from delta_api import get_ticker, get_positions, place_market_order, get_order, cancel_order

logger = logging.getLogger(__name__)

IST   = timezone(timedelta(hours=IST_OFFSET_HOURS, minutes=IST_OFFSET_MINS))
_lock = threading.Lock()


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_positions() -> list:
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_positions(positions: list):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)


def add_position(trade: dict):
    """Persist a newly opened trade to the positions file."""
    with _lock:
        positions = _load_positions()
        # Deduplicate: one slot per (asset, option_type)
        positions = [p for p in positions if p.get("symbol") != trade.get("symbol")]
        positions.append(trade)
        _save_positions(positions)
    logger.info("[PM] Position added: %s", trade.get("symbol"))


def _remove_position(symbol: str):
    with _lock:
        positions = [p for p in _load_positions() if p.get("symbol") != symbol]
        _save_positions(positions)
    logger.info("[PM] Position removed: %s", symbol)


# ── Trade logging ─────────────────────────────────────────────────────────────

def _log_trade_close(trade: dict, reason: str, exit_price: float, pnl: float):
    entry = {
        "timestamp":     datetime.now(IST).isoformat(),
        "action":        f"CLOSE_{reason}",
        "symbol":        trade.get("symbol"),
        "asset":         trade.get("asset"),
        "signal":        trade.get("signal"),
        "entry_premium": trade.get("entry_premium"),
        "exit_price":    exit_price,
        "pnl_usdt":      round(pnl, 4),
        "contracts":     trade.get("contracts"),
        "strike":        trade.get("strike"),
        "expiry_date":   trade.get("expiry_date"),
        "iv_rank":       trade.get("iv_rank"),
    }
    with open(TRADES_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.info("[PM] Trade closed | %s | Reason: %s | PnL: %.4f USDT", trade.get("symbol"), reason, pnl)


# ── Market data helpers ───────────────────────────────────────────────────────

def _get_current_premium(symbol: str) -> float:
    try:
        ticker = get_ticker(symbol)
        quotes = ticker.get("quotes", {})
        bid    = float(quotes.get("best_bid", 0) or 0)
        ask    = float(quotes.get("best_ask", 0) or 0)
        mark   = float(ticker.get("mark_price", 0) or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return mark
    except Exception as exc:
        logger.error("[PM] Cannot get premium for %s: %s", symbol, exc)
        return 0.0


def _get_spot_from_ticker(symbol: str) -> float:
    try:
        return float(get_ticker(symbol).get("spot_price", 0) or 0)
    except Exception:
        return 0.0


# ── Cancel exchange bracket orders ────────────────────────────────────────────

def _cancel_bracket_orders(trade: dict):
    """Cancel any exchange-side TP/SL orders tied to this position."""
    product_id = trade.get("product_id")
    for key, label in (("tp_order_id", "TP"), ("sl_order_id", "SL")):
        oid = trade.get(key)
        if not oid:
            continue
        try:
            state = get_order(oid).get("state", "")
            if state in ("open", "pending"):
                cancel_order(oid, product_id)
                logger.info("[PM] Cancelled exchange %s order %s for %s", label, oid, trade["symbol"])
            else:
                logger.info("[PM] Exchange %s order %s already in state '%s'", label, oid, state)
        except Exception as exc:
            logger.warning("[PM] Could not cancel exchange %s order %s: %s", label, oid, exc)


def _exchange_already_closed(trade: dict) -> Optional[str]:
    """
    Check if either the TP or SL exchange order was already filled by the exchange.
    Returns 'TP'/'SL' if filled, None otherwise.
    """
    for key, label in (("tp_order_id", "TP"), ("sl_order_id", "SL")):
        oid = trade.get(key)
        if not oid:
            continue
        try:
            order = get_order(oid)
            state = order.get("state", "")
            filled = int(order.get("size", 0)) - int(order.get("unfilled_size", 0))
            if state == "closed" or (filled > 0 and int(order.get("unfilled_size", 1)) == 0):
                logger.info("[PM] Exchange %s order %s was filled — position closed by exchange", label, oid)
                return label
        except Exception:
            pass
    return None


# ── Close position ────────────────────────────────────────────────────────────

def _close_position(trade: dict, reason: str) -> bool:
    symbol         = trade["symbol"]
    contracts      = trade["contracts"]
    entry_px       = float(trade.get("entry_premium", 0))
    contract_value = float(trade.get("contract_value", 1))

    # Cancel any open exchange bracket orders before placing market close
    _cancel_bracket_orders(trade)

    current_px = _get_current_premium(symbol)
    # Prices are in USD/underlying; multiply by contract_value to get USD P&L
    pnl = (entry_px - current_px) * contracts * contract_value

    logger.info(
        "[PM] CLOSING %s | Reason: %-20s | Entry: %.6f | Current: %.6f | PnL: %.6f USDT",
        symbol, reason, entry_px, current_px, pnl,
    )
    try:
        place_market_order(product_symbol=symbol, side="buy", size=contracts, reduce_only=True)
        _log_trade_close(trade, reason, current_px, pnl)
        _remove_position(symbol)
        return True
    except Exception as exc:
        logger.error("[PM] Failed to close %s: %s", symbol, exc)
        return False


# ── Exit checks ───────────────────────────────────────────────────────────────

def _is_time_to_close(trade: dict) -> bool:
    """True if we are within CLOSE_BEFORE_EXPIRY_MINS of expiry on expiry day."""
    expiry_str = trade.get("expiry_date", "")
    try:
        d, m, y = expiry_str.split("-")
        expiry_dt = datetime(int(y), int(m), int(d), 17, 30, tzinfo=IST)  # 5:30 PM IST
        close_by  = expiry_dt - timedelta(minutes=CLOSE_BEFORE_EXPIRY_MINS)
        return datetime.now(IST) >= close_by
    except Exception:
        return False


def _check_single_position(trade: dict) -> bool:
    """
    Evaluate all exit conditions for one open position.
    Returns True if position was closed.
    """
    symbol    = trade["symbol"]
    entry_px  = float(trade.get("entry_premium", 0))
    tp_price  = float(trade.get("tp_price", entry_px * TP_PCT))
    sl_price  = float(trade.get("sl_price", entry_px * SL_MULT))
    strike    = float(trade.get("strike", 0))
    opt_type  = trade.get("option_type", "").upper()

    # 0. Check if exchange already filled a bracket TP/SL order
    filled_by = _exchange_already_closed(trade)
    if filled_by:
        contract_value = float(trade.get("contract_value", 1))
        exit_px = float(trade.get("tp_price" if filled_by == "TP" else "sl_price", 0))
        pnl = (entry_px - exit_px) * trade["contracts"] * contract_value
        _log_trade_close(trade, f"EXCHANGE_{filled_by}", exit_px, pnl)
        _remove_position(symbol)
        return True

    # 1. Time-based exit (30 min before expiry)
    if _is_time_to_close(trade):
        return _close_position(trade, "TIME_EXIT")

    current_px = _get_current_premium(symbol)
    if current_px <= 0:
        logger.warning("[PM] Zero premium for %s — skipping this tick", symbol)
        return False

    logger.info(
        "[PM] %s | Entry: %.6f | Now: %.6f | TP≤%.6f | SL≥%.6f",
        symbol, entry_px, current_px, tp_price, sl_price,
    )

    # 2. Take Profit: premium decayed to 50 %
    if current_px <= tp_price:
        return _close_position(trade, "TAKE_PROFIT")

    # 3. Stop Loss: premium inflated to 2× entry (skipped when DISABLE_SL=True)
    if not DISABLE_SL and current_px >= sl_price:
        return _close_position(trade, "STOP_LOSS")
    if DISABLE_SL and current_px >= sl_price:
        logger.info("[PM] SL disabled — would have triggered at %.6f (current: %.6f)", sl_price, current_px)

    # 4. Emergency: spot has moved through the strike (skipped when DISABLE_EMERGENCY=True)
    if not DISABLE_EMERGENCY and strike > 0 and EMERGENCY_PCT > 0:
        spot = _get_spot_from_ticker(symbol)
        if spot > 0:
            if opt_type == "CALL" and spot >= strike * (1 + EMERGENCY_PCT / 100):
                logger.warning("[PM] EMERGENCY: spot %.2f > call strike %.2f", spot, strike)
                return _close_position(trade, "EMERGENCY_CALL")
            if opt_type == "PUT" and spot <= strike * (1 - EMERGENCY_PCT / 100):
                logger.warning("[PM] EMERGENCY: spot %.2f < put strike %.2f", spot, strike)
                return _close_position(trade, "EMERGENCY_PUT")

    return False


# ── Exchange sync ─────────────────────────────────────────────────────────────

def _sync_with_exchange():
    """
    Reconcile open_positions.json against live Delta Exchange positions.
    Any position we track that no longer exists on the exchange (manually closed,
    expired, or filled externally) is logged to trades.log and removed.
    """
    try:
        live = get_positions()
    except Exception as exc:
        logger.warning("[PM] Sync skipped — could not fetch live positions: %s", exc)
        return

    live_symbols = {
        p.get("product_symbol")
        for p in live
        if abs(float(p.get("size", 0) or 0)) > 0
    }

    with _lock:
        positions = _load_positions()
        stale = [p for p in positions if p.get("symbol") not in live_symbols]

    for p in stale:
        symbol         = p.get("symbol")
        entry_px       = float(p.get("entry_premium", 0))
        contract_value = float(p.get("contract_value", 1))
        contracts      = p.get("contracts", 0)
        try:
            exit_px = float(get_ticker(symbol).get("mark_price", 0) or 0)
        except Exception:
            exit_px = 0.0
        pnl = (entry_px - exit_px) * contracts * contract_value
        _log_trade_close(p, "EXTERNAL_CLOSE", exit_px, pnl)
        _remove_position(symbol)
        logger.warning(
            "[PM] SYNC: %s not found on exchange — removed from tracker (externally closed). "
            "Estimated PnL: %.4f USDT", symbol, pnl,
        )


# ── Monitor loop ──────────────────────────────────────────────────────────────

def run_monitor_loop():
    """
    Infinite background loop. Checks every open position every MONITOR_INTERVAL seconds.
    Designed to run as a daemon thread.
    """
    logger.info("[PM] Position monitor started (interval: %ds)", MONITOR_INTERVAL)
    while True:
        try:
            # Sync first: drop any positions closed outside the bot
            _sync_with_exchange()

            with _lock:
                positions = _load_positions()

            if positions:
                logger.info("[PM] Checking %d open position(s)", len(positions))
                for trade in list(positions):    # iterate a snapshot
                    _check_single_position(trade)
            else:
                logger.debug("[PM] No open positions")

        except Exception as exc:
            logger.error("[PM] Monitor loop error: %s", exc)

        time.sleep(MONITOR_INTERVAL)


# ── Manual helpers (used by webhook server) ───────────────────────────────────

def close_positions_for_asset(asset: str, reason: str = "MANUAL") -> list:
    """Close all open positions for a given asset. Returns list of closed symbols."""
    closed = []
    with _lock:
        positions = _load_positions()
    for trade in positions:
        if trade.get("asset") == asset:
            if _close_position(trade, reason):
                closed.append(trade.get("symbol"))
    return closed


def list_positions() -> list:
    return _load_positions()
