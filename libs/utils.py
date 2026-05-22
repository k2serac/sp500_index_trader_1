"""
utils.py — Market timing and support-level helpers for the S&P 500 reversal bot.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")

RTH_OPEN  = dt_time(9, 30)
RTH_CLOSE = dt_time(16, 0)

# First screenshot at 9:21 — 1 min after the 9:20 Periscope update, before RTH open.
PERISCOPE_SNAPSHOT_START = dt_time(9, 21)


def in_periscope_window(now: datetime) -> bool:
    """True during the window when Periscope screenshots should be taken."""
    t = now.time()
    return PERISCOPE_SNAPSHOT_START <= t < RTH_CLOSE


def is_rth(now: datetime) -> bool:
    t = now.time()
    return RTH_OPEN <= t < RTH_CLOSE


def minutes_since_open(now: datetime) -> float:
    open_dt = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return (now - open_dt).total_seconds() / 60


def in_trading_window(now: datetime, start_min: float, end_min: float) -> bool:
    if not is_rth(now):
        return False
    elapsed = minutes_since_open(now)
    return start_min <= elapsed <= end_min


def derive_support_levels(spy_price: float | None) -> list[float]:
    """
    Placeholder: returns round-number levels near the current SPY price.
    Replace with actual GEX walls from a Periscope screenshot or manual input.
    """
    if spy_price is None:
        return []
    base = round(spy_price)
    return [base - 2, base - 1, base, base + 1, base + 2]
