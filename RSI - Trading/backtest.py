"""
RSI-2 Dual-Timeframe Backtester — Delta Exchange historical data
Fetches real OHLCV data and simulates the exact strategy the bot runs.
"""

import requests
import time
import math
from datetime import datetime, timezone

BASE = "https://api.india.delta.exchange"

# ── Strategy params (must match bot config) ────────────────────────────────────
RSI_PERIOD     = 2
LONG_DAILY_RSI = 30.0
LONG_5M_RSI    = 35.0
SHORT_DAILY_RSI= 65.0
SHORT_5M_RSI   = 70.0
TP_PCT         = 0.02    # 2%
TRAIL_PCT      = 0.20    # 20%
INITIAL_CAPITAL= 100.0
LEVERAGE       = 5
SIZE_FACTOR    = 0.45    # fraction of capital used per trade (with leverage)

# ── Data fetch ─────────────────────────────────────────────────────────────────

def fetch(symbol, resolution, bars=4000):
    now = int(time.time())
    spb = 86400 if resolution == "D" else int(resolution) * 60
    r = requests.get(f"{BASE}/v2/chart/history", params={
        "symbol": symbol, "resolution": resolution,
        "from": now - spb * (bars + 5), "to": now
    }, timeout=15)
    res = r.json().get("result", {})
    return [
        {"ts": res["t"][i], "o": float(res["o"][i]), "h": float(res["h"][i]),
         "l": float(res["l"][i]), "c": float(res["c"][i])}
        for i in range(len(res.get("t", [])))
    ]

# ── RSI (Wilder smoothed) ──────────────────────────────────────────────────────

def calc_rsi(closes, period=2):
    if len(closes) < period + 1:
        return None
    ch = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    ag = sum(max(0, x) for x in ch[:period]) / period
    al = sum(max(0, -x) for x in ch[:period]) / period
    for x in ch[period:]:
        ag = (ag * (period-1) + max(0, x))  / period
        al = (al * (period-1) + max(0, -x)) / period
    return 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)

# ── Backtest engine ────────────────────────────────────────────────────────────

