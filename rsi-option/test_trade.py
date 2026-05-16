"""
Test script: dry-run the full trade pipeline for both BTC and ETH
without placing any real orders on the exchange.
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    stream=sys.stdout,
)

from strategy import (
    find_best_option, calculate_contracts, calculate_iv_rank,
    get_spot_price, get_ist_now, calculate_strike,
    get_expiry_sequence, round_to_tick,
)
from delta_api import get_usdt_balance
from config import CAPITAL_SPLIT, TP_PCT, SL_MULT, EMERGENCY_PCT

print("=" * 60)
print("  RSI2 Option Bot — Trade Pipeline Test (no real orders)")
print("=" * 60)

equity = get_usdt_balance()
print(f"\nAccount Balance : ${equity:.4f} USDT")
ist_now  = get_ist_now()
expiries = get_expiry_sequence(ist_now)
print(f"IST Time        : {ist_now.strftime('%H:%M:%S')}")
print(f"Expiry sequence : {expiries}")

for asset in ("BTC", "ETH"):
    for direction, sig in [("PUT", "SELL_PUT"), ("CALL", "SELL_CALL")]:
        print(f"\n{'─'*60}")
        print(f"  {asset} {direction} — signal: {sig}")
        print(f"{'─'*60}")

        spot = get_spot_price(asset)
        if not spot:
            print("  ERROR: could not fetch spot price")
            continue
        print(f"  Spot price      : ${spot:,.2f}")

        strike = calculate_strike(spot, asset, direction)
        print(f"  Target strike   : ${strike:,.0f}  ({CAPITAL_SPLIT[asset]*100:.0f}% alloc)")

        option = None
        for exp in expiries:
            option = find_best_option(asset, direction, strike, exp)
            if option:
                break

        if not option:
            print(f"  No option found on chain (expiries tried: {expiries})")
            continue

        iv_rank  = calculate_iv_rank(asset, option["iv"])
        alloc    = equity * CAPITAL_SPLIT[asset]
        contracts = calculate_contracts(alloc, equity, asset, option)

        tick      = option["tick_size"]
        raw_price = option["bid"] if option["bid"] > 0 else option["mid_price"] * 0.98
        limit_px  = round_to_tick(raw_price, tick)
        entry_px  = option["mid_price"]

        print(f"  Symbol          : {option['symbol']}")
        print(f"  Strike (actual) : ${option['strike']:,.0f}")
        print(f"  Expiry          : {option['expiry_date']}")
        print(f"  Bid / Ask       : ${option['bid']:.4f} / ${option['ask']:.4f}")
        print(f"  Mid price       : ${entry_px:.4f}")
        print(f"  Mark price      : ${option['mark_price']:.4f}")
        print(f"  Delta           : {option['delta']:.3f}")
        print(f"  IV (ask)        : {float(option['iv'])*100:.1f}%")
        print(f"  IV Rank         : {iv_rank:.1f}%")
        print(f"  Contract value  : {option['contract_value']} {asset}")
        print(f"  Spot (in opt)   : ${option['spot_price']:,.2f}")
        print(f"  Initial margin  : {option['initial_margin']}% of notional")
        print(f"  Tick size       : {tick}")
        print(f"  ---")
        print(f"  Allocated cap   : ${alloc:.4f}")
        notional = option["contract_value"] * option["spot_price"]
        margin_lot = (option["initial_margin"] / 100) * notional
        print(f"  Notional/lot    : ${notional:.4f}")
        print(f"  Margin/lot      : ${margin_lot:.4f}")
        print(f"  Risk budget     : ${equity * CAPITAL_SPLIT[asset] * 2/100:.4f}  (2% rule)")
        print(f"  SL/lot          : ${SL_MULT * entry_px:.4f}")
        print(f"  Contracts       : {contracts if contracts else 'SKIP (cannot afford)'}")
        if contracts:
            print(f"  Limit sell @    : ${limit_px}")
            print(f"  ---")
            print(f"  TP target       : ${entry_px * TP_PCT:.4f}  (50% decay from ${entry_px:.4f})")
            print(f"  SL target       : ${entry_px * SL_MULT:.4f}  (2x from ${entry_px:.4f})")
            emerg_price = (
                option["strike"] * (1 - EMERGENCY_PCT / 100)
                if direction == "PUT"
                else option["strike"] * (1 + EMERGENCY_PCT / 100)
            )
            print(f"  Emergency exit  : spot {'<=' if direction == 'PUT' else '>='} ${emerg_price:,.0f}")
            print(f"  Time exit       : 5:00 PM IST on {option['expiry_date']}")

print(f"\n{'='*60}")
print("  Test complete — no real orders were placed.")
print(f"{'='*60}\n")
