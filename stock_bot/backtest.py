"""
backtest.py — 252-day historical simulation of the ORB strategy.
Replays each trading day: pre-market analysis → ORB range → breakout entry → TP/SL/EOD exit.
Supports up to MAX_TRADES_PER_DAY re-entries (with TRADE_COOLDOWN_MINUTES cooldown).
No real orders are placed.

Run:
    python backtest.py
"""

from __future__ import annotations
import logging
import math
import sys
from collections import Counter
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo

import pandas as pd

from broker import Broker
from analysis import analyse
from config import (
    SYMBOL,
    ORB_MINUTES,
    RISK_PER_TRADE_PCT,
    POSITION_PCT_BY_SCORE,
    ATR_STOP_MULTIPLIER,
    MAX_STOP_PCT,
    TAKE_PROFIT_RATIO,
    BREAKEVEN_AFTER_R,
    TRAILING_AFTER_R,
    CLOSE_ALL_TIME,
    MIN_ATR_FILTER,
    MIN_SCORE_FILTER,
    MACD_CONFIRMATION,
    VWAP_FILTER,
    MAX_TRADES_PER_DAY,
    TRADE_COOLDOWN_MINUTES,
    adaptive_risk_pct,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)
ET  = ZoneInfo("America/New_York")

STARTING_EQUITY = 100_000.0
BACKTEST_DAYS   = 252
CLOSE_HOUR_ET, CLOSE_MIN_ET = map(int, CLOSE_ALL_TIME.split(":"))   # force-close time, from config


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_trading_days(broker: Broker, n: int) -> list[date]:
    """Return the last n market open days from Alpaca calendar."""
    end   = datetime.now(ET).date()
    start = end - timedelta(days=n * 2 + 10)   # buffer for weekends/holidays
    cal   = broker.api.get_calendar(start=str(start), end=str(end))
    days  = [c.date for c in cal]
    return days[-n:]


