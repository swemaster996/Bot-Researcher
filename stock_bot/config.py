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
MONITOR_UNTIL            = "15:30"   # stop new entries after this (was 14:55 — gave trades more runway)
CLOSE_ALL_TIME           = "15:45"   # force-close all positions (was 15:00 — cut winners short before reaching TP)
AFTER_HOURS_ANALYSIS     = "17:00"   # evening run for tomorrow prep

ORB_MINUTES = 15   # opening range window in minutes

# ── Risk management ────────────────────────────────────────────────────────────
RISK_PER_TRADE_PCT     = 0.01   # 1 % of account equity risked per trade

# Dynamic position cap by signal score (|score| = 2..6)
# Cap at 1.5× equity lets the risk model (RISK_PER_TRADE_PCT) be the binding
# constraint rather than an arbitrary size ceiling. Intraday margin allows
# 4:1 so 1.5× is very conservative. Score=2 stays small (weak signal).
POSITION_PCT_BY_SCORE = {
    2: 0.30,   # weak signal — small position
    3: 1.50,   # moderate conviction  (was 1.00 — cap freed up)
    4: 1.50,   # strong conviction
    5: 1.50,   # very strong
    6: 1.50,   # all 6 indicators aligned
}
ATR_STOP_MULTIPLIER    = 1.0    # stop = 1×ATR below/above entry price (used as reference; overridden by MAX_STOP_PCT below)
MAX_STOP_PCT           = 0.008  # fixed stop cap: 0.8% of entry price (original tight stop for larger positions)
TAKE_PROFIT_RATIO      = 2.0    # take-profit = 2× the stop distance (2 R)

# Adaptive risk scaling by ATR — bet bigger on trending days, smaller on choppy days
RISK_ATR_EXTREME   = 9.0    # ATR ≥ this → crash/rally day (e.g. March 2026 ATR 9+)
RISK_ATR_HIGH      = 8.0    # ATR ≥ this → trending market
RISK_ATR_LOW       = 5.0    # ATR < this → choppy/quiet market
RISK_PCT_EXTREME   = 0.020  # 2.0% risk on extreme vol days (ATR ≥ 9)
RISK_PCT_HIGH_VOL  = 0.015  # 1.5% risk on trending days (ATR 8–9)
RISK_PCT_NORMAL    = 0.010  # 1.0% risk on normal days
RISK_PCT_LOW_VOL   = 0.005  # 0.5% risk on quiet days
BREAKEVEN_AFTER_R      = 0.75   # move stop to entry once +0.75 R is reached — stops winners round-tripping to a full loss
TRAILING_AFTER_R       = 1.0    # activate ATR trailing stop once +1 R is reached
MAX_OPEN_POSITIONS     = 1      # one position at a time
MAX_TRADES_PER_DAY     = 3      # max entries per trading day
TRADE_COOLDOWN_MINUTES = 30     # minutes to wait after a close before re-entering
MAX_ATR_FILTER         = 15.0   # skip trade if daily ATR > this (only extreme days)
MIN_ATR_FILTER         = 3.0    # skip trade if daily ATR < this (too quiet for ORB)
MIN_SCORE_FILTER       = 3      # minimum |score| to enter — includes -3 SHORT signals
MACD_CONFIRMATION      = True   # require MACD histogram to align with bias (LONG: hist>0, SHORT: hist<0)
VWAP_FILTER            = True   # skip entry if price is on wrong side of VWAP (filters false breakouts in chop)

def adaptive_risk_pct(atr: float | None) -> float:
    """
    Return risk-per-trade percentage based on daily ATR.
    Extreme ATR (≥9) → crash/rally day  → bet 2.0% (captures big March 2026 moves)
    High ATR    (≥8) → trending market  → bet 1.5%
    Normal ATR       → ordinary day     → bet 1.0%
    Low ATR     (<5) → choppy/quiet     → bet 0.5% (limits damage in chop)
    """
    if atr is None:
        return RISK_PCT_NORMAL
    if atr >= RISK_ATR_EXTREME:
        return RISK_PCT_EXTREME
    if atr >= RISK_ATR_HIGH:
        return RISK_PCT_HIGH_VOL
    if atr < RISK_ATR_LOW:
        return RISK_PCT_LOW_VOL
    return RISK_PCT_NORMAL


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

# ── Logging ──────────────────────────────────────────