"""
signal_lib.py — Intraday reversal signal detection via IBKR Market Data.

MarketDataFeed  — Fetches real-time SPY bars, NYSE TICK, and VIX from IBKR.
SignalEvaluator — Evaluates the five reversal conditions and returns a ReversalSignal.
ClaudeAnalyst   — Sends signal summaries (and optional Periscope screenshots) to Claude.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from ib_async import IB, Index, Stock, util

logger = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MarketSnapshot:
    timestamp: datetime
    spy_price: float | None
    spy_bars: list[Bar]     # last N 5-min bars
    tick: float | None      # current NYSE TICK
    vix: float | None       # current VIX
    vix_bars: list[float]   # last N VIX closes


@dataclass
class ReversalSignal:
    timestamp: datetime
    snapshot: MarketSnapshot
    tick_flush: bool         # TICK hit below threshold
    tick_snap: bool          # TICK recovered above snap threshold
    vix_divergence: bool     # VIX not making new high while price retests lows
    volume_exhaustion: bool  # selling volume declining
    candle_structure: bool   # 5-min candle closed above low bar's high
    support_level: float | None
    conditions_met: int = field(init=False)

    def __post_init__(self):
        self.conditions_met = sum([
            self.tick_flush and self.tick_snap,
            self.vix_divergence,
            self.volume_exhaustion,
            self.candle_structure,
            self.support_level is not None,
        ])

    def summary(self) -> str:
        spy = self.snapshot.spy_price
        return (
            f"SPY={spy:.2f}  TICK={self.snapshot.tick}  VIX={self.snapshot.vix:.2f}\n"
            f"Conditions met: {self.conditions_met}/5\n"
            f"  TICK flush+snap : {'✓' if self.tick_flush and self.tick_snap else '✗'}\n"
            f"  VIX divergence  : {'✓' if self.vix_divergence else '✗'}\n"
            f"  Volume exhaustion: {'✓' if self.volume_exhaustion else '✗'}\n"
            f"  Candle structure : {'✓' if self.candle_structure else '✗'}\n"
            f"  Price at support : {'✓ (' + str(self.support_level) + ')' if self.support_level else '✗'}\n"
        )


@dataclass
class ClaudeVerdict:
    go: bool
    confidence: int
    support_level: float | None
    stop_level: float | None
    target_level: float | None
    reasoning: str
    raw_response: str


# ---------------------------------------------------------------------------
# MarketDataFeed
# ---------------------------------------------------------------------------

class MarketDataFeed:
    """Fetches real-time market data for SPY, NYSE TICK, and VIX via IBKR."""

    # IBKR contract definitions
    _SPY_CONTRACT   = Stock("SPY", "SMART", "USD")
    _TICK_CONTRACT  = Index("$TICK", "NYSE")
    _VIX_CONTRACT   = Index("VIX", "CBOE")

    def __init__(self, ib: IB, candle_timeframe: int = 5, bar_lookback: int = 6) -> None:
        self._ib = ib
        self._candle_tf = candle_timeframe
        self._bar_lookback = bar_lookback

    def snapshot(self) -> MarketSnapshot:
        now = datetime.now(MARKET_TZ)
        return MarketSnapshot(
            timestamp=now,
            spy_price=self._get_price(self._SPY_CONTRACT, "SPY"),
            spy_bars=self._get_bars(self._SPY_CONTRACT, "SPY"),
            tick=self._get_price(self._TICK_CONTRACT, "$TICK"),
            vix=self._get_price(self._VIX_CONTRACT, "VIX"),
            vix_bars=self._get_vix_bar_closes(),
        )

    def _get_price(self, contract, label: str) -> float | None:
        try:
            self._ib.qualifyContracts(contract)
            [ticker] = self._ib.reqTickers(contract)
            price = ticker.marketPrice()
            if price is None or math.isnan(price):
                logger.warning("No price for %s.", label)
                return None
            return float(price)
        except Exception as exc:
            logger.error("Error fetching %s price: %s", label, exc)
            return None

    def _get_bars(self, contract, label: str) -> list[Bar]:
        try:
            self._ib.qualifyContracts(contract)
            bars = self._ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=f"{self._bar_lookback * self._candle_tf + 5} S".replace("S", "mins"),
                barSizeSetting=f"{self._candle_tf} mins",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
            return [
                Bar(
                    time=b.date,
                    open=b.open,
                    high=b.high,
                    low=b.low,
                    close=b.close,
                    volume=b.volume,
                )
                for b in bars[-self._bar_lookback:]
            ]
        except Exception as exc:
            logger.error("Error fetching %s bars: %s", label, exc)
            return []

    def _get_vix_bar_closes(self) -> list[float]:
        bars = self._get_bars(self._VIX_CONTRACT, "VIX")
        return [b.close for b in bars]


# ---------------------------------------------------------------------------
# SignalEvaluator
# ---------------------------------------------------------------------------

class SignalEvaluator:
    """Evaluates intraday reversal conditions from a MarketSnapshot."""

    def __init__(
        self,
        tick_low: float = -800,
        tick_snap: float = 200,
        vix_divergence_bars: int = 3,
        volume_exhaustion_bars: int = 3,
        support_buffer_pct: float = 0.30,
        support_levels: list[float] | None = None,
    ) -> None:
        self._tick_low = tick_low
        self._tick_snap = tick_snap
        self._vix_div_bars = vix_divergence_bars
        self._vol_bars = volume_exhaustion_bars
        self._support_buffer_pct = support_buffer_pct
        self._support_levels: list[float] = support_levels or []

        # Rolling TICK history to detect flush+snap across consecutive polls
        self._tick_history: list[float] = []

    def update_support_levels(self, levels: list[float]) -> None:
        self._support_levels = levels
        logger.info("Support levels updated: %s", levels)

    def evaluate(self, snap: MarketSnapshot) -> ReversalSignal | None:
        if snap.spy_price is None or snap.tick is None or snap.vix is None:
            logger.warning("Incomplete snapshot — skipping evaluation.")
            return None

        # Update rolling TICK history
        self._tick_history.append(snap.tick)
        if len(self._tick_history) > 20:
            self._tick_history.pop(0)

        tick_flush = any(t <= self._tick_low for t in self._tick_history[-5:])
        tick_snap  = tick_flush and snap.tick >= self._tick_snap

        vix_div    = self._check_vix_divergence(snap)
        vol_exhaust = self._check_volume_exhaustion(snap.spy_bars)
        candle_ok  = self._check_candle_structure(snap.spy_bars)
        support    = self._nearest_support(snap.spy_price)

        signal = ReversalSignal(
            timestamp=snap.timestamp,
            snapshot=snap,
            tick_flush=tick_flush,
            tick_snap=tick_snap,
            vix_divergence=vix_div,
            volume_exhaustion=vol_exhaust,
            candle_structure=candle_ok,
            support_level=support,
        )
        return signal

    def _check_vix_divergence(self, snap: MarketSnapshot) -> bool:
        if len(snap.vix_bars) < self._vix_div_bars:
            return False
        recent = snap.vix_bars[-self._vix_div_bars:]
        # VIX must not be making a new high in recent bars
        return snap.vix < max(recent)

    def _check_volume_exhaustion(self, bars: list[Bar]) -> bool:
        if len(bars) < self._vol_bars:
            return False
        vols = [b.volume for b in bars[-self._vol_bars:]]
        # Down bars only; volume should be declining
        down_bars = [b for b in bars[-self._vol_bars:] if b.close < b.open]
        if len(down_bars) < 2:
            return False
        vols = [b.volume for b in down_bars]
        return vols[-1] < vols[0]

    def _check_candle_structure(self, bars: list[Bar]) -> bool:
        if len(bars) < 2:
            return False
        low_bar = min(bars[:-1], key=lambda b: b.low)
        last_bar = bars[-1]
        return last_bar.close > low_bar.high

    def _nearest_support(self, price: float) -> float | None:
        buffer = price * self._support_buffer_pct / 100
        candidates = [s for s in self._support_levels if abs(s - price) <= buffer]
        if not candidates:
            return None
        return min(candidates, key=lambda s: abs(s - price))


# ---------------------------------------------------------------------------
# ClaudeAnalyst
# ---------------------------------------------------------------------------

class ClaudeAnalyst:
    """Sends reversal signals to Claude for a go/no-go verdict."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        prompts_dir: str = "config/prompts",
        context_dir: str = "config/context",
    ) -> None:
        self._client = anthropic.Anthropic()
        self._model = model
        self._system_prompt = self._load_text(prompts_dir, "reversal.txt")
        self._context = self._load_text(context_dir, "reversal.txt")

    @staticmethod
    def _load_text(directory: str, filename: str) -> str:
        path = Path(directory) / filename
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        logger.warning("File not found: %s", path)
        return ""

    def analyze(
        self,
        signal: ReversalSignal,
        periscope_data=None,          # PeriscopeData | None
        periscope_screenshot_path: str | None = None,
    ) -> ClaudeVerdict:
        user_content: list = []

        context_block = f"<context>\n{self._context}\n</context>\n\n" if self._context else ""

        periscope_block = ""
        if periscope_data is not None:
            periscope_block = (
                f"<periscope>\n{periscope_data.summary()}\n</periscope>\n\n"
            )

        text_block = (
            f"{context_block}"
            f"{periscope_block}"
            f"Signal detected at {signal.timestamp.strftime('%H:%M ET')}:\n\n"
            f"{signal.summary()}"
        )
        user_content.append({"type": "text", "text": text_block})

        if periscope_screenshot_path:
            try:
                with open(periscope_screenshot_path, "rb") as f:
                    img_data = base64.standard_b64encode(f.read()).decode("utf-8")
                ext = Path(periscope_screenshot_path).suffix.lower().lstrip(".")
                media_type = f"image/{ext}" if ext in ("png", "jpg", "jpeg", "gif", "webp") else "image/png"
                user_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": img_data},
                })
                user_content.append({"type": "text", "text": "Periscope Market Maker Exposure screenshot attached above."})
            except Exception as exc:
                logger.warning("Could not load screenshot %s: %s", periscope_screenshot_path, exc)

        response = self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=self._system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text
        return self._parse_verdict(raw)

    @staticmethod
    def _parse_verdict(text: str) -> ClaudeVerdict:
        def extract(key: str) -> str:
            m = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
            return m.group(1).strip() if m else ""

        def to_float(s: str) -> float | None:
            try:
                return float(s.replace(",", ""))
            except (ValueError, AttributeError):
                return None

        go_str = extract("GO").lower()
        return ClaudeVerdict(
            go=go_str == "true",
            confidence=int(extract("CONFIDENCE") or 0),
            support_level=to_float(extract("SUPPORT_LEVEL")),
            stop_level=to_float(extract("STOP_LEVEL")),
            target_level=to_float(extract("TARGET_LEVEL")),
            reasoning=extract("REASONING"),
            raw_response=text,
        )
