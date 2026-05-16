import os
from dotenv import load_dotenv

load_dotenv()

# ── API ──────────────────────────────────────────────────────────────────────
DELTA_API_KEY    = os.getenv("DELTA_API_KEY", "")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET", "")
DELTA_BASE_URL   = "https://api.india.delta.exchange"   # Delta Exchange India

# Perpetual symbols used for RSI & spot-price fetching
SYMBOLS = {
    "BTC": "BTCUSD",
    "ETH": "ETHUSD",
}

# ── RSI Strategy ─────────────────────────────────────────────────────────────
RSI_LEN       = 2
RSI_DAY_LONG  = 30    # Daily RSI < 30  → sell put
RSI_5M_LONG   = 35    # 5m   RSI < 35  → confirm put signal
RSI_DAY_SHORT = 65    # Daily RSI > 65  → sell call
RSI_5M_SHORT  = 70    # 5m   RSI > 70  → confirm call signal
IV_RANK_MIN   = 25.0  # Skip if IV Rank below this

# Strike distance from spot (%). 0.0 = ATM, higher = further OTM
STRIKE_PCT = {
    "BTC": {"PUT": 1.5, "CALL": 1.5},
    "ETH": {"PUT": 2.0, "CALL": 2.0},
}

# ── Exit Rules ────────────────────────────────────────────────────────────────
TP_PCT             = 0.50   # Take profit: close when premium = 50% of entry
SL_MULT            = 2.0    # Stop loss:   close when premium = 2× entry
EMERGENCY_PCT      = 1.0    # Emergency:   spot crosses strike by 1%
DISABLE_SL         = True   # Skip software SL check (testing mode)
DISABLE_EMERGENCY  = True   # Skip emergency exit (spot-through-strike check)

# ── Reverse-signal flip ───────────────────────────────────────────────────────
REVERSE_FLIP_COUNT = 3      # After N consecutive reverse signals, close & flip position
DISABLE_FLIP       = True   # Skip the reverse-signal flip entirely

# ── Position Sizing ───────────────────────────────────────────────────────────
RISK_PCT = 100.0        # Use full margin capacity — risk_limit always exceeds margin_limit
MIN_LOTS = 1            # Always trade at least this many lots when margin allows
CAPITAL_SPLIT = {
    "BTC": 0.50,        # 50% of equity allocated to BTC trades
    "ETH": 0.50,        # 50% of equity allocated to ETH trades
}

# ── Timing (IST = UTC+5:30) ───────────────────────────────────────────────────
IST_OFFSET_HOURS = 5
IST_OFFSET_MINS  = 30

# Signal cutoffs (IST)
EXPIRY_CUTOFF_SAME_DAY_H = 11   # Before 11:30 AM → use D+0 expiry
EXPIRY_CUTOFF_SAME_DAY_M = 30
EXPIRY_CUTOFF_NEXT_DAY_H = 15   # 11:30–15:30 → use D+1; after 15:30 → D+1/D+2
EXPIRY_CUTOFF_NEXT_DAY_M = 30

CLOSE_BEFORE_EXPIRY_MINS = 30   # Close positions 30 min before 5:30 PM IST

# ── Server ────────────────────────────────────────────────────────────────────
WEBHOOK_PORT        = 5000
WEBHOOK_SECRET      = os.getenv("WEBHOOK_SECRET", "rsi_bot_secure_2024")
MONITOR_INTERVAL    = 60    # seconds between position-monitor ticks
RSI_SCAN_INTERVAL   = 300   # seconds between autonomous RSI scans (5 min)

# ── File paths ────────────────────────────────────────────────────────────────
IV_HISTORY_FILE    = "iv_history.json"
POSITIONS_FILE     = "open_positions.json"
TRADES_LOG_FILE    = "trades.log"
BOT_LOG_FILE       = "bot.log"
