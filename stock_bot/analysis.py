"""
analysis.py — Technical analysis engine.
Runs pre-market and after-hours to produce a trade bias for the day.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from config import (
    EMA_FAST, EMA_SLOW, EMA_TREND,
    RSI_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIG,
    BB_PERIOD, BB_STD, ATR_PERIOD,
)

log = logging.getLogger(__name__)

Bias = Literal["LONG", "SHORT", "FLAT"]


# ── Individual indicators ──────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(com=period - 1, adjust=False).mean()
    avg_l = loss.ewm(com=period - 1, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast=12, slow=26, signal=9):
    fast_ema  = ema(series, fast)
    slow_ema  = ema(series, slow)
    macd_line = fast_ema - slow_ema
    sig_line  = ema(macd_line, signal)
    histogram  = macd_line - sig_line
    return macd_line, sig_line, histogram


def bollinger(series: pd.Series, period=20, std=2.0):
    mid   = series.rolling(period).mean()
    sigma = series.rolling(period).std()
    upper = mid + std * sigma
    lower = mid - std * sigma
    return upper, mid, lower


def atr(df: pd.DataFrame, period=14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()


def support_resistance(df: pd.DataFrame, lookback: int = 20) -> tuple[float, float]:
    """Rough S/R from recent swings."""
    recent = df.tail(lookback)
    support    = float(recent["low"].min())
    resistance = float(recent["high"].max())
    return support, resistance


# ── Full analysis result ───────────────────────────────────────────────────────

@dataclass
class MarketSnapshot:
    symbol:       str
    timestamp:    pd.Timestamp
    close:        float
    ema20:        float
    ema50:        float
    ema200:       float
    rsi:          float
    macd_hist:    float
    bb_upper:     float
    bb_lower:     float
    atr:          float
    support:      float
    resistance:   float
    bias:         Bias
    score:        int           # -3 … +3 bull score
    key_levels:   dict = field(default_factory=dict)
    notes:        list  = field(default_factory=list)


def analyse(df: pd.DataFrame, symbol: str) -> MarketSnapshot:
    """
    Run full technical analysis on a daily OHLCV DataFrame.
    Returns a MarketSnapshot with a LONG / SHORT / FLAT bias.
    """
    if len(df) < EMA_TREND + 5:
        raise ValueError(f"Need at least {EMA_TREND + 5} bars, got {len(df)}")

    close = df["close"]
    last  = close.iloc[-1]
    ts    = df.index[-1]

    # ── Indicators ─────────────────────────────────────────────────────────────
    e20  = float(ema(close, EMA_FAST).iloc[-1])
    e50  = float(ema(close, EMA_SLOW).iloc[-1])
    e200 = float(ema(close, EMA_TREND).iloc[-1])
    rsi_ = float(rsi(close, RSI_PERIOD).iloc[-1])
    m_line, s_line, hist = macd(close, MACD_FAST, MACD_SLOW, MACD_SIG)
    macd_hist = float(hist.iloc[-1])
    macd_prev = float(hist.iloc[-2])
    bb_u, bb_m, bb_l = bollinger(close, BB_PERIOD, BB_STD)
    bb_upper = float(bb_u.iloc[-1])
    bb_lower = float(bb_l.iloc[-1])
    atr_val  = float(atr(df, ATR_PERIOD).iloc[-1])
    support, resistance = support_resistance(df)

    # ── Scoring  (+1 bull / -1 bear per factor) ────────────────────────────────
    score = 0
    notes = []

    # Trend: price vs EMAs
    if last > e200:
        score += 1; notes.append("✅ Price above EMA200 (uptrend)")
    else:
        score -= 1; notes.append("🔴 Price below EMA200 (downtrend)")

    if e20 > e50:
        score += 1; notes.append("✅ EMA20 > EMA50 (short-term bullish)")
    else:
        score -= 1; notes.append("🔴 EMA20 < EMA50 (short-term bearish)")

    # Momentum: RSI
    if rsi_ > 55:
        score += 1; notes.append(f"✅ RSI {rsi_:.1f} — bullish momentum")
    elif rsi_ < 45:
        score -= 1; notes.append(f"🔴 RSI {rsi_:.1f} — bearish momentum")
    else:
        notes.append(f"⚪ RSI {rsi_:.1f} — neutral zone")

    # MACD histogram direction
    if macd_hist > 0 and macd_hist > macd_prev:
        score += 1; notes.append("✅ MACD histogram expanding bullish")
    elif macd_hist < 0 and macd_hist < macd_prev:
        score -= 1; notes.append("🔴 MACD histogram expanding bearish")
    else:
        notes.append("⚪ MACD mixed signal")

    # Bollinger position
    bb_range = bb_upper - bb_lower
    if bb_range > 0:
        pct_b = (last - bb_lower) / bb_range
        if pct_b > 0.7:
            score += 1; notes.append(f"✅ Price in upper BB ({pct_b*100:.0f}%)")
        elif pct_b < 0.3:
            score -= 1; notes.append(f"🔴 Price in lower BB ({pct_b*100:.0f}%)")
        else:
            notes.append(f"⚪ Price mid-BB ({pct_b*100:.0f}%)")

    # ── Bias decision ──────────────────────────────────────────────────────────
    if score >= 2:
        bias: Bias = "LONG"
    elif score <= -2:
        bias = "SHORT"
    else:
        bias = "FLAT"

    key_levels = {
        "ema20":      round(e20, 2),
        "ema50":      round(e50, 2),
        "ema200":     round(e200, 2),
        "support":    round(support, 2),
        "resistance": round(resistance, 2),
        "bb_upper":   round(bb_upper, 2),
        "bb_lower":   round(bb_lower, 2),
        "atr":        round(atr_val, 2),
    }

    snap = MarketSnapshot(
        symbol=symbol,
        timestamp=ts,
        close=round(float(last), 2),
        ema20=round(e20, 2),
        ema50=round(e50, 2),
        ema200=round(e200, 2),
        rsi=round(rsi_, 2),
        macd_hist=round(macd_hist, 4),
        bb_upper=round(bb_upper, 2),
        bb_lower=round(bb_lower, 2),
        atr=round(atr_val, 2),
        support=round(support, 2),
        resistance=round(resistance, 2),
        bias=bias,
        score=score,
        key_levels=key_levels,
        notes=notes,
    )

    _log_snapshot(snap)
    return snap


def _log_snapshot(s: MarketSnapshot) -> None:
    sep = "─" * 55
    log.info(sep)
    log.info(f"  PRE-MARKET ANALYSIS  |  {s.symbol}  |  {s.timestamp.date()}")
    log.info(sep)
    log.info(f"  Close:    {s.close:>10.2f}")
    log.info(f"  EMA20:    {s.ema20:>10.2f}   EMA50:  {s.ema50:>10.2f}   EMA200: {s.ema200:>10.2f}")
    log.info(f"  RSI:      {s.rsi:>10.2f}   MACD-H: {s.macd_hist:>10.4f}   ATR:    {s.atr:>10.2f}")
    log.info(f"  BB:       {s.bb_lower:>10.2f} — {s.bb_upper:.2f}")
    log.info(f"  Support:  {s.support:>10.2f}   Resist: {s.resistance:>10.2f}")
    log.info(f"  Bull score: {s.score:+d}/5")
    for note in s.notes:
        log.info(f"    {note}")
    log.info(f"  ➤ BIAS: {s.bias}")
    log.info(sep)
