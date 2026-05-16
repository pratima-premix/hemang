"""
Real-order test: forces one ETH SELL_PUT trade through the complete pipeline.
  1. Finds best OTM put on Delta Exchange
  2. Sizes by max-leverage + 2% risk rule
  3. Places a real limit sell order
  4. Waits for fill confirmation (up to 120s, then cancels)
  5. If filled → writes to open_positions.json (monitor picks it up)

Run: venv/bin/python test_real_order.py
"""

import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_real_order")

from strategy import execute_trade
from position_manager import add_position, list_positions

print("=" * 60)
print("  REAL ORDER TEST — ETH SELL_PUT")
print("  A real limit order will be placed on Delta Exchange.")
print("=" * 60)
print()

# Force execute_trade for ETH SELL_PUT (skips RSI check — direct call)
trade = execute_trade("ETH", "SELL_PUT")

print()
if trade:
    add_position(trade)
    print("=" * 60)
    print("  ORDER FILLED AND POSITION RECORDED")
    print("=" * 60)
    print(f"  Symbol       : {trade['symbol']}")
    print(f"  Contracts    : {trade['contracts']}")
    print(f"  Entry premium: ${trade['entry_premium']:.4f}")
    print(f"  TP target    : ${trade['tp_price']:.4f}  (close when premium ≤ this)")
    print(f"  SL target    : ${trade['sl_price']:.4f}  (close when premium ≥ this)")
    print(f"  Expiry       : {trade['expiry_date']}  (time exit at 5:00 PM IST)")
    print()
    print("  Position monitor is now tracking this trade.")
    print("  Check bot.log for real-time TP/SL monitoring.")
    print("=" * 60)
else:
    print("=" * 60)
    print("  No trade was placed.")
    print("  Reason logged above — check IV Rank, sizing, or fill timeout.")
    print("=" * 60)

print()
print("Open positions now:")
for p in list_positions():
    print(f"  {p['symbol']} | {p['contracts']} lots | entry ${p['entry_premium']:.4f} "
          f"| TP ${p['tp_price']:.4f} | SL ${p['sl_price']:.4f}")
