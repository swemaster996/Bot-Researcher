"""
database.py — Supabase logger for NorthstarAI.
Saves every analysis, signal and trade so NorthstarAI can read them.
"""

from __future__ import annotations
import logging
from datetime import datetime, date
from typing import Optional

from supabase import create_client, Client
from analysis import MarketSnapshot
from config import SUPABASE_URL, SUPABASE_KEY

log = logging.getLogger(__name__)


class BotDatabase:
    def __init__(self):
        self.client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        log.info("Supabase connected ✅")

    # ── Log daily analysis ─────────────────────────────────────────────────────

    def log_analysis(self, snap: MarketSnapshot) -> None:
        try:
            self.client.table("bot_analyses").insert({
                "symbol":     snap.symbol,
                "date":       str(snap.timestamp.date()),
                "close":      snap.close,
                "ema20":      snap.ema20,
                "ema50":      snap.ema50,
                "ema200":     snap.ema200,
                "rsi":        snap.rsi,
                "macd_hist":  snap.macd_hist,
                "atr":        snap.atr,
                "bb_upper":   snap.bb_upper,
                "bb_lower":   snap.bb_lower,
                "support":    snap.support,
                "resistance": snap.resistance,
                "bull_score": snap.score,
                "bias":       snap.bias,
                "notes":      snap.notes,
            }).execute()
            log.info(f"Supabase: analys sparad ({snap.symbol} {snap.bias})")
        except Exception as e:
            log.error(f"Supabase log_analysis failed: {e}")

    # ── Log trade signal ───────────────────────────────────────────────────────

    def log_signal(
        self,
        symbol: str,
        signal: str,
        price: float,
        orb_high: Optional[float] = None,
        orb_low:  Optional[float] = None,
        reason:   str = "",
    ) -> None:
        try:
            self.client.table("bot_signals").insert({
                "symbol":   symbol,
                "signal":   signal,
                "price":    price,
                "orb_high": orb_high,
                "orb_low":  orb_low,
                "reason":   reason,
            }).execute()
            log.info(f"Supabase: signal sparad ({signal} @ {price})")
        except Exception as e:
            log.error(f"Supabase log_signal failed: {e}")

    # ── Log trade entry ────────────────────────────────────────────────────────

    def log_trade_open(
        self,
        symbol:      str,
        side:        str,
        qty:         int,
        entry_price: float,
        stop_loss:   float,
        take_profit: float,
        risk_usd:    float,
    ) -> int:
        """Returns the trade row ID for later update on close."""
        try:
            res = self.client.table("bot_trades").insert({
                "symbol":      symbol,
                "side":        side,
                "qty":         qty,
                "entry_price": entry_price,
                "stop_loss":   stop_loss,
                "take_profit": take_profit,
                "risk_usd":    risk_usd,
                "status":      "open",
            }).execute()
            trade_id = res.data[0]["id"]
            log.info(f"Supabase: trade öppnad (id={trade_id})")
            return trade_id
        except Exception as e:
            log.error(f"Supabase log_trade_open failed: {e}")
            return -1

    # ── Log equity snapshot ────────────────────────────────────────────────────

    def log_equity(self, equity: float) -> None:
        try:
            self.client.table("bot_equity").insert({
                "equity": round(equity, 2),
            }).execute()
            log.debug(f"Supabase: equity sparad (${equity:,.2f})")
        except Exception as e:
            log.error(f"Supabase log_equity failed: {e}")

    # ── Log trade close ────────────────────────────────────────────────────────

    def log_trade_close(
        self,
        trade_id:   int,
        exit_price: float,
        pnl:        float,
    ) -> None:
        if trade_id < 0:
            return
        try:
            self.client.table("bot_trades").update({
                "status":     "closed",
                "exit_price": exit_price,
                "pnl":        pnl,
                "closed_at":  datetime.utcnow().isoformat(),
            }).eq("id", trade_id).execute()
            log.info(f"Supabase: trade stängd (id={trade_id}, PnL={pnl:+.2f})")
        except Exception as e:
            log.error(f"Supabase log_trade_close failed: {e}")
