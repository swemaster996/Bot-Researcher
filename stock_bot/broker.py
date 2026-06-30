"""
broker.py — Alpaca API wrapper.
Handles data fetching, order placement, and position queries.
"""

from __future__ import annotations
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import TimeFrame

from config import API_KEY, API_SECRET, BASE_URL, SYMBOL

log = logging.getLogger(__name__)
ET  = ZoneInfo("America/New_York")


class Broker:
    def __init__(self):
        self.api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version="v2")
        log.info(f"Alpaca connected | account: {self.account().id}")

    # ── Account ────────────────────────────────────────────────────────────────

    def account(self):
        return self.api.get_account()

    def equity(self) -> float:
        return float(self.api.get_account().equity)

    def buying_power(self) -> float:
        return float(self.api.get_account().buying_power)

    # ── Market data ────────────────────────────────────────────────────────────

    def daily_bars(self, symbol: str, days: int = 250) -> pd.DataFrame:
        """Fetch `days` daily bars, return clean OHLCV DataFrame."""
        end   = datetime.now(ET).date()
        start = end - timedelta(days=days + 50)   # buffer for weekends/holidays

        bars = self.api.get_bars(
            symbol,
            TimeFrame.Day,
            start=str(start),
            end=str(end),
            limit=days,
            adjustment="all",   # split/dividend adjusted
            feed="iex",         # free tier data feed
        ).df

        bars.index = pd.to_datetime(bars.index).tz_localize(None)
        bars = bars[["open", "high", "low", "close", "volume"]].tail(days)
        log.info(f"Fetched {len(bars)} daily bars for {symbol}")
        return bars

    def intraday_bars(self, symbol: str, minutes: int = 1, lookback_min: int = 120) -> pd.DataFrame:
        """Fetch recent 1-min bars (for ORB window)."""
        end   = datetime.now(ET)
        start = end - timedelta(minutes=lookback_min)

        bars = self.api.get_bars(
            symbol,
            TimeFrame.Minute,
            start=start.isoformat(),
            end=end.isoformat(),
            feed="iex",         # free tier data feed
        ).df

        idx = pd.to_datetime(bars.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        bars.index = idx.tz_convert(ET)
        bars = bars[["open", "high", "low", "close", "volume"]]
        return bars

    def latest_price(self, symbol: str) -> float:
        trade = self.api.get_latest_trade(symbol, feed="iex")
        return float(trade.price)

    # ── Orders ─────────────────────────────────────────────────────────────────

    def market_order(
        self,
        symbol: str,
        side: str,          # "buy" or "sell"
        qty: int,
    ):
        log.info(f"MARKET ORDER → {side.upper()} {qty}x {symbol}")
        return self.api.submit_order(
            symbol=symbol,
            qty=qty,
            side=side,
            type="market",
            time_in_force="day",
        )

    def bracket_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        take_profit: float,
        stop_loss: float,
    ):
        """
        Submit a bracket order (entry + TP + SL in one call).
        side: "buy" (long) or "sell" (short)
        """
        log.info(
            f"BRACKET ORDER → {side.upper()} {qty}x {symbol} "
            f"| TP={take_profit:.2f}  SL={stop_loss:.2f}"
        )
        order_side  = side
        tp_side     = "sell" if side == "buy" else "buy"

        return self.api.submit_order(
            symbol=symbol,
            qty=qty,
            side=order_side,
            type="market",
            time_in_force="day",
            order_class="bracket",
            take_profit={"limit_price": round(take_profit, 2)},
            stop_loss={"stop_price": round(stop_loss, 2)},
        )

    def close_position(self, symbol: str):
        try:
            self.api.close_position(symbol)
            log.info(f"Closed position: {symbol}")
        except Exception as e:
            log.warning(f"close_position({symbol}): {e}")

    def close_all_positions(self):
        self.api.close_all_positions()
        log.info("All positions closed.")

    def cancel_all_orders(self):
        self.api.cancel_all_orders()
        log.info("All open orders cancelled.")

    # ── Position queries ───────────────────────────────────────────────────────

    def get_position(self, symbol: str):
        try:
            return self.api.get_position(symbol)
        except tradeapi.rest.APIError:
            return None

    def position_count(self) -> int:
        return len(self.api.list_positions())

    def get_open_orders(self, symbol: str = None) -> list:
        """Return list of open orders, optionally filtered by symbol."""
        orders = self.api.list_orders(status="open")
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        return orders

    def move_stop(self, order_id: str, new_stop: float) -> None:
        """Raise (or lower) a stop order to new_stop price."""
        self.api.replace_order(order_id, stop_price=round(new_stop, 2))
        log.info(f"Stop order moved → {new_stop:.2f}")

    # ── Clock helpers ──────────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        return self.api.get_clock().is_open

    def next_open(self) -> datetime:
        return self.api.get_clock().next_open