def fetch_intraday(broker: Broker, symbol: str, day: date) -> pd.DataFrame:
    """Fetch 1-min bars for a single trading day (9:25 → 16:05 ET)."""
    tz    = ET
    start = datetime(day.year, day.month, day.day, 9, 25, tzinfo=tz)
    end   = datetime(day.year, day.month, day.day, 16, 5,  tzinfo=tz)

    from alpaca_trade_api.rest import TimeFrame
    bars = broker.api.get_bars(
        symbol,
        TimeFrame.Minute,
        start=start.isoformat(),
        end=end.isoformat(),
        feed="iex",
    ).df

    if bars.empty:
        return bars

    idx = pd.to_datetime(bars.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    bars.index = idx.tz_convert(ET)
    return bars[["open", "high", "low", "close", "volume"]]


def fetch_daily_up_to(broker: Broker, symbol: str, up_to: date, n: int = 250) -> pd.DataFrame:
    """Fetch n daily bars ending the day BEFORE `up_to` (pre-market view)."""
    end   = up_to - timedelta(days=1)
    start = end   - timedelta(days=n + 60)

    from alpaca_trade_api.rest import TimeFrame
    bars = broker.api.get_bars(
        symbol,
        TimeFrame.Day,
        start=str(start)[:10],
        end=str(end)[:10],
        limit=n,
        adjustment="all",
        feed="iex",
    ).df

    bars.index = pd.to_datetime(bars.index).tz_localize(None)
    return bars[["open", "high", "low", "close", "volume"]].tail(n)


# ── VWAP helper ────────────────────────────────────────────────────────────────

def compute_vwap(bars: pd.DataFrame) -> float:
    """
    Classic session VWAP: sum(typical_price × volume) / sum(volume).
    Pass all bars from market open up to (and including) the current bar.
    """
    tp  = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    vol = bars["volume"]
    total_vol = vol.sum()
    if total_vol == 0:
        return float(tp.mean())
    return float((tp * vol).sum() / total_vol)


# ── Single-trade simulation ────────────────────────────────────────────────────

def _simulate_one_trade(
    bars: pd.DataFrame,
    scan_bars: pd.DataFrame,
    snap,
    orb_high: float,
    orb_low: float,
    close_time,
    atr_pts: float,
    mkt_open,
) -> dict:
    """
    Scan scan_bars for one breakout entry and manage it to exit.
    Returns a dict with entry, side, stop, tp, exit_price, exit_reason, exit_ts.
    exit_reason = 'no_trade' if no entry was found.
    """
    entry_price   = None
    side          = None
    stop_loss     = None
    take_profit   = None
    current_stop  = None
    highest_seen  = 0.0
    lowest_seen   = 999_999.0
    stop_phase    = "initial"
    exit_price    = None
    exit_reason   = "no_trade"
    exit_ts       = None

    for ts, bar in scan_bars.iterrows():
        if ts >= close_time:
            break

        if entry_price is None:
            # ── Look for breakout (close-confirmation required) ──────────────
            if snap.bias == "LONG" and bar["high"] > orb_high and bar["close"] > orb_high:
                candidate = orb_high + 0.01

                if VWAP_FILTER:
                    bars_so_far = bars[(bars.index >= mkt_open) & (bars.index <= ts)]
                    vwap = compute_vwap(bars_so_far)
                    if candidate < vwap:
                        continue   # below VWAP — likely fake, skip

                entry_price  = candidate
                atr_stop     = entry_price - atr_pts * ATR_STOP_MULTIPLIER
                safe_floor   = entry_price * (1 - MAX_STOP_PCT)
                stop_loss    = max(atr_stop, safe_floor)
                risk_pts     = entry_price - stop_loss
                take_profit  = entry_price + risk_pts * TAKE_PROFIT_RATIO
                side         = "buy"
                current_stop = stop_loss
                highest_seen = entry_price

            elif snap.bias == "SHORT" and bar["low"] < orb_low and bar["close"] < orb_low:
                candidate = orb_low - 0.01

                if VWAP_FILTER:
                    bars_so_far = bars[(bars.index >= mkt_open) & (bars.index <= ts)]
                    vwap = compute_vwap(bars_so_far)
                    if candidate > vwap:
                        continue   # above VWAP — likely fake, skip

                entry_price  = candidate
                atr_stop     = entry_price + atr_pts * ATR_STOP_MULTIPLIER
                safe_ceil    = entry_price * (1 + MAX_STOP_PCT)
                stop_loss    = min(atr_stop, safe_ceil)
                risk_pts     = stop_loss - entry_price
                take_profit  = entry_price - risk_pts * TAKE_PROFIT_RATIO
                side         = "sell"
                current_stop = stop_loss
                lowest_seen  = entry_price

        else:
            # ── Two-phase stop management ────────────────────────────────────
            initial_risk = abs(entry_price - stop_loss)

            if side == "buy":
                if bar["high"] > highest_seen:
                    highest_seen = bar["high"]
                profit_pts = highest_seen - entry_price

                if stop_phase == "initial" and profit_pts >= initial_risk * BREAKEVEN_AFTER_R:
                    be_stop = entry_price * 1.0005
                    if be_stop > current_stop:
                        current_stop = be_stop
                        stop_phase = "breakeven"

                if profit_pts >= initial_risk * TRAILING_AFTER_R:
                    stop_phase = "trailing"
                    new_stop = highest_seen - atr_pts * 0.5
                    if new_stop > current_stop + 0.10:
                        current_stop = new_stop

                if stop_phase == "trailing":
                    if bar["low"] <= current_stop:
                        exit_price = round(current_stop, 2); exit_reason = "trailing_stop"; exit_ts = ts; break
                elif stop_phase == "breakeven":
                    if bar["high"] >= take_profit:
                        exit_price = round(take_profit, 2);  exit_reason = "take_profit";   exit_ts = ts; break
                    if bar["low"] <= current_stop:
                        exit_price = round(current_stop, 2); exit_reason = "breakeven_stop"; exit_ts = ts; break
                else:
                    if bar["high"] >= take_profit:
                        exit_price = round(take_profit, 2);  exit_reason = "take_profit";   exit_ts = ts; break
                    if bar["low"] <= current_stop:
                        exit_price = round(current_stop, 2); exit_reason = "stop_loss";     exit_ts = ts; break

            else:  # SHORT
                if bar["low"] < lowest_seen:
                    lowest_seen = bar["low"]
                profit_pts = entry_price - lowest_seen

                if stop_phase == "initial" and profit_pts >= initial_risk * BREAKEVEN_AFTER_R:
                    be_stop = entry_price * 0.9995
                    if be_stop < current_stop:
                        current_stop = be_stop
                        stop_phase = "breakeven"

                if profit_pts >= initial_risk * TRAILING_AFTER_R:
                    stop_phase = "trailing"
                    new_stop = lowest_seen + atr_pts * 0.5
                    if new_stop < current_stop - 0.10:
                        current_stop = new_stop

                if stop_phase == "trailing":
                    if bar["high"] >= current_stop:
                        exit_price = round(current_stop, 2); exit_reason = "trailing_stop"; exit_ts = ts; break
                elif stop_phase == "breakeven":
                    if bar["low"] <= take_profit:
                        exit_price = round(take_profit, 2);  exit_reason = "take_profit";   exit_ts = ts; break
                    if bar["high"] >= current_stop:
                        exit_price = round(current_stop, 2); exit_reason = "breakeven_stop"; exit_ts = ts; break
                else:
                    if bar["low"] <= take_profit:
                        exit_price = round(take_profit, 2);  exit_reason = "take_profit";   exit_ts = ts; break
                    if bar["high"] >= current_stop:
                        exit_price = round(current_stop, 2); exit_reason = "stop_loss";     exit_ts = ts; break

    # EOD close if still in trade at close_time
    if entry_price and exit_price is None:
        eod_bars = bars[bars.index >= close_time]
        if not eod_bars.empty:
            exit_price  = round(float(eod_bars.iloc[0]["close"]), 2)
            exit_reason = "eod_close"
            exit_ts     = close_time
        else:
            exit_price  = round(float(scan_bars.iloc[-1]["close"]), 2)
            exit_reason = "eod_close"
            exit_ts     = scan_bars.index[-1]

    return {
        "entry_price": entry_price,
        "side":        side,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "exit_price":  exit_price,
        "exit_reason": exit_reason,
        "exit_ts":     exit_ts,
    }


# ── Day simulation ─────────────────────────────────────────────────────────────

def simulate_day(broker: Broker, day: date, equity: float) -> dict:
    """
    Simulate one trading day. Returns a result dict.
    Allows up to MAX_TRADES_PER_DAY re-entries after stops
    (with TRADE_COOLDOWN_MINUTES cooldown between trades).
    """
    result = {
        "date":         day,
        "bias":         "—",
        "orb_high":     None,
        "orb_low":      None,
        "entry":        None,
        "side":         None,
        "stop_loss":    None,
        "take_profit":  None,
        "exit_price":   None,
        "exit_reason":  "no_trade",
        "pnl":          0.0,
        "equity":       equity,
        "trades_count": 0,
    }

    # ── Pre-market analysis ──────────────────────────────────────────────────
    try:
        daily = fetch_daily_up_to(broker, symbol=SYMBOL, up_to=day)
        if len(daily) < 50:
            result["exit_reason"] = "insufficient_history"
            return result
        snap = analyse(daily, SYMBOL)
        result["bias"] = snap.bias
    except Exception as e:
        log.warning(f"{day}  analysis failed: {e}")
        result["exit_reason"] = "analysis_error"
        return result

    if snap.bias == "FLAT":
        result["exit_reason"] = "flat_bias"
        return result

    if abs(snap.score) < MIN_SCORE_FILTER:
        result["exit_reason"] = "low_conviction"
        return result

    # MACD direction must confirm the bias (filters entries where momentum contradicts signal)
    if MACD_CONFIRMATION:
        if snap.bias == "LONG" and snap.macd_hist < 0:
            result["exit_reason"] = "macd_conflict"
            return result
        if snap.bias == "SHORT" and snap.macd_hist > 0:
            result["exit_reason"] = "macd_conflict"
            return result

    if snap.atr and snap.atr < MIN_ATR_FILTER:
        result["exit_reason"] = "low_atr"
        return result

    # ── Intraday bars ────────────────────────────────────────────────────────
    try:
        bars = fetch_intraday(broker, SYMBOL, day)
    except Exception as e:
        log.warning(f"{day}  intraday fetch failed: {e}")
        result["exit_reason"] = "data_error"
        return result

    if bars.empty:
        result["exit_reason"] = "no_intraday_data"
        return result

    # ── Build ORB range (9:30 → 9:30+ORB_MINUTES) ──────────────────────────
    mkt_open = bars.index[0].replace(hour=9, minute=30, second=0)
    orb_end  = mkt_open + pd.Timedelta(minutes=ORB_MINUTES)

    orb_bars = bars[(bars.index >= mkt_open) & (bars.index < orb_end)]
    if orb_bars.empty:
        result["exit_reason"] = "no_orb_bars"
        return result

    orb_high = float(orb_bars["high"].max())
    orb_low  = float(orb_bars["low"].min())
    result["orb_high"] = round(orb_high, 2)
    result["orb_low"]  = round(orb_low,  2)

    close_time = bars.index[0].replace(hour=CLOSE_HOUR_ET, minute=CLOSE_MIN_ET, second=0)
    atr_pts    = snap.atr if snap.atr else 5.0
    post_orb   = bars[bars.index >= orb_end]

    # ── Multi-trade loop ─────────────────────────────────────────────────────
    running_equity = equity
    day_pnl        = 0.0
    trades_count   = 0
    last_exit_ts   = None

    while trades_count < MAX_TRADES_PER_DAY:
        # Determine which bars to scan (apply cooldown after a stop)
        if last_exit_ts is not None:
            cooldown_end = last_exit_ts + pd.Timedelta(minutes=TRADE_COOLDOWN_MINUTES)
            scan_bars = post_orb[post_orb.index >= cooldown_end]
        else:
            scan_bars = post_orb

        # Filter to before close_time
        scan_bars = scan_bars[scan_bars.index < close_time]

        if scan_bars.empty:
            break

        t = _simulate_one_trade(
            bars=bars,
            scan_bars=scan_bars,
            snap=snap,
            orb_high=orb_high,
            orb_low=orb_low,
            close_time=close_time,
            atr_pts=atr_pts,
            mkt_open=mkt_open,
        )

        if t["entry_price"] is None:
            break   # no breakout found in remaining bars

        # Calculate P&L for this trade
        risk_pts  = abs(t["entry_price"] - t["stop_loss"])
        risk_pct  = adaptive_risk_pct(snap.atr)
        qty_risk  = math.floor((running_equity * risk_pct) / risk_pts) if risk_pts > 0 else 0
        abs_score = abs(snap.score)
        cap_pct   = POSITION_PCT_BY_SCORE.get(abs_score, POSITION_PCT_BY_SCORE[2])
        qty_cap   = math.floor((running_equity * cap_pct) / t["entry_price"])
        qty       = min(qty_risk, qty_cap)

        if t["side"] == "buy":
            trade_pnl = (t["exit_price"] - t["entry_price"]) * qty
        else:
            trade_pnl = (t["entry_price"] - t["exit_price"]) * qty

        day_pnl        += trade_pnl
        running_equity += trade_pnl
        trades_count   += 1
        last_exit_ts    = t["exit_ts"]

        # Store first trade's details in result for reporting
        if result["entry"] is None:
            result["entry"]       = round(t["entry_price"], 2)
            result["side"]        = t["side"]
            result["stop_loss"]   = round(t["stop_loss"],   2)
            result["take_profit"] = round(t["take_profit"], 2)

        # Always update with the latest exit
        result["exit_price"]  = t["exit_price"]
        result["exit_reason"] = t["exit_reason"]

        # After TP, trailing stop, or EOD: day is done
        if t["exit_reason"] in ("take_profit", "trailing_stop", "eod_close"):
            break
        # After stop_loss / breakeven_stop: try to re-enter (loop continues)

    result["pnl"]          = round(day_pnl, 2)
    result["equity"]       = round(running_equity, 2)
    result["trades_count"] = trades_count

    return result


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info(f"  ORB Backtest — {SYMBOL} — last {BACKTEST_DAYS} trading days")
    log.info("=" * 60)

    broker = Broker()
    days   = get_trading_days(broker, BACKTEST_DAYS)
    log.info(f"Testing {len(days)} days: {days[0]} → {days[-1]}\n")

    equity  = STARTING_EQUITY
    results = []

    for day in days:
        r = simulate_day(broker, day, equity)
        equity = r["equity"]
        results.append(r)

        tc     = r["trades_count"]
        tc_str = f" ×{tc}" if tc > 1 else ""
        status = f"{'✅' if r['pnl'] > 0 else '❌' if r['pnl'] < 0 else '—'}"
        log.info(
            f"{r['date']}  {r['bias']:5}  {r['exit_reason']:14}  "
            f"PnL: {r['pnl']:+8.2f}  Equity: ${r['equity']:,.2f}  {status}{tc_str}"
        )

    # ── Summary ──────────────────────────────────────────────────────────────
    _skipped = {"no_trade", "flat_bias", "low_conviction", "macd_conflict",
                "analysis_error", "data_error", "no_intraday_data", "no_orb_bars",
                "insufficient_history", "low_atr"}

    trade_days  = [r for r in results if r["exit_reason"] not in _skipped]
    skipped_days = [r for r in results if r["exit_reason"] in _skipped]
    wins        = [r for r in trade_days if r["pnl"] > 0]
    losses      = [r for r in trade_days if r["pnl"] < 0]
    total_pnl   = sum(r["pnl"] for r in results)
    win_rate    = len(wins) / len(trade_days) * 100 if trade_days else 0
    avg_win     = sum(r["pnl"] for r in wins)   / len(wins)   if wins   else 0
    avg_loss    = sum(r["pnl"] for r in losses) / len(losses) if losses else 0
    profit_factor = abs(sum(r["pnl"] for r in wins) / sum(r["pnl"] for r in losses)) if losses else float("inf")
    total_trades  = sum(r["trades_count"] for r in results)

    log.info("")
    log.info("=" * 60)
    log.info("  BACKTEST RESULTS")
    log.info("=" * 60)
    log.info(f"  Period:          {days[0]} → {days[-1]}")
    log.info(f"  Starting equity: ${STARTING_EQUITY:,.2f}")
    log.info(f"  Final equity:    ${equity:,.2f}")
    log.info(f"  Total P&L:       ${total_pnl:+,.2f}  ({(total_pnl/STARTING_EQUITY*100):+.2f}%)")
    log.info(f"  Trade days:      {len(trade_days)} / {BACKTEST_DAYS}  (total trades: {total_trades})")
    log.info(f"  Win rate:        {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L, by day P&L)")
    log.info(f"  Avg win:         ${avg_win:+,.2f}")
    log.info(f"  Avg loss:        ${avg_loss:+,.2f}")
    log.info(f"  Profit factor:   {profit_factor:.2f}")

    log.info("")
    log