def run(symbol, contract_value, tick_size):
    print(f"\n{'='*60}")
    print(f"  Backtesting: {symbol}")
    print(f"{'='*60}")

    bars_5m  = fetch(symbol, "5",  bars=4000)
    bars_day = fetch(symbol, "D",  bars=800)

    if not bars_5m or not bars_day:
        print("  No data."); return

    oldest = datetime.fromtimestamp(bars_5m[0]["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
    newest = datetime.fromtimestamp(bars_5m[-1]["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
    print(f"  5m data : {len(bars_5m)} bars  ({oldest} → {newest})")
    print(f"  Daily   : {len(bars_day)} bars\n")

    # Build daily-close lookup: day_start_ts → close
    daily_closes_seq = [b["c"] for b in bars_day]
    daily_ts_seq     = [b["ts"] for b in bars_day]

    def get_daily_rsi_at(ts):
        """Return RSI(2) on daily bars whose day-start is BEFORE ts."""
        secs_in_day = 86400
        day_start = (ts // secs_in_day) * secs_in_day
        closes = [c for t, c in zip(daily_ts_seq, daily_closes_seq) if t < day_start]
        return calc_rsi(closes)

    # Simulation state
    capital   = INITIAL_CAPITAL
    position  = None   # dict with keys: side, entry, peak, tp, contracts, bars_held
    trades    = []
    equity    = [capital]

    for i in range(10, len(bars_5m)):
        bar = bars_5m[i]
        closes_5m = [b["c"] for b in bars_5m[max(0, i-30):i+1]]

        rsi5 = calc_rsi(closes_5m)
        rsid = get_daily_rsi_at(bar["ts"])

        if rsi5 is None or rsid is None:
            continue

        # ── In a position: check exits first ─────────────────────────────────
        if position:
            side  = position["side"]
            entry = position["entry"]
            tp    = position["tp"]
            trail_dist = entry * TRAIL_PCT
            position["bars_held"] += 1

            # Update peak
            if side == "long":
                position["peak"] = max(position["peak"], bar["h"])
                stop = position["peak"] - trail_dist
                # TP hit?
                if bar["h"] >= tp:
                    exit_px = tp
                    pnl = (exit_px - entry) * position["contracts"] * contract_value
                    capital += pnl
                    trades.append({"side": side, "entry": entry, "exit": exit_px,
                                   "pnl": pnl, "pct": (exit_px-entry)/entry*100,
                                   "bars": position["bars_held"], "reason": "TP"})
                    position = None
                # Trail stop hit?
                elif bar["l"] <= stop:
                    exit_px = stop
                    pnl = (exit_px - entry) * position["contracts"] * contract_value
                    capital += pnl
                    trades.append({"side": side, "entry": entry, "exit": exit_px,
                                   "pnl": pnl, "pct": (exit_px-entry)/entry*100,
                                   "bars": position["bars_held"], "reason": "STOP"})
                    position = None
            else:  # short
                position["peak"] = min(position["peak"], bar["l"])
                stop = position["peak"] + trail_dist
                if bar["l"] <= tp:
                    exit_px = tp
                    pnl = (entry - exit_px) * position["contracts"] * contract_value
                    capital += pnl
                    trades.append({"side": side, "entry": entry, "exit": exit_px,
                                   "pnl": pnl, "pct": (entry-exit_px)/entry*100,
                                   "bars": position["bars_held"], "reason": "TP"})
                    position = None
                elif bar["h"] >= stop:
                    exit_px = stop
                    pnl = (entry - exit_px) * position["contracts"] * contract_value
                    capital += pnl
                    trades.append({"side": side, "entry": entry, "exit": exit_px,
                                   "pnl": pnl, "pct": (entry-exit_px)/entry*100,
                                   "bars": position["bars_held"], "reason": "STOP"})
                    position = None

        # ── No position: check entry signals ─────────────────────────────────
        if not position:
            entry_px = bar["c"]
            notional = capital * 0.5 * LEVERAGE * SIZE_FACTOR
            contracts = max(1, math.floor(notional / (entry_px * contract_value)))

            if rsid < LONG_DAILY_RSI and rsi5 < LONG_5M_RSI:
                position = {
                    "side": "long", "entry": entry_px, "peak": entry_px,
                    "tp": round(entry_px * (1 + TP_PCT), 8),
                    "contracts": contracts, "bars_held": 0
                }
            elif rsid > SHORT_DAILY_RSI and rsi5 > SHORT_5M_RSI:
                position = {
                    "side": "short", "entry": entry_px, "peak": entry_px,
                    "tp": round(entry_px * (1 - TP_PCT), 8),
                    "contracts": contracts, "bars_held": 0
                }

        equity.append(capital)

    # ── Results ───────────────────────────────────────────────────────────────
    if not trades:
        print("  No completed trades in this period.")
        return

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    tp_hits = [t for t in trades if t["reason"] == "TP"]
    sl_hits = [t for t in trades if t["reason"] == "STOP"]

    total_pnl   = sum(t["pnl"] for t in trades)
    gross_profit= sum(t["pnl"] for t in wins)
    gross_loss  = abs(sum(t["pnl"] for t in losses))
    pf          = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    avg_bars    = sum(t["bars"] for t in trades) / len(trades)

    # Max drawdown
    peak_eq = equity[0]
    max_dd  = 0
    for eq in equity:
        peak_eq = max(peak_eq, eq)
        dd = (peak_eq - eq) / peak_eq * 100
        max_dd = max(max_dd, dd)

    print(f"  Period        : {oldest} → {newest}")
    print(f"  Total trades  : {len(trades)}  (TP: {len(tp_hits)}  Stop: {len(sl_hits)})")
    print(f"  Win rate      : {len(wins)/len(trades)*100:.1f}%  ({len(wins)} wins / {len(losses)} losses)")
    print(f"  Net P&L       : ${total_pnl:+.2f}  ({total_pnl/INITIAL_CAPITAL*100:+.2f}%)")
    print(f"  Gross profit  : ${gross_profit:.2f}")
    print(f"  Gross loss    : ${gross_loss:.2f}")
    print(f"  Profit factor : {pf:.2f}")
    print(f"  Max drawdown  : {max_dd:.2f}%")
    print(f"  Avg bars/trade: {avg_bars:.0f} bars = {avg_bars*5/60:.1f} hours")
    print(f"  Final capital : ${capital:.2f}")
    print()
    print(f"  {'#':>3}  {'Side':6}  {'Entry':>10}  {'Exit':>10}  {'P&L':>8}  {'%':>6}  {'Bars':>5}  Reason")
    print(f"  {'-'*65}")
    for idx, t in enumerate(trades, 1):
        sign = "✓" if t["pnl"] > 0 else "✗"
        print(f"  {idx:>3}  {t['side']:6}  {t['entry']:>10.2f}  {t['exit']:>10.2f}  "
              f"{t['pnl']:>+8.3f}  {t['pct']:>+6.2f}%  {t['bars']:>5}  {sign} {t['reason']}")

# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nRSI-2 Dual-Timeframe Backtest — Delta Exchange Data")
    print(f"Strategy : Daily RSI<{LONG_DAILY_RSI} & 5m RSI<{LONG_5M_RSI} → LONG  |  "
          f"Daily RSI>{SHORT_DAILY_RSI} & 5m RSI>{SHORT_5M_RSI} → SHORT")
    print(f"Exit     : TP={TP_PCT*100}%  |  Trailing stop={TRAIL_PCT*100}% from peak")
    print(f"Capital  : ${INITIAL_CAPITAL}  |  Leverage: {LEVERAGE}x  |  Size factor: {SIZE_FACTOR}")

    run("ETHUSD", contract_value=0.01,   tick_size=0.05)
    run("BTCUSD", contract_value=0.001,  tick_size=0.5)
