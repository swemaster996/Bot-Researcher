"""
config.py — All settings in one place.
Set ALPACA_API_KEY and ALPACA_SECRET_KEY as environment variables
or paste them directly here for testing.
"""

import os

# ── Alpaca credentials ─────────────────────────────────────────────────────────
API_KEY    = os.getenv("ALPACA_API_KEY",    "PKBS54NQSOBT6NQH5GVQJEO7W6")
API_SECRET = os.getenv("ALPACA_SECRET_KEY", "ANeLXnJH1jGk7ryhUdZk2ej2PMQTKbYRVTGk6tZb9nzW")
BASE_URL   = "https://paper-api.alpaca.markets"   # switch to live when ready

# ── Supabase (NorthstarAI) ─────────────────────────────────────────────────────
SUPABASE_URL = "https://mjtwpiwdvhcoegjnpcjv.supabase.co"
SUPABASE_KEY = "sb_publishable_UV9_fzBKlk72UAKZj7Mbfg_81iYZZaN"

# ── Instrument ─────────────────────────────────────────────────────────────────
SYMBOL = "SPY"          # S&P 500 ETF  (change to "QQQ" for Nasdaq)
                        # Note: DAX requires a different broker (e.g. IBKR)

# ── Strategy timing (all times Eastern / New York) ────────────────────────────
PRE_MARKET_ANALYSIS_TIME = "08:00"   # full overnight analysis
ORB_START_TIME           = "09:30"   # market open — begin collecting range
ORB_END_TIME             = "09:45"   # first 15 min = opening range
MONITOR_UNTIL            = "14:55"   # stop new entries after this
CLOSE_ALL_TIME           = "15:00"   # force-close all positions (1 hour before close)
AFTER_HOURS_ANALYSIS     = "17:00"   # evening run for tomorrow prep

ORB_MINUTES = 15   # opening range window in minutes

# ── Risk management ────────────────────────────────────────────────────────────
RISK_PER_TRADE_PCT     = 0.01   # 1 % of account equity risked per trade

# Dynamic position cap by signal score (|score| = 2..5)
# Higher conviction → larger allowed position size
POSITION_PCT_BY_SCORE = {
    2: 0.30,   # just clears bias threshold — small position
    3: 0.50,   # moderate conviction
    4: 0.70,   # strong conviction
    5: 1.00,   # all 5 indicators aligned — full port
    6: 1.00,   # all 6 indicators (incl. volume) — full port
}
ATR_STOP_MULTIPLIER    = 1.0    # stop = 1×ATR below/above entry price (used as reference; overridden by MAX_STOP_PCT below)
MAX_STOP_PCT           = 0.008  # fixed stop cap: 0.8% of entry price (original tight stop for larger positions)
TAKE_PROFIT_RATIO      = 2.0    # take-profit = 2× the stop distance (2 R)
TRAILING_AFTER_R       = 1.0    # activate trailing stop once +1 R is reached
MAX_OPEN_POSITIONS     = 1      # one position at a time
MAX_TRADES_PER_DAY     = 3      # max entries per trading day
TRADE_COOLDOWN_MINUTES = 30     # minutes to wait after a close before re-entering
MAX_ATR_FILTER         = 15.0   # skip trade if daily ATR > this (only extreme days)

# ── Technical-analysis parameters ─────────────────────────────────────────────
EMA_FAST   = 20
EMA_SLOW   = 50
EMA_TREND  = 200
RSI_PERIOD = 14
MACD_FAST  = 12
MACD_SLOW  = 26
MACD_SIG   = 9
BB_PERIOD  = 20
BB_STD     = 2.0
ATR_PERIOD = 14

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_FILE = "trading_bot.log"
