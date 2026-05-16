import numpy as np
import pandas as pd
from typing import Dict, Tuple
from config import (EMA_FAST, EMA_MID, EMA_SLOW, MACD_FAST, MACD_SLOW, MACD_SIG,
                    RSI_LEN, RSI_TRIGGER, MFI_LEN, ATR_LEN, ATR_LO, ATR_HI,
                    SQZ_LEN, CYCLE_LEN, VOL_Z_WIN, STOP_MULT)


# ── EMA ───────────────────────────────────────────────────────────────────────
def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def calc_ema_ribbon(df: pd.DataFrame) -> Dict:
    e8, e21, e34 = _ema(df["close"], EMA_FAST), _ema(df["close"], EMA_MID), _ema(df["close"], EMA_SLOW)
    v8, v21, v34 = e8.iloc[-1], e21.iloc[-1], e34.iloc[-1]
    return {
        "ema8": v8, "ema21": v21, "ema34": v34,
        "bull": v8 > v21 > v34,
        "bear": v8 < v21 < v34,
    }


# ── MACD ──────────────────────────────────────────────────────────────────────
def calc_macd(df: pd.DataFrame) -> Dict:
    macd_line  = _ema(df["close"], MACD_FAST) - _ema(df["close"], MACD_SLOW)
    sig_line   = _ema(macd_line, MACD_SIG)
    hist       = macd_line - sig_line
    hist_sm    = _ema(hist, 3)
    hist_adapt = hist.rolling(14).std().fillna(0) * 0.5

    ml, sl, hs, ha = macd_line.iloc[-1], sig_line.iloc[-1], hist_sm.iloc[-1], hist_adapt.iloc[-1]
    return {
        "macd": ml, "signal": sl,
        "hist_raw": hist.iloc[-1],
        "hist_smooth": hs, "hist_adapt": ha,
        "bull": ml > sl and hs > ha,
        "bear": ml < sl and hs < -ha,
    }


