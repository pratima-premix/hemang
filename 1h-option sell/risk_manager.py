import logging
from typing import Dict
from config import (RISK_PER_TRADE_PCT, MAX_EXPOSURE_PCT, KILL_SWITCH_DD_PCT,
                    CONTRACT_VALUE, INITIAL_MARGIN_PCT, FIXED_LOTS)

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, initial_equity: float):
        self.peak_equity    = initial_equity
        self.current_equity = initial_equity
        self.kill_switch    = False
        self._squeeze_bars: Dict[str, int] = {}

    def update_equity(self, equity: float):
        self.current_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity
        dd = self._dd_pct()
        if dd >= KILL_SWITCH_DD_PCT:
            if not self.kill_switch:
                logger.warning(f"KILL SWITCH ACTIVATED — drawdown {dd:.2f}%")
            self.kill_switch = True
        else:
            self.kill_switch = False

    def _dd_pct(self) -> float:
        if self.peak_equity == 0:
            return 0.0
        return (self.peak_equity - self.current_equity) / self.peak_equity * 100

    def drawdown_pct(self) -> float:
        return self._dd_pct()

    def calc_qty(self, equity: float, stop_dist: float, spot: float,
                 asset: str, alloc: float, regime: int,
                 post_squeeze: bool) -> int:
        """
        Returns number of whole contracts to trade.

        If FIXED_LOTS[asset] > 0, uses that directly (with regime/squeeze scaling).
        Otherwise uses dynamic sizing based on equity and risk %.
        """
        if stop_dist <= 0 or spot <= 0:
            return 0

        regime_scale = {0: 1.0, 1: 0.75, 2: 0.0}.get(regime, 0.0)
        if post_squeeze:
            regime_scale *= 0.5
        if regime_scale == 0:
            return 0

        cv             = CONTRACT_VALUE[asset]
        im_pct         = INITIAL_MARGIN_PCT[asset]
        margin_per_lot = spot * cv * (im_pct / 100.0)
        risk_per_lot   = stop_dist * cv

        # ── Dynamic sizing ────────────────────────────────────────────────
        allocated_usd  = equity * alloc
        risk_capital   = allocated_usd * (RISK_PER_TRADE_PCT / 100.0)
        margin_budget  = allocated_usd * (MAX_EXPOSURE_PCT / 100.0)

        if risk_per_lot <= 0 or margin_per_lot <= 0:
            return 0

        risk_qty   = risk_capital / risk_per_lot
        margin_cap = margin_budget / margin_per_lot

        raw = min(risk_qty, margin_cap) * regime_scale
        dynamic_qty = max(int(raw), 1)

        max_by_alloc = int(allocated_usd / margin_per_lot)
        dynamic_qty = min(dynamic_qty, max_by_alloc)

        # ── Fixed floor: take whichever is larger ─────────────────────────
        fixed = FIXED_LOTS.get(asset, 0)
        if fixed > 0:
            floor_qty = max(int(fixed * regime_scale), 1)
            qty = max(dynamic_qty, floor_qty)
        else:
            qty = dynamic_qty

        logger.info(
            f"{asset} sizing | equity={equity:.2f} alloc={alloc*100:.0f}%={allocated_usd:.2f} "
            f"risk_cap={risk_capital:.4f} risk/lot={risk_per_lot:.4f} "
            f"margin/lot={margin_per_lot:.4f} "
            f"risk_qty={risk_qty:.1f} margin_cap={margin_cap:.1f} "
            f"regime_scale={regime_scale:.2f} dynamic={dynamic_qty} floor={fixed} → qty={qty} lots"
        )
        return qty

    def track_squeeze(self, asset: str, squeeze_on: bool):
        if squeeze_on:
            self._squeeze_bars[asset] = 3
        elif self._squeeze_bars.get(asset, 0) > 0:
            self._squeeze_bars[asset] -= 1

    def post_squeeze(self, asset: str) -> bool:
        return self._squeeze_bars.get(asset, 0) > 0
