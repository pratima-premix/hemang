import logging
from typing import Optional, Dict
from config import MIN_SIGNAL_STRENGTH, RSI_TRIGGER
from indicators import calc_strike

logger = logging.getLogger(__name__)

REGIME_NAME = {0: "Compression", 1: "Expansion", 2: "Velocity"}


def evaluate_signal(ind: Dict, spread: float) -> Optional[Dict]:
    close  = ind["close"]
    cpr    = ind["cpr"]
    regime = ind["regime"]
    macd   = ind["macd"]
    ribbon = ind["ribbon"]
    rsi    = ind["rsi"]
    mfi    = ind["mfi"]
    vol    = ind["volume"]
    atr    = ind["atr"]
    sm     = ind["stop_mult"]

    # ── Global safety gates ────────────────────────────────────────────────
    if regime == 2:
        logger.info("SKIP: Velocity regime")
        return None
    if ind["squeeze"]:
        logger.info("SKIP: Squeeze active")
        return None

    above = close > cpr["top"]
    below = close < cpr["bottom"]

    if not above and not below:
        logger.info(f"SKIP: Price inside CPR ({cpr['bottom']:.2f} – {cpr['top']:.2f})")
        return None

    # ── SELL PUT (bullish bias) ───────────────────────────────────────────
    if above:
        if rsi > 80 or mfi > 80:
            logger.info(f"SKIP: Extreme overbought RSI={rsi:.1f} MFI={mfi:.1f}")
            return None
        if (ribbon["bull"] and
                ind["long_strength"] >= MIN_SIGNAL_STRENGTH and
                macd["bull"] and
                rsi > RSI_TRIGGER and
                mfi > RSI_TRIGGER):
            strike = calc_strike(cpr, close, spread, "put")
            sl     = strike - atr * sm
            return {
                "action": "SELL_PUT",
                "strike": strike,
                "sl_price": sl,
                "regime": REGIME_NAME[regime],
                "strength": ind["long_strength"],
            }

    # ── SELL CALL (bearish bias) ──────────────────────────────────────────
    if below:
        if (ribbon["bear"] and
                ind["short_strength"] >= MIN_SIGNAL_STRENGTH and
                macd["bear"] and
                rsi < (100 - RSI_TRIGGER) and
                mfi < (100 - RSI_TRIGGER)):
            strike = calc_strike(cpr, close, spread, "call")
            sl     = strike + atr * sm
            return {
                "action": "SELL_CALL",
                "strike": strike,
                "sl_price": sl,
                "regime": REGIME_NAME[regime],
                "strength": ind["short_strength"],
            }

    logger.info(
        f"SKIP: No qualifying signal | long={ind['long_strength']:.0f} "
        f"short={ind['short_strength']:.0f} RSI={rsi:.1f} MFI={mfi:.1f}"
    )
    return None


def check_exit(ind: Dict, position_side: str) -> Optional[str]:
    ribbon = ind["ribbon"]
    macd   = ind["macd"]
    if position_side == "SELL_PUT":
        if not ribbon["bull"] or macd["hist_raw"] < 0:
            return "MOMENTUM_FADE"
    elif position_side == "SELL_CALL":
        if not ribbon["bear"] or macd["hist_raw"] > 0:
            return "MOMENTUM_FADE"
    return None
