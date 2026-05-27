"""
journal_lib.py — Intraday trading journal for the S&P 500 reversal bot.

TradingJournal — Appends structured events to a daily JSON file.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("America/New_York")


class TradingJournal:
    """Writes structured trading events to journal/<bot>/YYYY-MM-DD.json."""

    def __init__(self, bot_name: str = "sp500trader", base_dir: str = "journal") -> None:
        self._bot = bot_name
        self._base_dir = Path(base_dir) / bot_name
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._date_str = datetime.now(MARKET_TZ).strftime("%Y-%m-%d")
        self._path = self._base_dir / f"{self._date_str}.json"
        self._data = self._load()
        logger.info("Journal opened: %s", self._path)

    def _load(self) -> dict:
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.error("Failed to load journal: %s", exc)
        return {
            "date": self._date_str,
            "bot_sessions": [],
            "signals_detected": [],
            "claude_verdicts": [],
            "trades": [],
        }

    def _save(self) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, default=str)

    def log_session_start(self) -> None:
        self._data["bot_sessions"].append({
            "start": datetime.now(MARKET_TZ).isoformat(),
            "stop": None,
        })
        self._save()

    def log_session_stop(self) -> None:
        sessions = self._data["bot_sessions"]
        if sessions and sessions[-1]["stop"] is None:
            sessions[-1]["stop"] = datetime.now(MARKET_TZ).isoformat()
        self._save()

    def log_signal(self, signal, periscope_data=None) -> None:
        snap = signal.snapshot
        bid, ask = snap.spy_bid, snap.spy_ask
        self._data["signals_detected"].append({
            "timestamp": signal.timestamp.isoformat(),
            "spy_price": snap.spy_price,
            "spy_bid": snap.spy_bid,
            "spy_ask": snap.spy_ask,
            "spy_spread": round(ask - bid, 4) if bid is not None and ask is not None else None,
            "spy_opening_gap_pct": snap.spy_opening_gap_pct,
            "es_price": snap.es_price,
            "tick": snap.tick,
            "vix": snap.vix,
            "conditions_met": signal.conditions_met,
            "tick_flush": signal.tick_flush,
            "tick_snap": signal.tick_snap,
            "vix_divergence": signal.vix_divergence,
            "volume_exhaustion": signal.volume_exhaustion,
            "candle_structure": signal.candle_structure,
            "support_level": signal.support_level,
            "periscope_summary": periscope_data.summary() if periscope_data is not None else None,
        })
        self._save()

    def log_claude_verdict(self, signal, verdict) -> None:
        self._data["claude_verdicts"].append({
            "timestamp": signal.timestamp.isoformat(),
            "spy_price": signal.snapshot.spy_price,
            "spy_opening_gap_pct": signal.snapshot.spy_opening_gap_pct,
            "es_price": signal.snapshot.es_price,
            "conditions_met": signal.conditions_met,
            "go": verdict.go,
            "confidence": verdict.confidence,
            "support_level": verdict.support_level,
            "stop_level": verdict.stop_level,
            "target_level": verdict.target_level,
            "reasoning": verdict.reasoning,
            "raw_response": verdict.raw_response,
            "spy_price_at_eod": None,   # filled in by log_eod_snapshot
        })
        self._save()

    def log_eod_snapshot(self, spy_price: float | None, es_price: float | None = None) -> None:
        """Record closing prices and back-fill them into all open verdicts for the day."""
        self._data["eod_snapshot"] = {
            "timestamp": datetime.now(MARKET_TZ).isoformat(),
            "spy_price": spy_price,
            "es_price": es_price,
        }
        for verdict in self._data["claude_verdicts"]:
            if verdict.get("spy_price_at_eod") is None:
                verdict["spy_price_at_eod"] = spy_price
        self._save()

    def log_entry(self, trade: dict, signal, verdict) -> None:
        self._data["trades"].append({
            "order_ref": trade["order_ref"],
            "action": "entry",
            "timestamp": trade["entered_at"].isoformat(),
            "instrument": "SPY",
            "quantity": trade["quantity"],
            "entry_price": trade["entry_price"],
            "stop_price": trade["stop_price"],
            "target_price": trade["target_price"],
            "conditions_met": signal.conditions_met,
            "claude_confidence": verdict.confidence,
            "exit_price": None,
            "exit_reason": None,
            "pnl": None,
        })
        self._save()

    def log_exit(self, trade: dict, reason: str, exit_price: float | None = None) -> None:
        for t in self._data["trades"]:
            if t["order_ref"] == trade["order_ref"] and t["exit_reason"] is None:
                t["exit_price"] = exit_price
                t["exit_reason"] = reason
                if exit_price and t["entry_price"]:
                    t["pnl"] = round((exit_price - t["entry_price"]) * t["quantity"], 2)
                break
        self._save()
