"""
trade_lib.py — Trade execution and risk management for the S&P 500 reversal bot.

TradeManager — Places, monitors, and closes SPY limit orders via IBKR.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder

from .signal_lib import ClaudeVerdict, ReversalSignal

logger = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("America/New_York")


class TradeManager:
    """Manages a single intraday SPY reversal trade at a time."""

    def __init__(
        self,
        ib: IB,
        instrument: str = "SPY",
        max_amm_per_trade: float = 10000,
        stop_buffer_pct: float = 0.20,
        target_pct: float = 0.50,
        journal=None,
    ) -> None:
        self._ib = ib
        self._instrument = instrument
        self._max_amm = max_amm_per_trade
        self._stop_buffer_pct = stop_buffer_pct
        self._target_pct = target_pct
        self._journal = journal

        self._contract = Stock(instrument, "SMART", "USD")
        self._active_trade: dict | None = None

    @property
    def has_position(self) -> bool:
        return self._active_trade is not None

    def enter(self, signal: ReversalSignal, verdict: ClaudeVerdict) -> bool:
        if self.has_position:
            logger.warning("Already in a position — skipping entry.")
            return False

        entry_price = signal.snapshot.spy_price
        if entry_price is None:
            logger.error("No SPY price — cannot enter.")
            return False

        stop_price  = verdict.stop_level or entry_price * (1 - self._stop_buffer_pct / 100)
        target_price = verdict.target_level or entry_price * (1 + self._target_pct / 100)

        quantity = max(1, int(self._max_amm / entry_price))
        limit_price = round(entry_price * 1.001, 2)  # small slip above current price

        try:
            self._ib.qualifyContracts(self._contract)
            order = LimitOrder("BUY", quantity, limit_price)
            order.orderRef = f"SP500Reversal_{datetime.now(MARKET_TZ).strftime('%Y%m%d_%H%M')}"
            trade = self._ib.placeOrder(self._contract, order)
            logger.info(
                "Entry order placed: BUY %d %s @ %.2f  stop=%.2f  target=%.2f",
                quantity, self._instrument, limit_price, stop_price, target_price,
            )
            self._active_trade = {
                "order_ref": order.orderRef,
                "quantity": quantity,
                "entry_price": limit_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "trade": trade,
                "entered_at": datetime.now(MARKET_TZ),
            }
            if self._journal:
                self._journal.log_entry(self._active_trade, signal, verdict)
            return True
        except Exception as exc:
            logger.error("Failed to place entry order: %s", exc)
            return False

    def check_stops_and_targets(self, current_price: float) -> None:
        if not self.has_position or current_price is None:
            return

        stop   = self._active_trade["stop_price"]
        target = self._active_trade["target_price"]

        if current_price <= stop:
            logger.info("Stop hit at %.2f — closing position.", current_price)
            self._close("stop hit")
        elif current_price >= target:
            logger.info("Target hit at %.2f — closing position.", current_price)
            self._close("target hit")

    def close_all(self, reason: str = "end of session") -> None:
        if self.has_position:
            self._close(reason)

    def _close(self, reason: str) -> None:
        if not self._active_trade:
            return
        qty = self._active_trade["quantity"]
        try:
            order = MarketOrder("SELL", qty)
            self._ib.placeOrder(self._contract, order)
            logger.info("Closed %d %s — reason: %s", qty, self._instrument, reason)
            if self._journal:
                self._journal.log_exit(self._active_trade, reason)
        except Exception as exc:
            logger.error("Failed to close position: %s", exc)
        finally:
            self._active_trade = None
