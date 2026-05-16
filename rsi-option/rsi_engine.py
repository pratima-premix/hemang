"""
RSI(2) engine.
Fetches OHLCV candles from Delta Exchange perpetuals and computes
Wilder's RSI on daily and 5-minute timeframes independently for BTC and ETH.
"""

import logging
import time
from typing import Optional

import numpy as np

from config import RSI_LEN, RSI_DAY_LONG, RSI_DAY_SHORT, RSI_5M_LONG, RSI_5M_SHORT, SYMBOLS
from delta_api import get_ohlcv

logger = logging.getLogger(__name__)


# ── Core RSI math ─────────────────────────────────────────────────────────────

def _wilder_rsi(closes: list, period: int = 2) -> Optional[float]:
    """
    Wilder's smoothed RSI.  Requires at least (period + 1) data points.
    Returns the most-recent RSI value or None on insufficient data.
    """
    if len(closes) < period + 1:
        return None

    arr    = np.array(closes, dtype=float)
    deltas = np.diff(arr)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Seed with simple average of first `period` bars
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    # Wilder smoothing: α = 1/period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 4)


# ── OHLCV fetching ────────────────────────────────────────────────────────────

def _fetch_closes(asset: str, resolution: str, count: int = 50) -> list:
    """
    Download recent close prices for the perpetual futures of an asset.
    resolution : '5m' = 5-minute bars | '1d' = daily bars
    count      : number of closes needed (fetches 3× as a buffer)
    """
    symbol = SYMBOLS.get(asset)
    if not symbol:
        raise ValueError(f"Unknown asset: {asset}")

    now = int(time.time())
    seconds_per_bar = {
        "5s": 5, "1m": 60, "3m": 180, "5m": 300, "15m": 900,
        "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
        "6h": 21600, "12h": 43200, "1d": 86400, "1w": 604800,
    }.get(resolution, 300)

    start = now - seconds_per_bar * count * 3   # 3× buffer
    candles = get_ohlcv(symbol, resolution, start, now)

    if not candles:
        logger.warning("No %s candles returned for %s", resolution, symbol)
        return []

    # Candle format: [timestamp, open, high, low, close, volume]
    closes = []
    for c in candles:
        if isinstance(c, (list, tuple)) and len(c) >= 5:
            closes.append(float(c[4]))          # index 4 = close
        elif isinstance(c, dict):
            closes.append(float(c.get("close", c.get("c", 0))))

    return closes[-count:]                       # Return the most-recent N


# ── Public interface ──────────────────────────────────────────────────────────

def get_daily_rsi(asset: str) -> Optional[float]:
    """RSI(2) on closed daily candles only (drops the current forming candle)."""
    try:
        closes = _fetch_closes(asset, "1d", count=31)
        # Drop the last entry — it is today's still-forming candle.
        # TradingView request.security("D", ...) returns only confirmed bars;
        # skipping the partial bar keeps both bots in sync.
        if len(closes) > 1:
            closes = closes[:-1]
        return _wilder_rsi(closes[-30:], RSI_LEN)
    except Exception as exc:
        logger.error("Daily RSI error for %s: %s", asset, exc)
        return None


def get_5m_rsi(asset: str) -> Optional[float]:
    """RSI(2) on closed 5m candles only (drops the current forming bar)."""
    try:
        closes = _fetch_closes(asset, "5m", count=21)
        if len(closes) > 1:
            closes = closes[:-1]
        return _wilder_rsi(closes[-20:], RSI_LEN)
    except Exception as exc:
        logger.error("5m RSI error for %s: %s", asset, exc)
        return None


def get_signal(asset: str) -> dict:
    """
    Evaluate dual-timeframe RSI signal for an asset.

    Returns:
        {
            'asset'    : str,
            'daily_rsi': float | None,
            '5m_rsi'   : float | None,
            'signal'   : 'SELL_PUT' | 'SELL_CALL' | 'NONE',
            'error'    : str | None,
        }
    """
    result = {
        "asset":     asset,
        "daily_rsi": None,
        "5m_rsi":    None,
        "signal":    "NONE",
        "error":     None,
    }

    daily = get_daily_rsi(asset)
    rsi5m = get_5m_rsi(asset)

    if daily is None or rsi5m is None:
        result["error"] = "RSI calculation failed (insufficient candle data)"
        return result

    result["daily_rsi"] = daily
    result["5m_rsi"]    = rsi5m

    if daily < RSI_DAY_LONG and rsi5m < RSI_5M_LONG:
        result["signal"] = "SELL_PUT"
    elif daily > RSI_DAY_SHORT and rsi5m > RSI_5M_SHORT:
        result["signal"] = "SELL_CALL"

    logger.info(
        "[%s] Daily RSI: %.2f | 5m RSI: %.2f | Signal: %s",
        asset, daily, rsi5m, result["signal"],
    )
    return result
