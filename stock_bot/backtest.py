"""
backtest.py — 30-day historical simulation of the ORB strategy.
Replays each trading day: pre-market analysis → ORB range → breakout entry → TP/SL/EOD exit.
No real orders are placed.

Run:
    python backtest.py
"""

from __future__ import annotations
import logging
import math
import sys
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo

import pandas as pd

from broker import Broker
from analysis import analyse
from config import (
    SYMBOL,
    ORB_MINUTES,
    RISK_PER_TRADE_PCT,
    MAX_STOP_PCT,
    TAKE_PROFIT_RATIO,
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
BACKTEST_DAYS   = 30
CLOSE_HOUR_ET   = 15   # force-close at 15:00 ET
CLOSE_MIN_ET    = 0


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


# ── Simulation ─────────────────────────────────────────────────────────────────

def simulate_day(broker: Broker, day: date, equity: float) -> dict:
    """
    Simulate one trading day. Returns a result dict.
    """
    result = {
        "date":        day,
        "bias":        "—",
        "orb_high":    None,
        "orb_low":     None,
        "entry":       None,
        "side":        None,
        "stop_loss":   None,
        "take_profit": None,
        "exit_price":  None,
        "exit_reason": "no_trade",
        "pnl":         0.0,
        "equity":      equity,
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

    # ── Scan post-ORB bars for breakout ─────────────────────────────────────
    post_orb = bars[bars.index >= orb_end]
    close_time = bars.index[0].replace(hour=CLOSE_HOUR_ET, minute=CLOSE_MIN_ET, second=0)

    entry_price  = None
    side         = None
    stop_loss    = None
    take_profit  = None

    for ts, bar in post_orb.iterrows():
        if ts >= close_time:
            break   # EOD — no new entries

        if entry_price is None:
            # Look for breakout
            if snap.bias == "LONG" and bar["high"] > orb_high:
                entry_price = orb_high + 0.01   # approximate fill at breakout
                raw_stop    = orb_low
                floor_stop  = entry_price * (1 - MAX_STOP_PCT)
                stop_loss   = max(raw_stop, floor_stop)
                risk_pts    = entry_price - stop_loss
                take_profit = entry_price + risk_pts * TAKE_PROFIT_RATIO
                side        = "buy"
                result["entry"]       = round(entry_price, 2)
                result["side"]        = side
                result["stop_loss"]   = round(stop_loss,   2)
                result["take_profit"] = round(take_profit, 2)

            elif snap.bias == "SHORT" and bar["low"] < orb_low:
                entry_price = orb_low - 0.01
                raw_stop    = orb_high
                ceil_stop   = entry_price * (1 + MAX_STOP_PCT)
                stop_loss   = min(raw_stop, ceil_stop)
                risk_pts    = stop_loss - entry_price
                take_profit = entry_price - risk_pts * TAKE_PROFIT_RATIO
                side        = "sell"
                result["entry"]       = round(entry_price, 2)
                result["side"]        = side
                result["stop_loss"]   = round(stop_loss,   2)
                result["take_profit"] = round(take_profit, 2)

        else:
            # Monitor for TP / SL hit
            if side == "buy":
                if bar["high"] >= take_profit:
                    result["exit_price"]  = round(take_profit, 2)
                    result["exit_reason"] = "take_profit"
                    break
                if bar["low"] <= stop_loss:
                    result["exit_price"]  = round(stop_loss, 2)
                    result["exit_reason"] = "stop_loss"
                    break
            else:
                if bar["low"] <= take_profit:
                    result["exit_price"]  = round(take_profit, 2)
                    result["exit_reason"] = "take_profit"
                    break
                if bar["high"] >= stop_loss:
                    result["exit_price"]  = round(stop_loss, 2)
                    result["exit_reason"] = "stop_loss"
                    break

    # EOD close if still in trade
    if entry_price and result["exit_price"] is None:
        eod_bars = post_orb[post_orb.index >= close_time]
        if not eod_bars.empty:
            result["exit_price"]  = round(float(eod_bars.iloc[0]["close"]), 2)
            result["exit_reason"] = "eod_close"
        else:
            result["exit_price"]  = round(float(post_orb.iloc[-1]["close"]), 2)
            result["exit_reason"] = "eod_close"

    # ── Calculate P&L ────────────────────────────────────────────────────────
    if entry_price and result["exit_price"]:
        risk_pts = abs(entry_price - stop_loss)
        qty      = math.floor((equity * RISK_PER_TRADE_PCT) / risk_pts) if risk_pts > 0 else 0
        if side == "buy":
            pnl = (result["exit_price"] - entry_price) * qty
        else:
            pnl = (entry_price - result["exit_price"]) * qty
        result["pnl"]    = round(pnl, 2)
        result["equity"] = round(equity + pnl, 2)

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

        status = f"{'✅' if r['pnl'] > 0 else '❌' if r['pnl'] < 0 else '—'}"
        log.info(
            f"{r['date']}  {r['bias']:5}  {r['exit_reason']:12}  "
            f"PnL: {r['pnl']:+8.2f}  Equity: ${r['equity']:,.2f}  {status}"
        )

    # ── Summary ──────────────────────────────────────────────────────────────
    trades      = [r for r in results if r["exit_reason"] not in ("no_trade", "flat_bias", "analysis_error", "data_error", "no_intraday_data", "no_orb_bars", "insufficient_history")]
    wins        = [r for r in trades if r["pnl"] > 0]
    losses      = [r for r in trades if r["pnl"] < 0]
    total_pnl   = sum(r["pnl"] for r in results)
    win_rate    = len(wins) / len(trades) * 100 if trades else 0
    avg_win     = sum(r["pnl"] for r in wins)   / len(wins)   if wins   else 0
    avg_loss    = sum(r["pnl"] for r in losses) / len(losses) if losses else 0
    profit_factor = abs(sum(r["pnl"] for r in wins) / sum(r["pnl"] for r in losses)) if losses else float("inf")

    log.info("")
    log.info("=" * 60)
    log.info("  BACKTEST RESULTS")
    log.info("=" * 60)
    log.info(f"  Period:         {days[0]} → {days[-1]}")
    log.info(f"  Starting equity: ${STARTING_EQUITY:,.2f}")
    log.info(f"  Final equity:    ${equity:,.2f}")
    log.info(f"  Total P&L:       ${total_pnl:+,.2f}  ({(total_pnl/STARTING_EQUITY*100):+.2f}%)")
    log.info(f"  Trades taken:    {len(trades)} / {BACKTEST_DAYS} days")
    log.info(f"  Win rate:        {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    log.info(f"  Avg win:         ${avg_win:+,.2f}")
    log.info(f"  Avg loss:        ${avg_loss:+,.2f}")
    log.info(f"  Profit factor:   {profit_factor:.2f}")
    log.info("=" * 60)

    # Save to CSV
    df = pd.DataFrame(results)
    df.to_csv("backtest_results.csv", index=False)
    log.info("  Results saved to backtest_results.csv")


if __name__ == "__main__":
    main()
