import os
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.getenv("DELTA_API_KEY",    "rjZB67AAPiyJEmE8B0rn5RyQgDVcA8")
API_SECRET = os.getenv("DELTA_API_SECRET", "jCK3EyiqWQM18I7vAMZt4VR2b44sQKP27twLkd3sAvJVb5Ukoybnoe1X34Z5")
BASE_URL   = "https://api.india.delta.exchange"

ASSETS         = ["BTC", "ETH"]
CAPITAL_SPLIT  = {"BTC": 0.50, "ETH": 0.50}
SPREAD_WIDTH   = {"BTC": 50.0, "ETH": 5.0}

# Contract value per lot (in underlying coin)
# BTC option: 1 lot = 0.001 BTC  |  ETH option: 1 lot = 0.01 ETH
CONTRACT_VALUE = {"BTC": 0.001, "ETH": 0.01}

# Initial margin % for option selling (from Delta Exchange product data)
INITIAL_MARGIN_PCT = {"BTC": 0.5, "ETH": 1.0}   # percent of underlying notional

# Fixed lot sizes — set to 0 to use dynamic sizing based on balance
FIXED_LOTS = {"BTC": 100, "ETH": 180}

# Timeframes
TF_1H  = "1h"
TF_1D  = "1d"

# Signal
MIN_SIGNAL_STRENGTH = 50
KILL_SWITCH_DD_PCT  = 999.0  # disabled
RISK_PER_TRADE_PCT  = 2.0
MAX_EXPOSURE_PCT    = 30.0

# Indicator periods
EMA_FAST     = 8
EMA_MID      = 21
EMA_SLOW     = 34
MACD_FAST    = 8
MACD_SLOW    = 21
MACD_SIG     = 5
RSI_LEN      = 21
RSI_TRIGGER  = 50
MFI_LEN      = 21
ATR_LEN      = 21
ATR_LO       = 0.8   # Compression boundary %
ATR_HI       = 1.6   # Velocity boundary %
SQZ_LEN      = 20
CYCLE_LEN    = 55
VOL_Z_WIN    = 20

# ATR stop multipliers per regime
STOP_MULT = {0: 1.05, 1: 1.55, 2: 2.10}

# TP / SL
TP_DECAY_PCT      = 0.50   # bracket TP: close when premium decays to 50% of entry
BRACKET_SL_MULT   = 3.00   # bracket SL: close if option price reaches 3× entry premium

# Time / trail logic
MAX_HOLD_CANDLES    = 4    # after 4 candles, evaluate whether to trail or close
TRAIL_THRESHOLD_PCT = 0.70 # if premium has decayed to ≤70% of entry (≥30% profit), trail to expiry
PRE_EXPIRY_CLOSE_MINS = 60 # force-close this many minutes before expiry to avoid pin/gamma risk

# Expiry preference: D+1 (next calendar day), fallback D+2 … D+7
EXPIRY_TARGET_DAYS = 1     # prefer options expiring tomorrow
