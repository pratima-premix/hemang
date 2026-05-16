"""
RSI-2 Dual-Timeframe Trading Bot  –  Delta Exchange (India)
Runs every 5 minutes, manages ETH and BTC perpetual futures.
Supports position reversal: if opposite signal fires while in a trade,
closes current position and opens the opposite side.
"""

import logging
import math
import time
from datetime import datetime, timezone

from config import (
    API_KEY, API_SECRET,
    SYMBOLS, LEVERAGE,
    CAPITAL_SPLIT,        # fraction of wallet per symbol (0.50 each)
    START_SIZE_FACTOR,    # start with this fraction of calculated size
    MIN_CONTRACTS,        # hard minimum contracts to place
    DRY_RUN,
    REVERSAL_ENABLED,     # flip position on opposite signal
)
from delta_client import DeltaClient
from strategy import Signal, StrategyConfig, compute_signal, tp_price, trail_stop_price

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── State (in-memory per run) ─────────────────────────────────────────────────
# { product_id: { "signal": Signal, "entry": float, "peak": float, "stop_order_id": int } }
_state: dict[int, dict] = {}

cfg = StrategyConfig()
client = DeltaClient(API_KEY, API_SECRET)


# ── Helpers ───────────────────────────────────────────────────────────────────

def calc_contracts(symbol_cfg: dict, entry_price: float, usdt_balance: float) -> int:
    notional = usdt_balance * CAPITAL_SPLIT * LEVERAGE * START_SIZE_FACTOR
    contract_value_usd = entry_price * symbol_cfg["contract_value"]
    raw = notional / contract_value_usd
    size = max(MIN_CONTRACTS, math.floor(raw))
    log.info("%s  wallet=%.2f USDT  notional=%.2f  contracts=%d",
             symbol_cfg["symbol"], usdt_balance, notional, size)
    return size


def round_price(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 8)


def _enter_position(sym: dict, signal: Signal) -> None:
    """Place market entry + TP limit + initial trailing stop. Updates _state."""
    pid  = sym["product_id"]
    name = sym["symbol"]

    mark = client.get_mark_price(name)
    if mark <= 0:
        log.error("%s  invalid mark price: %s", name, mark)
        return

    usdt_bal = client.get_usdt_balance()
    size = calc_contracts(sym, mark, usdt_bal)
    side = "buy" if signal == Signal.LONG else "sell"
    tp   = round_price(tp_price(mark, signal, cfg), sym["tick_size"])

    log.info("%s  ENTERING %s  mark=%.4f  size=%d  TP=%.4f",
             name, signal.value.upper(), mark, size, tp)

    if DRY_RUN:
        log.info("[DRY-RUN] would place %s %d contracts", side, size)
        _state[pid] = {"signal": signal, "entry": mark, "peak": mark, "stop_order_id": None}
        return

    entry_resp = client.place_market_order(pid, side, size)
    if not entry_resp.get("success"):
        log.error("%s  entry order failed: %s", name, entry_resp)
        return

    entry_price = float(entry_resp.get("result", {}).get("average_fill_price", mark))

    # TP as limit reduce-only
    tp = round_price(tp_price(entry_price, signal, cfg), sym["tick_size"])
    tp_side = "sell" if signal == Signal.LONG else "buy"
    client.place_limit_order(pid, tp_side, size, tp, reduce_only=True)

    # Initial trailing stop
    try:
        client.cancel_all_stop_orders(pid)
    except Exception:
        pass
    initial_stop = round_price(trail_stop_price(entry_price, entry_price, signal, cfg), sym["tick_size"])
    stop_side = "sell" if signal == Signal.LONG else "buy"
    stop_resp = client.place_stop_market_order(pid, stop_side, size, initial_stop)
    stop_id = stop_resp.get("result", {}).get("id")

    _state[pid] = {
        "signal": signal,
        "entry": entry_price,
        "peak": entry_price,
        "stop_order_id": stop_id,
    }
    log.info("%s  entry done  entry=%.4f  TP=%.4f  stop=%.4f  stop_id=%s",
             name, entry_price, tp, initial_stop, stop_id)


# ── Core per-symbol logic ─────────────────────────────────────────────────────

