#!/usr/bin/env python3
"""
CFX-CPR Naked Option Sell Bot  v1.0
Assets: BTC + ETH  |  Timeframe: 1H  |  Exchange: Delta Exchange India
"""
import time
import logging
import schedule
import pandas as pd
from datetime import datetime, timezone

from delta_client  import DeltaClient
from indicators    import run_indicators
from strategy      import evaluate_signal, check_exit
from risk_manager  import RiskManager
from order_manager import OrderManager
from config        import ASSETS, CAPITAL_SPLIT, SPREAD_WIDTH, TF_1H, TF_1D

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ── Globals ───────────────────────────────────────────────────────────────────
client  = DeltaClient()
om      = OrderManager(client)
rm: RiskManager = None          # initialised on first run

# Perpetual symbols on Delta Exchange India
PERP_SYMBOL = {"BTC": "BTCUSD", "ETH": "ETHUSD"}


def _to_df(candles: list) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("time").dropna(subset=["close"]).reset_index(drop=True)
    return df


# ── Main strategy tick ────────────────────────────────────────────────────────
def run_strategy():
    global rm
    logger.info("=" * 60)
    logger.info(f"Tick: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")

    # Wallet
    equity = client.get_wallet_balance()
    if equity <= 0:
        logger.error("Cannot read USDT balance — skipping tick")
        return
    logger.info(f"Equity: ${equity:,.2f} USDT")

    # Init risk manager once
    if rm is None:
        rm = RiskManager(equity)
    rm.update_equity(equity)

    if rm.kill_switch:
        logger.warning(f"KILL SWITCH active (DD={rm.drawdown_pct():.2f}%) — closing all, no new entries")
        om.close_all()
        return

    # Per-asset loop
    for asset in ASSETS:
        logger.info(f"\n── {asset} ──────────────────────────────────────")
        try:
            sym = PERP_SYMBOL[asset]

            df_1h    = _to_df(client.get_candles(sym, TF_1H, 250))
            df_daily = _to_df(client.get_candles(sym, TF_1D, 50))

            if df_1h.empty or len(df_daily) < 3:
                logger.error(f"{asset}: insufficient candle data")
                continue

            ind = run_indicators(df_1h, df_daily)
            logger.info(
                f"{asset} close={ind['close']:.2f} | "
                f"regime={ind['regime']}({['Comp','Exp','Vel'][ind['regime']]}) | "
                f"long={ind['long_strength']:.0f} short={ind['short_strength']:.0f} | "
                f"squeeze={ind['squeeze']} RSI={ind['rsi']:.1f} MFI={ind['mfi']:.1f}"
            )

            rm.track_squeeze(asset, ind["squeeze"])

            # ── Exit check for open positions ──────────────────────────────
            if om.has_position(asset):
                reason = check_exit(ind, om.get_position(asset)["side"])
                if reason:
                    logger.info(f"{asset}: momentum exit — {reason}")
                    om.close_position(asset, reason)
                else:
                    om.check_sl_tp(asset, ind["close"])

            # ── Entry check ────────────────────────────────────────────────
            if not om.has_position(asset):
                sig = evaluate_signal(ind, SPREAD_WIDTH[asset])
                if sig:
                    logger.info(
                        f"{asset}: SIGNAL {sig['action']} | strike={sig['strike']} "
                        f"sl={sig['sl_price']:.2f} regime={sig['regime']} str={sig['strength']:.0f}"
                    )
                    stop_dist = abs(ind["close"] - sig["sl_price"])
                    qty = rm.calc_qty(
                        equity       = equity,
                        stop_dist    = stop_dist,
                        spot         = ind["close"],
                        asset        = asset,
                        alloc        = CAPITAL_SPLIT[asset],
                        regime       = ind["regime"],
                        post_squeeze = rm.post_squeeze(asset),
                    )
                    if qty > 0:
                        om.place_option_order(asset, sig, qty, ind)
                    else:
                        logger.info(f"{asset}: qty=0, order skipped")

        except Exception:
            logger.exception(f"{asset}: unhandled exception in tick")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("CFX-CPR Bot starting — assets=%s split=%s", ASSETS, CAPITAL_SPLIT)

    # Recover any open positions from exchange before first scan (prevents duplicate entries)
    om.sync_on_startup(ASSETS)

    # Scan every 15 minutes — catches CPR breakouts mid-candle without waiting up to 58 min.
    # Indicators still use the latest closed 1H candle; live price is checked 4x per hour.
    run_strategy()
    schedule.every(15).minutes.do(run_strategy)

    while True:
        schedule.run_pending()
        time.sleep(15)
