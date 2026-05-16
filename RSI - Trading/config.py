import os

# ── API Credentials ───────────────────────────────────────────────────────────
API_KEY    = os.getenv("DELTA_API_KEY",    "kWJbb7efJG2QzuAIzDkj2ryd3xODDe")
API_SECRET = os.getenv("DELTA_API_SECRET", "THXhwU6Ot87yKzb3izGWa8MIkhAzttde0xteH2lW9pUFjOHzO5VkhyJVFnM5")

# ── Symbol Config ─────────────────────────────────────────────────────────────
# product_id  : from Delta Exchange /v2/products
# contract_value: ETH=0.01 ETH per contract, BTC=0.001 BTC per contract
SYMBOLS = [
    {
        "symbol":         "ETHUSD",
        "product_id":     3136,
        "tick_size":      0.05,
        "contract_value": 0.01,   # 1 contract = 0.01 ETH
    },
    {
        "symbol":         "BTCUSD",
        "product_id":     27,
        "tick_size":      0.5,
        "contract_value": 0.001,  # 1 contract = 0.001 BTC
    },
]

# ── Capital & Sizing ──────────────────────────────────────────────────────────
CAPITAL_SPLIT    = 0.50   # 50% of wallet per symbol
LEVERAGE         = 10     # leverage applied on Delta Exchange position
START_SIZE_FACTOR = 0.90  # aggressive sizing ~7 ETH / ~2 BTC contracts at $80 balance
MIN_CONTRACTS    = 1      # never go below this

# ── Safety ────────────────────────────────────────────────────────────────────
DRY_RUN          = False  # Set True to paper-trade (no real orders placed)
REVERSAL_ENABLED = False  # Set True to allow position flip on opposite signal
