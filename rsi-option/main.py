"""
RSI2 Option Selling Bot — Fully Autonomous
==========================================
Two background loops driven entirely by Delta Exchange API data:

  1. RSI Scanner  — fetches OHLCV every 5 min, computes dual-TF RSI(2),
                    opens option positions when both daily + 5m align.
                    Sizing = max leverage capped by 2% risk rule.

  2. Position Monitor — checks every 60 s, closes on TP / SL / emergency
                        / 30-min-before-expiry time exit.

Startup: cancels any dangling open orders from previous sessions.
"""

import json
import logging
import os
import signal
import sys
import threading
import time

from config import RSI_SCAN_INTERVAL, POSITIONS_FILE, REVERSE_FLIP_COUNT, DISABLE_FLIP
from delta_api import cancel_all_open_orders
from position_manager import add_position, run_monitor_loop, close_positions_for_asset
from rsi_engine import get_signal
from strategy import execute_trade, has_open_position, get_position_signal


# ── Logging (stdout only — nohup redirects to bot.log) ───────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("main")


# ── RSI Scanner ───────────────────────────────────────────────────────────────

# Tracks consecutive reverse-direction signals per asset while a position is open.
_reverse_count = {"BTC": 0, "ETH": 0}


def _scan_once():
    for asset in ("BTC", "ETH"):
        try:
            sig = get_signal(asset)
            if sig.get("error"):
                logger.warning("[RSI] %s: %s", asset, sig["error"])
                continue

            signal_type = sig["signal"]
            logger.info(
                "[RSI] %s | Daily RSI: %.2f | 5m RSI: %.2f | Signal: %s",
                asset, sig["daily_rsi"], sig["5m_rsi"], signal_type,
            )

            if has_open_position(asset):
                current_signal = get_position_signal(asset)

                # Same direction or NONE → reset counter, hold position
                if signal_type == "NONE" or signal_type == current_signal:
                    if _reverse_count[asset] > 0:
                        logger.info("[RSI] %s: reverse counter reset (was %d)", asset, _reverse_count[asset])
                    _reverse_count[asset] = 0
                    logger.info("[RSI] %s: position open (%s) — hold", asset, current_signal)
                    continue

                # Reverse direction
                if DISABLE_FLIP:
                    logger.info("[RSI] %s: reverse signal (open=%s, new=%s) — flip disabled, holding", asset, current_signal, signal_type)
                    continue

                _reverse_count[asset] += 1
                logger.warning(
                    "[RSI] %s: reverse signal %d/%d (open=%s, new=%s)",
                    asset, _reverse_count[asset], REVERSE_FLIP_COUNT, current_signal, signal_type,
                )

                if _reverse_count[asset] >= REVERSE_FLIP_COUNT:
                    logger.warning("[RSI] %s: FLIP — closing %s and opening %s", asset, current_signal, signal_type)
                    closed = close_positions_for_asset(asset, reason="REVERSE_FLIP")
                    logger.info("[RSI] %s: closed %d position(s): %s", asset, len(closed), closed)
                    _reverse_count[asset] = 0
                    trade = execute_trade(asset, signal_type)
                    if trade:
                        add_position(trade)
                        logger.info("[RSI] Position opened (flip): %s", trade["symbol"])
                continue

            # No position open — normal flow, reset counter
            _reverse_count[asset] = 0
            if signal_type != "NONE":
                trade = execute_trade(asset, signal_type)
                if trade:
                    add_position(trade)
                    logger.info("[RSI] Position opened: %s", trade["symbol"])

        except Exception as exc:
            logger.error("[RSI] Scan error for %s: %s", asset, exc)


def run_rsi_scanner():
    logger.info("[RSI] Scanner started — interval: %ds", RSI_SCAN_INTERVAL)
    _scan_once()
    while True:
        time.sleep(RSI_SCAN_INTERVAL)
        _scan_once()


# ── Graceful shutdown ─────────────────────────────────────────────────────────

def _handle_signal(signum, frame):
    logger.info("Shutdown signal %d received — exiting.", signum)
    sys.exit(0)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    logger.info(
        "\n"
        "╔══════════════════════════════════════════════════════════╗\n"
        "║       RSI2 Option Selling Bot — Delta Exchange India     ║\n"
        "║  Mode   : Fully autonomous (Delta Exchange API only)     ║\n"
        "║  Assets : BTC (50%%)  |  ETH (50%%)                       ║\n"
        "║  Signal : Dual-TF RSI(2) — Daily + 5m                   ║\n"
        "║  Sizing : Max leverage  capped by 2%% risk rule           ║\n"
        "║  Scan   : every %3ds     Monitor: every 60s             ║\n"
        "╚══════════════════════════════════════════════════════════╝",
        RSI_SCAN_INTERVAL,
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    # Cancel dangling orders — but keep TP/SL bracket orders for open positions
    protected = set()
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE) as f:
                for p in json.load(f):
                    for key in ("tp_order_id", "sl_order_id"):
                        if p.get(key):
                            protected.add(p[key])
    except Exception:
        pass
    logger.info("Startup: cleaning up dangling orders (protecting %d bracket orders)…", len(protected))
    cancel_all_open_orders(protected_order_ids=protected)

    # Thread: position monitor
    t_monitor = threading.Thread(target=run_monitor_loop, daemon=True, name="monitor")
    t_monitor.start()
    logger.info("Position monitor thread started")

    # Main thread: RSI scanner (keeps process alive)
    run_rsi_scanner()


if __name__ == "__main__":
    main()
