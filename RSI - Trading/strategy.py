"""
RSI-2 strategy: dual-timeframe (Daily + 5m) for ETH and BTC perpetuals.

Entry:
  LONG  – Daily RSI(2) < 30  AND  5m RSI(2) < 35
  SHORT – Daily RSI(2) > 65  AND  5m RSI(2) > 70

Exit:
  Take-profit : +2% from entry (long) / -2% (short)
  Trailing stop: 8% trail distance  (managed manually in bot.py)
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Signal(Enum):
    NONE = "none"
    LONG = "long"
    SHORT = "short"


@dataclass
class StrategyConfig:
    rsi_period: int = 2
    long_daily_rsi: float = 30.0
    long_5m_rsi: float = 35.0
    short_daily_rsi: float = 65.0
    short_5m_rsi: float = 70.0
    take_profit_pct: float = 0.02    # 2%
    trail_pct: float = 0.08          # 8% trailing distance (safe at 10x leverage)
    trail_offset_pct: float = 0.01   # 1% stop execution offset


def _rsi(closes: list[float], period: int) -> Optional[float]:
    """Wilder-smoothed RSI. Returns None when not enough data."""
    if len(closes) < period + 1:
        return None

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(0.0, c) for c in changes]
    losses = [max(0.0, -c) for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_signal(closes_daily: list[float], closes_5m: list[float],
                   cfg: StrategyConfig = StrategyConfig()) -> tuple[Signal, float, float]:
    """
    Returns (Signal, rsi_daily, rsi_5m).
    Uses the second-to-last candle for both timeframes (last closed candle).
    """
    # Use confirmed (closed) candles: exclude the last in-progress bar
    daily = closes_daily[:-1] if len(closes_daily) > 1 else closes_daily
    m5 = closes_5m[:-1] if len(closes_5m) > 1 else closes_5m

    rsi_d = _rsi(daily, cfg.rsi_period)
    rsi_5 = _rsi(m5, cfg.rsi_period)

    if rsi_d is None or rsi_5 is None:
        return Signal.NONE, rsi_d or 50.0, rsi_5 or 50.0

    if rsi_d < cfg.long_daily_rsi and rsi_5 < cfg.long_5m_rsi:
        return Signal.LONG, rsi_d, rsi_5
    if rsi_d > cfg.short_daily_rsi and rsi_5 > cfg.short_5m_rsi:
        return Signal.SHORT, rsi_d, rsi_5

    return Signal.NONE, rsi_d, rsi_5


def tp_price(entry: float, signal: Signal, cfg: StrategyConfig = StrategyConfig()) -> float:
    if signal == Signal.LONG:
        return entry * (1 + cfg.take_profit_pct)
    return entry * (1 - cfg.take_profit_pct)


def trail_stop_price(entry: float, peak: float, signal: Signal,
                     cfg: StrategyConfig = StrategyConfig()) -> float:
    """
    Compute current trailing stop trigger price.
      Long : peak_price - (entry * trail_pct)
      Short: peak_price + (entry * trail_pct)
    The 'offset' is applied at execution time (market order slippage allowance).
    """
    trail_dist = entry * cfg.trail_pct
    if signal == Signal.LONG:
        return peak - trail_dist
    return peak + trail_dist
