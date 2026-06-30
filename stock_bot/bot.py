"""
bot.py — Main loop and scheduler.
Run:  python bot.py

Schedule (all times Eastern / New York):
  08:00  Pre-market analysis
  09:30  Market opens — start collecting opening range
  09:45  ORB complete — watch for breakout every 60 s
  15:55  Close all positions, cancel orders
  17:00  After-hours analysis (prep for tomorrow)
"""

from __future__ import annotations
import logging
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import schedule

from analysis import analyse, MarketSnapshot
from broker import Broker
from database import BotDatabase
from strategy import OrbStrategy
from config import (
    SYMBOL,
    PRE_MARKET_ANALYSIS_TIME,
    ORB_START_TIME,
    ORB_END_TIME,
    MONITOR_UNTIL,
    CLOSE_ALL_TIME,
    AFTER_HOURS_ANALYSIS,
    LOG_FILE,
)

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)
ET  = ZoneInfo("America/New_York")

# ── Global state ───────────────────────────────────────────────────────────────
broker:   Broker        | None = None
db:       BotDatabase   | None = None
snap:     MarketSnapshot | None = None
strategy: OrbStrategy   | None = None

monitoring      = False    # True from ORB end until market close
orb_building    = False    # True from market open until ORB end
_equity_tick    = 0        # counter for equity logging (every 5 min)


# ── Scheduled jobs ─────────────────────────────────────────────────────────────

def job_pre_market() -> None:
    """08:00 ET — full technical analysis, generate daily bias."""
    global snap, strategy
    log.info("=== PRE-MARKET ANALYSIS STARTING ===")
    try:
        df   = broker.daily_bars(SYMBOL, days=250)
        snap = analyse(df, SYMBOL)
        db.log_analysis(snap)          # → Supabase
        # strategy will be instantiated fresh each day
        strategy = OrbStrategy(broker, snap, db)
        log.info(f"Pre-market done | Bias for today: {snap.bias}")
    except Exception as e:
        log.error(f"Pre-market analysis failed: {e}", exc_info=True)


def job_market_open() -> None:
    """09:30 ET — market just opened, mark that we're in the ORB collection window."""
    global orb_building, monitoring
    log.info("=== MARKET OPEN — collecting opening range ===")
    orb_building = True
    monitoring   = False


def job_orb_end() -> None:
    """09:45 ET — ORB window done; build range and start monitoring."""
    global orb_building, monitoring
    if strategy is None:
        log.warning("Strategy not initialised — skipping ORB.")
        return

    log.info("=== ORB WINDOW CLOSED — building range ===")
    orb_building = False

    try:
        strategy.build_range()
        monitoring = True
        log.info("ORB built — monitoring for breakout every 60 s")
    except Exception as e:
        log.error(f"build_range failed: {e}", exc_info=True)


def job_monitor() -> None:
    """
    Runs every 60 s from 09:45 until 15:55.
    Checks for breakout entry (if not yet in trade) or monitors open position.
    Logs account equity to Supabase every 5 minutes.
    """
    global _equity_tick

    if not monitoring:
        return
    if strategy is None:
        return

    now = datetime.now(ET).strftime("%H:%M")
    if now >= MONITOR_UNTIL:
        return

    try:
        if not strategy.in_trade:
            strategy.check_entry()
        else:
            strategy.monitor()
    except Exception as e:
        log.error(f"Monitor tick error: {e}", exc_info=True)

    # Log equity every 5 minutes (every 5th tick)
    _equity_tick += 1
    if _equity_tick >= 5:
        _equity_tick = 0
        try:
            db.log_equity(broker.equity())
        except Exception as e:
            log.warning(f"Equity log failed: {e}")


def job_close_all() -> None:
    """15:55 ET — force-close everything before market close."""
    global monitoring
    log.info("=== END OF DAY — closing all positions ===")
    monitoring = False

    try:
        broker.cancel_all_orders()
        broker.close_all_positions()
    except Exception as e:
        log.error(f"EOD close failed: {e}", exc_info=True)

    if strategy:
        strategy.reset_for_new_day()


def job_after_hours() -> None:
    """17:00 ET — re-run analysis on today's closed data (prep for tomorrow)."""
    log.info("=== AFTER-HOURS ANALYSIS ===")
    try:
        df   = broker.daily_bars(SYMBOL, days=250)
        snap_ah = analyse(df, SYMBOL)
        log.info(
            f"Tomorrow's pre-bias (provisional): {snap_ah.bias} "
            f"| score={snap_ah.score:+d}"
        )
    except Exception as e:
        log.error(f"After-hours analysis failed: {e}", exc_info=True)


# ── Scheduler setup ────────────────────────────────────────────────────────────

def _et_to_local(et_hhmm: str) -> str:
    """
    Convert a time string like '08:00' (Eastern Time) to the equivalent
    local system time string, so the schedule library fires at the right moment
    regardless of which timezone the host computer is in.
    """
    h, m = map(int, et_hhmm.split(":"))
    today = datetime.now().date()
    et_aware = datetime(today.year, today.month, today.day, h, m, tzinfo=ET)
    local_str = et_aware.astimezone().strftime("%H:%M")
    return local_str


def setup_schedule() -> None:
    t_pre   = _et_to_local(PRE_MARKET_ANALYSIS_TIME)
    t_open  = _et_to_local(ORB_START_TIME)
    t_orb   = _et_to_local(ORB_END_TIME)
    t_close = _et_to_local(CLOSE_ALL_TIME)
    t_ah    = _et_to_local(AFTER_HOURS_ANALYSIS)

    schedule.every().day.at(t_pre).do(job_pre_market)
    schedule.every().day.at(t_open).do(job_market_open)
    schedule.every().day.at(t_orb).do(job_orb_end)
    schedule.every(60).seconds.do(job_monitor)          # continuous monitor tick
    schedule.every().day.at(t_close).do(job_close_all)
    schedule.every().day.at(t_ah).do(job_after_hours)

    log.info("Schedule loaded (local time → ET):")
    log.info(f"  {t_pre}  Pre-market analysis  ({PRE_MARKET_ANALYSIS_TIME} ET)")
    log.info(f"  {t_open}  Market open / ORB starts  ({ORB_START_TIME} ET)")
    log.info(f"  {t_orb}  ORB closes → watch for breakout  ({ORB_END_TIME} ET)")
    log.info(f"  {t_close}  Close all positions  ({CLOSE_ALL_TIME} ET)")
    log.info(f"  {t_ah}  After-hours analysis  ({AFTER_HOURS_ANALYSIS} ET)")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    global broker, db

    log.info("╔══════════════════════════════════════════╗")
    log.info("║   SPY ORB Trading Bot  —  paper mode     ║")
    log.info("╚══════════════════════════════════════════╝")

    # Connect broker + database
    try:
        broker = Broker()
        db     = BotDatabase()
    except Exception as e:
        log.critical(f"Cannot connect: {e}")
        sys.exit(1)

    acc = broker.account()
    log.info(f"Account equity: ${float(acc.equity):,.2f}  |  "
             f"Buying power: ${float(acc.buying_power):,.2f}")

    # Log starting equity snapshot
    try:
        db.log_equity(broker.equity())
    except Exception as e:
        log.warning(f"Initial equity log failed: {e}")

    # Run pre-market analysis immediately on startup (useful if bot starts late)
    job_pre_market()

    setup_schedule()
    log.info("Bot running — waiting for next scheduled event …")

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