def run_symbol(sym: dict) -> None:
    pid  = sym["product_id"]
    name = sym["symbol"]

    # ── 1. Fetch candles ──────────────────────────────────────────────────────
    closes_5m = client.get_candles(name, "5", bars=30)
    closes_1d = client.get_candles(name, "D", bars=20)

    if len(closes_5m) < 10 or len(closes_1d) < 5:
        log.warning("%s  not enough candles (5m=%d 1d=%d)", name, len(closes_5m), len(closes_1d))
        return

    signal, rsi_d, rsi_5 = compute_signal(closes_1d, closes_5m, cfg)
    log.info("%s  RSI_daily=%.2f  RSI_5m=%.2f  signal=%s", name, rsi_d, rsi_5, signal.value)

    # ── 2. Check open position ────────────────────────────────────────────────
    position = client.get_position(pid)

    if position is None:
        # Clear stale state and cancel any orphan stop orders
        _state.pop(pid, None)
        try:
            client.cancel_all_stop_orders(pid)
        except Exception:
            pass

        if signal == Signal.NONE:
            return

        _enter_position(sym, signal)

    else:
        # ── 3. Reconstruct state if bot restarted mid-position ────────────────
        st = _state.get(pid)
        if st is None:
            entry    = float(position.get("entry_price", 0))
            pos_side = "buy" if float(position.get("size", 0)) > 0 else "sell"
            sig      = Signal.LONG if pos_side == "buy" else Signal.SHORT
            _state[pid] = {"signal": sig, "entry": entry, "peak": entry, "stop_order_id": None}
            st = _state[pid]
            log.info("%s  reconstructed state after restart  entry=%.4f  signal=%s",
                     name, entry, sig.value)

        sig = st["signal"]

        # ── 4. Reversal: opposite signal → close + flip ───────────────────────
        is_reversal = REVERSAL_ENABLED and (
            (sig == Signal.LONG  and signal == Signal.SHORT) or
            (sig == Signal.SHORT and signal == Signal.LONG)
        )

        if is_reversal:
            log.info("%s  REVERSAL — closing %s, entering %s",
                     name, sig.value.upper(), signal.value.upper())

            if not DRY_RUN:
                # Cancel TP + stop before closing
                try:
                    client.cancel_all_orders(pid)
                except Exception as e:
                    log.warning("%s  cancel all orders failed: %s", name, e)

                pos_size = abs(int(float(position.get("size", 0))))
                pos_side = "buy" if float(position.get("size", 0)) > 0 else "sell"
                close_resp = client.close_position(pid, pos_size, pos_side)
                if not close_resp.get("success"):
                    log.error("%s  failed to close position for reversal: %s", name, close_resp)
                    return
                log.info("%s  position closed for reversal", name)
            else:
                log.info("[DRY-RUN] would close %s and enter %s", sig.value, signal.value)

            _state.pop(pid, None)
            _enter_position(sym, signal)
            return

        # ── 5. Manage trailing stop ───────────────────────────────────────────
        mark = client.get_mark_price(name)
        peak = st["peak"]

        if sig == Signal.LONG:
            new_peak = max(peak, mark)
        else:
            new_peak = min(peak, mark)

        if new_peak != peak:
            st["peak"] = new_peak
            new_stop = round_price(trail_stop_price(st["entry"], new_peak, sig, cfg), sym["tick_size"])
            log.info("%s  peak moved %.4f→%.4f  new trail stop=%.4f",
                     name, peak, new_peak, new_stop)

            if not DRY_RUN:
                try:
                    client.cancel_all_stop_orders(pid)
                except Exception as e:
                    log.warning("%s  cancel stops failed: %s", name, e)

                # Re-verify position still open — TP may have filled between ticks
                recheck = client.get_position(pid)
                if recheck is None:
                    log.info("%s  position closed by TP between ticks — skipping stop placement", name)
                    _state.pop(pid, None)
                    return

                size = abs(int(float(recheck.get("size", 1))))
                stop_side = "sell" if sig == Signal.LONG else "buy"
                stop_resp = client.place_stop_market_order(pid, stop_side, size, new_stop)
                st["stop_order_id"] = stop_resp.get("result", {}).get("id")
        else:
            log.info("%s  peak unchanged=%.4f  mark=%.4f", name, peak, mark)


# ── Main loop ─────────────────────────────────────────────────────────────────

def seconds_to_next_5m() -> float:
    now = time.time()
    return 300 - (now % 300)


def main():
    log.info("=" * 60)
    log.info("RSI-2 Bot starting  DRY_RUN=%s  symbols=%s",
             DRY_RUN, [s["symbol"] for s in SYMBOLS])
    log.info("=" * 60)

    while True:
        tick_start = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log.info("── Tick: %s ──────────────────────────────────", tick_start)

        for sym in SYMBOLS:
            try:
                run_symbol(sym)
            except Exception as exc:
                log.exception("%s  unhandled error: %s", sym["symbol"], exc)

        wait = seconds_to_next_5m()
        log.info("sleeping %.0fs until next 5m candle\n", wait)
        time.sleep(wait)


if __name__ == "__main__":
    main()
