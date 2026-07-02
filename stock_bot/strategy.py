"""
strategy.py — Opening Range Breakout (ORB) strategy.
Collects the first 15 min, sets breakout levels, executes with risk management.
"""

from __future__ import annotations
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from analysis import MarketSnapshot, Bias
from broker import Broker
from config import (
    SYMBOL,
    ORB_MINUTES,
    RISK_PER_TRADE_PCT,
    MAX_POSITION_PCT,
    MAX_STOP_PCT,
    TAKE_PROFIT_RATIO,
    MAX_OPEN_POSITIONS,
    MAX_TRADES_PER_DAY,
    TRADE_COOLDOWN_MINUTES,
)

log = logging.getLogger(__name__)
ET  = ZoneInfo("America/New_York")


@dataclass
class OrbRange:
    high:  float
    low:   float
    size:  float    # high - low

    def breakout_long(self,  price: float) -> bool: return price > self.high
    def breakout_short(self, price: float) -> bool: return price < self.low


@dataclass
class TradeSetup:
    side:        str    # "buy" or "sell"
    entry:       float
    stop_loss:   float
    take_profit: float
    qty:         int
    risk_usd:    float


class OrbStrategy:
    """
    Workflow:
      1. build_range()  — called at ORB_END_TIME with 15-min intraday bars
      2. check_entry()  — called every minute; places order on first breakout
      3. monitor()      — called every minute while in trade
    """

    def __init__(self, broker: Broker, snapshot: MarketSnapshot, db=None):
        self.broker   = broker
        self.snapshot = snapshot
        self.db       = db
        self.orb: Optional[OrbRange]    = None
        self.setup: Optional[TradeSetup] = None
        self.in_trade     = False
        self.trade_id     = -1
        self.highest_seen = 0.0      # highest price since entry (LONG trailing)
        self.lowest_seen  = 999999.  # lowest price since entry  (SHORT trailing)
        self.current_stop = 0.0      # tracks where stop currently is
        self.stop_phase   = "initial"  # "initial" → "breakeven" → "trailing"
        self.trades_today    = 0                     # entries taken today
        self.cooldown_until: datetime | None = None  # re-entry blocked until this time

    # ── Phase 1: build opening range ──────────────────────────────────────────

    def build_range(self) -> OrbRange:
        bars = self.broker.intraday_bars(SYMBOL, lookback_min=ORB_MINUTES + 5)

        # keep only bars from 09:30 up to ORB_MINUTES
        market_open = bars.index[0].replace(hour=9, minute=30, second=0)
        orb_end     = market_open + pd.Timedelta(minutes=ORB_MINUTES)
        orb_bars    = bars[(bars.index >= market_open) & (bars.index < orb_end)]

        if orb_bars.empty:
            raise RuntimeError("No intraday bars found for ORB window.")

        high = float(orb_bars["high"].max())
        low  = float(orb_bars["low"].min())
        size = round(high - low, 4)

        self.orb = OrbRange(high=round(high, 2), low=round(low, 2), size=size)

        log.info(
            f"ORB built | High={self.orb.high:.2f}  Low={self.orb.low:.2f}  "
            f"Size={size:.2f}  Bias={self.snapshot.bias}"
        )
        return self.orb

    # ── Phase 2: check for breakout entry ─────────────────────────────────────

    def check_entry(self) -> bool:
        """
        Returns True if an order was placed.
        Respects MAX_TRADES_PER_DAY and cooldown after each close.
        """
        if self.in_trade or self.orb is None:
            return False

        if self.trades_today >= MAX_TRADES_PER_DAY:
            return False

        if self.cooldown_until and datetime.now(ET) < self.cooldown_until:
            return False

        if self.broker.position_count() >= MAX_OPEN_POSITIONS:
            return False

        price  = self.broker.latest_price(SYMBOL)
        equity = self.broker.equity()
        bias: Bias = self.snapshot.bias

        setup = None

        if bias == "LONG" and self.orb.breakout_long(price):
            setup = self._build_long_setup(price, equity)

        elif bias == "SHORT" and self.orb.breakout_short(price):
            setup = self._build_short_setup(price, equity)

        if setup is None:
            return False

        if setup.qty < 1:
            log.warning("Position size rounds to 0 — skipping trade.")
            return False

        self._execute(setup)
        return True

    # ── Phase 3: monitor open position ────────────────────────────────────────

    def monitor(self) -> None:
        """Check if position still exists; log unrealised P&L; trail stop."""
        pos = self.broker.get_position(SYMBOL)
        if pos is None:
            if self.in_trade:
                log.info("Position closed (TP/SL hit or manual) — logging to Supabase.")
                self._on_position_closed()
                self.in_trade = False
            return

        price  = float(pos.current_price)
        unreal = float(pos.unrealized_pl)
        pct    = float(pos.unrealized_plpc) * 100
        log.info(
            f"Position | {pos.side.upper()} {pos.qty}x {SYMBOL} "
            f"@ {float(pos.avg_entry_price):.2f} | "
            f"P&L: {'+'if unreal>=0 else ''}{unreal:.2f} ({pct:+.2f}%) | "
            f"Stop: {self.current_stop:.2f} [{self.stop_phase}]"
        )

        if self.setup:
            self._trail_stop(price)

    def _on_position_closed(self) -> None:
        """Fetch exit price from Alpaca orders and log trade close to Supabase."""
        if not self.db or self.trade_id < 0 or not self.setup:
            return
        try:
            close_side = "sell" if self.setup.side == "buy" else "buy"
            # Find the most recent filled closing order
            orders = self.broker.api.list_orders(
                status="closed", limit=10, direction="desc"
            )
            exit_price = None
            for o in orders:
                if o.symbol == SYMBOL and o.side == close_side and o.filled_avg_price:
                    exit_price = float(o.filled_avg_price)
                    log.info(f"Exit found via order {o.id[:8]}: ${exit_price:.2f}")
                    break

            if exit_price is None:
                exit_price = self.broker.latest_price(SYMBOL)
                log.info(f"Exit fallback to latest price: ${exit_price:.2f}")

            qty = self.setup.qty
            pnl = ((exit_price - self.setup.entry) if self.setup.side == "buy"
                   else (self.setup.entry - exit_price)) * qty
            self.db.log_trade_close(self.trade_id, exit_price, round(pnl, 2))
            log.info(f"Supabase: trade close logged — exit=${exit_price:.2f} P&L={pnl:+.2f}")
        except Exception as e:
            log.warning(f"_on_position_closed failed: {e}")

        # Increment daily counter and set re-entry cooldown
        self.trades_today += 1
        self.cooldown_until = datetime.now(ET) + timedelta(minutes=TRADE_COOLDOWN_MINUTES)
        log.info(
            f"Trade {self.trades_today}/{MAX_TRADES_PER_DAY} done today. "
            f"Cooldown until {self.cooldown_until.strftime('%H:%M')} ET"
        )

    # ── Trailing stop ─────────────────────────────────────────────────────────

    def _trail_stop(self, price: float) -> None:
        """
        Two-phase trailing stop:
          Phase 1 — break-even: once unrealised profit >= 1R, move stop to entry.
          Phase 2 — trailing:   once >= 1.5R, trail stop 0.5 × ATR below highest.
        Stop only ever moves in the favourable direction.
        """
        if self.setup is None:
            return

        atr       = self.snapshot.atr if self.snapshot.atr else 1.0
        entry     = self.setup.entry
        risk_pts  = abs(entry - self.setup.stop_loss)   # 1R in price points

        if self.setup.side == "buy":
            # Track highest price seen
            if price > self.highest_seen:
                self.highest_seen = price

            profit_pts = self.highest_seen - entry

            # Phase 1: break-even at 0.5R
            if self.stop_phase == "initial" and profit_pts >= risk_pts * 0.5:
                new_stop = entry + 0.05
                self._apply_stop(new_stop, "breakeven")

            # Phase 2: trailing at 1R
            elif self.stop_phase in ("initial", "breakeven") and profit_pts >= risk_pts:
                new_stop = self.highest_seen - atr * 0.3
                if new_stop > self.current_stop + 0.10:
                    self._apply_stop(new_stop, "trailing")

            elif self.stop_phase == "trailing":
                new_stop = self.highest_seen - atr * 0.3
                if new_stop > self.current_stop + 0.10:
                    self._apply_stop(new_stop, "trailing")

        elif self.setup.side == "sell":
            if price < self.lowest_seen:
                self.lowest_seen = price

            profit_pts = entry - self.lowest_seen

            if self.stop_phase == "initial" and profit_pts >= risk_pts * 0.5:
                new_stop = entry - 0.05
                self._apply_stop(new_stop, "breakeven")

            elif self.stop_phase in ("initial", "breakeven") and profit_pts >= risk_pts:
                new_stop = self.lowest_seen + atr * 0.3
                if new_stop < self.current_stop - 0.10:
                    self._apply_stop(new_stop, "trailing")

            elif self.stop_phase == "trailing":
                new_stop = self.lowest_seen + atr * 0.3
                if new_stop < self.current_stop - 0.10:
                    self._apply_stop(new_stop, "trailing")

    def _apply_stop(self, new_stop: float, phase: str) -> None:
        """Find the open stop order and move it to new_stop."""
        try:
            orders = self.broker.get_open_orders(SYMBOL)
            for order in orders:
                if getattr(order, "type", "") == "stop":
                    self.broker.move_stop(order.id, new_stop)
                    log.info(
                        f"🔒 Stop raised → {new_stop:.2f}  "
                        f"(phase: {self.stop_phase} → {phase})"
                    )
                    self.current_stop = new_stop
                    self.stop_phase   = phase
                    return
            log.debug("No open stop order found to trail.")
        except Exception as e:
            log.warning(f"Trail stop failed: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_long_setup(self, entry: float, equity: float) -> TradeSetup:
        # Stop = below ORB low; capped by MAX_STOP_PCT
        raw_stop   = self.orb.low
        floor_stop = entry * (1 - MAX_STOP_PCT)
        stop       = max(raw_stop, floor_stop)
        risk_pts   = entry - stop

        tp        = entry + risk_pts * TAKE_PROFIT_RATIO
        risk_usd  = equity * RISK_PER_TRADE_PCT

        # Size by risk (1%), then hard-cap by MAX_POSITION_PCT to prevent all-in
        qty_risk  = math.floor(risk_usd / risk_pts) if risk_pts > 0 else 0
        qty_cap   = math.floor((equity * MAX_POSITION_PCT) / entry)
        qty       = min(qty_risk, qty_cap)

        if qty < qty_risk:
            log.info(
                f"Position cap applied: risk-sized qty={qty_risk} → capped to {qty} "
                f"(max {MAX_POSITION_PCT*100:.0f}% of equity = ${equity*MAX_POSITION_PCT:,.0f})"
            )

        return TradeSetup("buy", entry, stop, tp, qty, risk_usd)

    def _build_short_setup(self, entry: float, equity: float) -> TradeSetup:
        # Stop = above ORB high; capped by MAX_STOP_PCT
        raw_stop   = self.orb.high
        ceil_stop  = entry * (1 + MAX_STOP_PCT)
        stop       = min(raw_stop, ceil_stop)
        risk_pts   = stop - entry

        tp        = entry - risk_pts * TAKE_PROFIT_RATIO
        risk_usd  = equity * RISK_PER_TRADE_PCT

        # Size by risk (1%), then hard-cap by MAX_POSITION_PCT to prevent all-in
        qty_risk  = math.floor(risk_usd / risk_pts) if risk_pts > 0 else 0
        qty_cap   = math.floor((equity * MAX_POSITION_PCT) / entry)
        qty       = min(qty_risk, qty_cap)

        if qty < qty_risk:
            log.info(
                f"Position cap applied: risk-sized qty={qty_risk} → capped to {qty} "
                f"(max {MAX_POSITION_PCT*100:.0f}% of equity = ${equity*MAX_POSITION_PCT:,.0f})"
            )

        return TradeSetup("sell", entry, stop, tp, qty, risk_usd)

    def _execute(self, setup: TradeSetup) -> None:
        self.setup = setup
        log.info(
            f"ENTERING {setup.side.upper()} | qty={setup.qty} "
            f"entry≈{setup.entry:.2f} | SL={setup.stop_loss:.2f} "
            f"| TP={setup.take_profit:.2f} | risk=${setup.risk_usd:.0f}"
        )

        self.broker.bracket_order(
            symbol=SYMBOL,
            side=setup.side,
            qty=setup.qty,
            take_profit=setup.take_profit,
            stop_loss=setup.stop_loss,
        )

        if self.db:
            self.trade_id = self.db.log_trade_open(
                symbol=SYMBOL, side=setup.side, qty=setup.qty,
                entry_price=setup.entry, stop_loss=setup.stop_loss,
                take_profit=setup.take_profit, risk_usd=setup.risk_usd,
            )
            self.db.log_signal(SYMBOL, setup.side.upper(), setup.entry,
                               self.orb.high if self.orb else None,
                               self.orb.low  if self.orb else None,
                               "ORB breakout")

        self.in_trade     = True
        self.current_stop = setup.stop_loss
        self.highest_seen = setup.entry
        self.lowest_seen  = setup.entry
        self.stop_phase   = "initial"

    def reset_for_new_day(self) -> None:
        self.orb             = None
        self.setup           = None
        self.in_trade        = False
        self.trade_id        = -1
        self.highest_seen    = 0.0
        self.lowest_seen     = 999999.
        self.current_stop    = 0.0
        self.stop_phase      = "initial"
        self.trades_today    = 0
        self.cooldown_until  = None
        log.info("Strategy reset for new trading day.")