# ── RSI ───────────────────────────────────────────────────────────────────────
def calc_rsi(df: pd.DataFrame) -> float:
    d    = df["close"].diff()
    gain = d.clip(lower=0).ewm(span=RSI_LEN, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(span=RSI_LEN, adjust=False).mean()
    rs   = gain / loss.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


# ── MFI ───────────────────────────────────────────────────────────────────────
def calc_mfi(df: pd.DataFrame) -> float:
    tp   = (df["high"] + df["low"] + df["close"]) / 3
    mf   = tp * df["volume"]
    pos  = mf.where(tp > tp.shift(1), 0.0).rolling(MFI_LEN).sum()
    neg  = mf.where(tp <= tp.shift(1), 0.0).rolling(MFI_LEN).sum()
    return float((100 - 100 / (1 + pos / neg.replace(0, np.nan))).iloc[-1])


# ── ATR + Regime ──────────────────────────────────────────────────────────────
def calc_atr_regime(df: pd.DataFrame) -> Tuple[float, int, float]:
    hi, lo, pc = df["high"], df["low"], df["close"].shift(1)
    tr  = pd.concat([hi - lo, (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=ATR_LEN, adjust=False).mean()

    atr_pct = (atr / df["close"]) * 100
    smoothed = _ema(atr_pct, 13).iloc[-1]

    if smoothed < ATR_LO:
        regime = 0
    elif smoothed < ATR_HI:
        regime = 1
    else:
        regime = 2

    return float(atr.iloc[-1]), regime, STOP_MULT[regime]


# ── Squeeze (BB inside KC) ────────────────────────────────────────────────────
def calc_squeeze(df: pd.DataFrame) -> bool:
    basis = df["close"].rolling(SQZ_LEN).mean()
    std   = df["close"].rolling(SQZ_LEN).std()
    bb_hi = basis + 2.0 * std
    bb_lo = basis - 2.0 * std

    hi, lo, pc = df["high"], df["low"], df["close"].shift(1)
    tr   = pd.concat([hi - lo, (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    katr = tr.rolling(SQZ_LEN).mean()
    kc_hi = basis + 1.5 * katr
    kc_lo = basis - 1.5 * katr

    return bool(bb_hi.iloc[-1] < kc_hi.iloc[-1] and bb_lo.iloc[-1] > kc_lo.iloc[-1])


# ── Volume Z-Score ────────────────────────────────────────────────────────────
def calc_volume(df: pd.DataFrame) -> Dict:
    vm  = df["volume"].rolling(VOL_Z_WIN).mean()
    vs  = df["volume"].rolling(VOL_Z_WIN).std().replace(0, np.nan)
    vz  = ((df["volume"] - vm) / vs).iloc[-1]
    vol = df["volume"].iloc[-1]
    return {
        "vol_ma": float(vm.iloc[-1]),
        "vol_z": float(vz),
        "impulse": vol > vm.iloc[-1] * 1.15 or vz > 0.5,
    }


# ── Cycle Oscillator (price vs slow EMA) ─────────────────────────────────────
def calc_cycle(df: pd.DataFrame) -> Dict:
    basis = _ema(df["close"], CYCLE_LEN)
    osc   = df["close"] - basis
    v     = float(osc.iloc[-1])
    return {"value": v, "bull": v > 0, "bear": v < 0}


# ── CPR ───────────────────────────────────────────────────────────────────────
def calc_cpr(h: float, l: float, c: float) -> Dict:
    cp = (h + l + c) / 3
    bc = (h + l) / 2
    tc = (cp - bc) + cp
    return {
        "cp": cp,
        "top":    max(bc, tc),
        "bottom": min(bc, tc),
        "tr1": 2 * cp - l,
        "ts1": 2 * cp - h,
    }


# ── Strike selection ──────────────────────────────────────────────────────────
def calc_strike(cpr: Dict, close: float, spread: float, opt: str) -> float:
    if opt == "call":
        raw = cpr["top"] if close < cpr["ts1"] else (cpr["cp"] + max(close, cpr["tr1"])) / 2
        return float(np.ceil(raw / spread) * spread)
    else:
        raw = cpr["bottom"] if close > cpr["tr1"] else (cpr["cp"] + min(close, cpr["ts1"])) / 2
        return float(np.floor(raw / spread) * spread)


# ── Composite signal strength (0-100) ────────────────────────────────────────
def composite_strength(ribbon: Dict, macd: Dict, rsi: float, mfi: float,
                        volume: Dict, cycle: Dict, direction: str) -> float:
    score = 0.0
    trig  = RSI_TRIGGER
    if direction == "long":
        if ribbon["bull"]:      score += 20
        if macd["bull"]:        score += 20
        if rsi > trig:          score += 15
        if mfi > trig:          score += 15
        if volume["impulse"]:   score += 15
        if cycle["bull"]:       score += 15
    else:
        if ribbon["bear"]:      score += 20
        if macd["bear"]:        score += 20
        if rsi < (100 - trig):  score += 15
        if mfi < (100 - trig):  score += 15
        if volume["impulse"]:   score += 15
        if cycle["bear"]:       score += 15
    return score


# ── Master calculation ────────────────────────────────────────────────────────
def run_indicators(df_1h: pd.DataFrame, df_daily: pd.DataFrame) -> Dict:
    ribbon   = calc_ema_ribbon(df_1h)
    macd     = calc_macd(df_1h)
    rsi      = calc_rsi(df_1h)
    mfi      = calc_mfi(df_1h)
    atr, regime, stop_mult = calc_atr_regime(df_1h)
    squeeze  = calc_squeeze(df_1h)
    volume   = calc_volume(df_1h)
    cycle    = calc_cycle(df_1h)

    prev = df_daily.iloc[-2]
    cpr  = calc_cpr(float(prev["high"]), float(prev["low"]), float(prev["close"]))

    close       = float(df_1h["close"].iloc[-1])
    long_str    = composite_strength(ribbon, macd, rsi, mfi, volume, cycle, "long")
    short_str   = composite_strength(ribbon, macd, rsi, mfi, volume, cycle, "short")

    return {
        "close": close,
        "ribbon": ribbon, "macd": macd,
        "rsi": rsi, "mfi": mfi,
        "atr": atr, "regime": regime, "stop_mult": stop_mult,
        "squeeze": squeeze, "volume": volume, "cycle": cycle,
        "cpr": cpr,
        "long_strength": long_str,
        "short_strength": short_str,
    }
