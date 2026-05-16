"""
Backtest the reverse-signal flip mechanism.

Replays historical 5m candles, generates the same SELL_PUT / SELL_CALL signals
the live bot would have produced, and simulates the flip-on-3-reverse-signals
logic to count how often it would have fired.
"""

import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/home/hemang/rsi-option")

from delta_api import get_ohlcv
from rsi_engine import _wilder_rsi
from config import (
    RSI_LEN, RSI_DAY_LONG, RSI_DAY_SHORT,
    RSI_5M_LONG, RSI_5M_SHORT, SYMBOLS,
    REVERSE_FLIP_COUNT,
)

LOOKBACK_DAYS = 14
SEC_DAY = 86400
SEC_5M  = 300
IST     = timezone(timedelta(hours=5, minutes=30))


def candle_ts(c):
    if isinstance(c, dict):
        return int(c.get("time", c.get("t", 0)))
    return int(c[0])


def candle_close(c):
    if isinstance(c, dict):
        return float(c.get("close", c.get("c", 0)))
    return float(c[4])


def fetch_5m_paginated(symbol, total_days):
    """Fetch 5m candles in chunks (Delta caps results per request)."""
    now    = int(time.time())
    start  = now - total_days * SEC_DAY
    chunk  = 2 * SEC_DAY              # 2 days per request (~576 bars)
    all_c  = []
    cursor = start
    while cursor < now:
        end = min(cursor + chunk, now)
        c   = get_ohlcv(symbol, "5m", cursor, end)
        all_c.extend(c or [])
        cursor = end
        time.sleep(0.2)
    # Dedupe by ts (chunks can overlap)
    seen, out = set(), []
    for c in sorted(all_c, key=candle_ts):
        ts = candle_ts(c)
        if ts in seen:
            continue
        seen.add(ts)
        out.append(c)
    return out


def compute_signal(daily_rsi, rsi_5m):
    if daily_rsi is None or rsi_5m is None:
        return "NONE"
    if daily_rsi < RSI_DAY_LONG and rsi_5m < RSI_5M_LONG:
        return "SELL_PUT"
    if daily_rsi > RSI_DAY_SHORT and rsi_5m > RSI_5M_SHORT:
        return "SELL_CALL"
    return "NONE"


def backtest(asset):
    symbol = SYMBOLS[asset]
    print(f"\n=== Backtest {asset} ({symbol}) | last {LOOKBACK_DAYS} days ===")

    candles_5m = fetch_5m_paginated(symbol, LOOKBACK_DAYS)
    now        = int(time.time())
    daily      = get_ohlcv(symbol, "1d", now - (LOOKBACK_DAYS + 30) * SEC_DAY, now) or []
    daily      = sorted(daily, key=candle_ts)

    print(f"  Loaded {len(candles_5m)} x 5m bars, {len(daily)} x daily bars")

    closes_5m = [(candle_ts(c), candle_close(c)) for c in candles_5m]
    closes_1d = [(candle_ts(c), candle_close(c)) for c in daily]

    open_sig      = None
    entry_time    = None
    reverse_count = 0
    entries       = 0
    flips         = 0
    sig_counts    = {"NONE": 0, "SELL_PUT": 0, "SELL_CALL": 0}
    flip_events   = []

    for i, (ts, _close) in enumerate(closes_5m):
        if i < 15:
            continue

        win_5m = [c[1] for c in closes_5m[max(0, i - 20): i + 1]]
        rsi_5m = _wilder_rsi(win_5m, RSI_LEN)

        d_closes = [c[1] for c in closes_1d if c[0] <= ts]
        if len(d_closes) < 5:
            continue
        rsi_d = _wilder_rsi(d_closes[-30:], RSI_LEN)

        sig = compute_signal(rsi_d, rsi_5m)
        sig_counts[sig] += 1

        if open_sig is None:
            if sig != "NONE":
                open_sig      = sig
                entry_time    = ts
                reverse_count = 0
                entries      += 1
            continue

        # Position is open
        if sig == "NONE" or sig == open_sig:
            reverse_count = 0
            continue

        # Reverse signal
        reverse_count += 1
        if reverse_count >= REVERSE_FLIP_COUNT:
            flip_events.append({
                "time":         ts,
                "from":         open_sig,
                "to":           sig,
                "duration_min": (ts - entry_time) // 60,
            })
            flips        += 1
            open_sig      = sig
            entry_time    = ts
            reverse_count = 0
            entries      += 1

    print(f"\n  Results:")
    print(f"    Total entries:   {entries}")
    print(f"    Flip triggers:   {flips}  (≥{REVERSE_FLIP_COUNT} consecutive reverse signals)")
    print(f"    Signal mix:      NONE={sig_counts['NONE']}, "
          f"SELL_PUT={sig_counts['SELL_PUT']}, SELL_CALL={sig_counts['SELL_CALL']}")

    if flip_events:
        print(f"\n  Flip events (showing first 20):")
        print(f"    {'When (IST)':<20} {'From':<10} → {'To':<10} {'Held':>10}")
        for ev in flip_events[:20]:
            t = datetime.fromtimestamp(ev["time"], IST).strftime("%Y-%m-%d %H:%M")
            held = f"{ev['duration_min']} min"
            if ev['duration_min'] >= 60:
                held = f"{ev['duration_min'] / 60:.1f} h"
            print(f"    {t:<20} {ev['from']:<10} → {ev['to']:<10} {held:>10}")
        if len(flip_events) > 20:
            print(f"    ... and {len(flip_events) - 20} more")

    return {"entries": entries, "flips": flips}


if __name__ == "__main__":
    totals = {"entries": 0, "flips": 0}
    for asset in ("BTC", "ETH"):
        r = backtest(asset)
        totals["entries"] += r["entries"]
        totals["flips"]   += r["flips"]
    print(f"\n=== TOTAL ===")
    print(f"  Entries: {totals['entries']}  |  Flips: {totals['flips']}  |  "
          f"Flip rate: {totals['flips']/max(1,totals['entries'])*100:.1f}%")
