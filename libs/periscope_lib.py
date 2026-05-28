"""
periscope_lib.py — Claude Vision reader for UnusualWhales Periscope screenshots.

PeriscopeData   — Structured output extracted from a Periscope screenshot set.
PeriscopeReader — Sends screenshots to Claude Vision and parses the response.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

logger = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("America/New_York")


class PeriscopeParseError(Exception):
    """Claude returned a non-empty response that could not be parsed as JSON.

    Indicates a prompt or model issue — distinct from an empty response
    (chart has no GEX data) which is a legitimate no-data condition.
    """

_SYSTEM_PROMPT = """\
You are a quantitative analyst reading UnusualWhales Periscope screenshots to extract
structured market-maker positioning data for an intraday SPX/SPY mean-reversion strategy.

You will receive one or two screenshots:
1. Market Maker Exposure (always present) — shows four columns of bars per price level:
   Gamma, Vanna, Charm, and Positions. A horizontal dotted line marks the gamma flip level.
2. Delta Flow (optional) — three-panel chart: SPX price (top), net delta bars per time period
   (middle, green = buying / red = selling), net delta by contract/expiry (bottom stacked bars).

The four columns in Market Maker Exposure:
- Gamma: GEX at each price level. Green = positive (stabilising), red = negative (amplifying).
- Vanna: Sensitivity of delta to IV changes. Positive Vanna = MMs buy when IV drops (bullish on rallies).
- Charm: Delta decay over time. Positive Charm = delta bleeds bullish intraday.
- Positions: Net MM open interest at each level. Green = net long, red = net short.

Extract the following and return ONLY valid JSON, no markdown, no commentary:

{
  "spx_price": <float>,
  "gamma_flip": <float — price of the horizontal dotted/dashed line>,
  "above_gamma_flip": <bool>,
  "gex_support": [<float>, ...],     // significant GREEN Gamma bars BELOW current price
  "gex_resistance": [<float>, ...],  // significant RED Gamma bars ABOVE current price
  "vanna_support": [<float>, ...],   // significant POSITIVE Vanna levels BELOW current price
  "vanna_resistance": [<float>, ...],// significant NEGATIVE Vanna levels ABOVE current price
  "charm_support": [<float>, ...],    // significant GREEN Charm bars BELOW current price (positive Charm = bullish delta bleed)
  "charm_resistance": [<float>, ...], // significant RED Charm bars ABOVE current price (negative Charm = bearish delta bleed)
  "charm_bias": "<bullish|bearish|neutral>",    // dominant Charm direction across all levels
  "positions_support": [<float>, ...],    // significant GREEN Positions bars BELOW current price (net long MM exposure)
  "positions_resistance": [<float>, ...], // significant RED Positions bars ABOVE current price (net short MM exposure)
  "positions_bias": "<long|short|neutral>",     // dominant net MM Positions across all levels
  "mm_bias": "<bullish|bearish|neutral>",       // overall bias combining all four Greeks + delta flow
  "notes": "<one or two sentences on the most notable features>",
  "delta_trend": "<accumulating|distributing|neutral|null>",
  "delta_sign": "<positive|negative|mixed|null>",
  "delta_turning": <bool|null>,
  "delta_exhaustion": <bool|null>,
  "delta_notes": "<one sentence on delta flow — null if Delta Flow screenshot not provided>"
}

Rules:
- Include only levels where the bar is clearly significant (not noise).
- For each distinct, significant bar report exactly ONE price level — the visual centre of
  that bar. Do not report the same bar as multiple values.
  The chart has labeled grid lines at fixed intervals (e.g. every 20 points: 7280, 7300, 7320 …).
  If a bar's centre sits between two grid labels, report the interpolated midpoint
  (e.g. centre halfway between 7320 and 7340 → 7330). Use 10-point precision.
- List all price level arrays in ascending order.
- Populate delta_* fields only if the Delta Flow screenshot is provided; otherwise set all to null.
- delta_trend: direction of net delta bars over the most recent 30-60 minutes.
  "accumulating" = bars are green or turning green. "distributing" = bars are red or turning red.
- delta_sign: whether the most recent net delta bar is positive (green) or negative (red).
  "mixed" if alternating rapidly with no clear direction.
- delta_turning: true if there is a visible inflection in the middle panel — e.g. a run of red bars
  followed by a green bar at the right edge, signalling selling exhaustion.
- delta_exhaustion: true if the red (selling) bars are visibly shrinking in magnitude approaching
  the right edge, even if still negative — smaller bars = selling pressure fading.
- If a value cannot be determined from the screenshot, use null.
"""

_TIDE_SYSTEM_PROMPT = """\
You are a quantitative analyst reading an UnusualWhales Market Tide (flow/overview) screenshot
to extract structured options flow data for an intraday SPX/SPY mean-reversion strategy.

The screenshot shows the Market Tide page with:
- A chart containing a SPX price line and a Market Tide line (net cumulative options flow)
- A net volume area (green = net buying/positive flow, red = net selling/negative flow)
- Possibly a momentum oscillator below the main chart

Your goal: assess whether the broad options flow supports a mean-reversion long entry.

Return ONLY valid JSON, no markdown, no commentary:

{
  "tide_direction": "<rising|falling|flat>",
  "tide_sign": "<positive|negative>",
  "tide_turning": <bool — true if the tide line is visibly inflecting/reversing right now>,
  "spx_tide_divergence": <bool — true if SPX is moving opposite to the tide line>,
  "tide_notes": "<one or two sentences on the most notable features>"
}

Definitions:
- tide_direction: the slope of the Market Tide line over the most recent 30-60 minutes.
  "rising" = tide line trending up. "falling" = trending down. "flat" = roughly horizontal.
- tide_sign: whether the net volume area is currently above zero (positive) or below zero (negative).
- tide_turning: true if the tide line has a clear inflection point at the current right edge —
  i.e. it was falling and is now curling up, or vice versa.
- spx_tide_divergence: true if SPX price is rising while tide is falling, or vice versa.
  Divergence at a support level (SPX holds but tide keeps falling) = bearish.
  Divergence where SPX falls but tide is already curling up = bullish for mean-reversion long.
- If a value cannot be determined, use null.
"""


@dataclass
class PeriscopeData:
    timestamp: datetime
    spx_price: float | None
    gamma_flip: float | None
    gex_support: list[float] = field(default_factory=list)
    gex_resistance: list[float] = field(default_factory=list)
    vanna_support: list[float] = field(default_factory=list)
    vanna_resistance: list[float] = field(default_factory=list)
    charm_support: list[float] = field(default_factory=list)
    charm_resistance: list[float] = field(default_factory=list)
    charm_bias: str = "neutral"       # "bullish" | "bearish" | "neutral"
    positions_support: list[float] = field(default_factory=list)
    positions_resistance: list[float] = field(default_factory=list)
    positions_bias: str = "neutral"   # "long" | "short" | "neutral"
    above_gamma_flip: bool | None = None
    mm_bias: str = "neutral"          # "bullish" | "bearish" | "neutral"
    notes: str = ""
    # Delta Flow fields (populated when periscope_delta_flow screenshot is available)
    delta_trend: str = "unknown"       # "accumulating" | "distributing" | "neutral" | "unknown"
    delta_sign: str = "unknown"        # "positive" | "negative" | "mixed" | "unknown"
    delta_turning: bool | None = None  # True if delta visibly inflecting at the right edge
    delta_exhaustion: bool | None = None  # True if selling bars shrinking in magnitude
    delta_notes: str = ""
    # Market Tide fields (populated when flow_overview screenshot is available)
    tide_direction: str = "unknown"         # "rising" | "falling" | "flat" | "unknown"
    tide_sign: str = "unknown"              # "positive" | "negative" | "unknown"
    tide_turning: bool | None = None        # True if tide is visibly inflecting right now
    spx_tide_divergence: bool | None = None # True if SPX and tide moving in opposite directions
    tide_notes: str = ""

    # Periscope shows SPX-denominated levels (~5400 range); SPY trades at ~1/10th scale.
    SPX_TO_SPY = 10.0

    def all_gex_levels(self) -> list[float]:
        """Combined GEX support + resistance levels in SPX points."""
        return sorted(set(self.gex_support + self.gex_resistance))

    def all_key_levels(self) -> list[float]:
        """All significant levels across all four Greeks in SPX points."""
        return sorted(set(
            self.gex_support + self.gex_resistance +
            self.vanna_support + self.vanna_resistance +
            self.charm_support + self.charm_resistance +
            self.positions_support + self.positions_resistance
        ))

    def all_key_levels_spy(self) -> list[float]:
        """All key levels converted to SPY price scale (SPX ÷ 10) for SignalEvaluator."""
        return [round(l / self.SPX_TO_SPY, 2) for l in self.all_key_levels()]

    def to_spy(self, spx_level: float) -> float:
        """Convert a single SPX level to SPY scale."""
        return round(spx_level / self.SPX_TO_SPY, 2)

    def summary(self) -> str:
        flip = f"{self.gamma_flip:.0f}" if self.gamma_flip else "?"
        side = "ABOVE" if self.above_gamma_flip else "BELOW"
        fmt  = lambda levels: ", ".join(f"{v:.0f}" for v in levels) or "none"
        delta_line = ""
        if self.delta_trend != "unknown":
            turning = " TURNING" if self.delta_turning else ""
            exhaust = " [exhaustion]" if self.delta_exhaustion else ""
            delta_line = (
                f"\n  Delta: {self.delta_trend} / {self.delta_sign}{turning}{exhaust}"
                + (f"  — {self.delta_notes}" if self.delta_notes else "")
            )
        tide_line = ""
        if self.tide_direction != "unknown":
            turning = " TURNING" if self.tide_turning else ""
            div = " [SPX/Tide divergence]" if self.spx_tide_divergence else ""
            tide_line = (
                f"\n  Tide: {self.tide_direction} / {self.tide_sign}{turning}{div}"
                + (f"  — {self.tide_notes}" if self.tide_notes else "")
            )
        return (
            f"SPX={self.spx_price}  GammaFlip={flip} ({side})  bias={self.mm_bias}\n"
            f"  GEX support    : {fmt(self.gex_support)}\n"
            f"  GEX resistance : {fmt(self.gex_resistance)}\n"
            f"  Vanna support  : {fmt(self.vanna_support)}\n"
            f"  Vanna resist.  : {fmt(self.vanna_resistance)}\n"
            f"  Charm support  : {fmt(self.charm_support)}\n"
            f"  Charm resist.  : {fmt(self.charm_resistance)}\n"
            f"  Pos. support   : {fmt(self.positions_support)}\n"
            f"  Pos. resist.   : {fmt(self.positions_resistance)}\n"
            f"  Charm={self.charm_bias}  Positions={self.positions_bias}"
            f"{delta_line}"
            f"{tide_line}\n"
            f"  Notes: {self.notes}"
        )


class PeriscopeReader:
    """Reads Periscope screenshots via Claude Vision and returns structured GEX data."""

    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        self._client = anthropic.Anthropic()
        self._model = model

    def read(self, screenshots: dict[str, Path]) -> PeriscopeData | None:
        """Extract GEX data from a set of Periscope screenshots.

        Args:
            screenshots: Mapping of slug → path as returned by capture_periscope_screenshots().
                         Must contain at least "periscope_market_exposure".

        Returns:
            PeriscopeData on success, None if the required screenshot is missing or Claude fails.
        """
        exposure_path = screenshots.get("periscope_market_exposure")
        if exposure_path is None:
            logger.warning("PeriscopeReader: market_exposure screenshot not found — skipping.")
            return None

        content: list = []

        content.append({"type": "text", "text": "Market Maker Exposure screenshot:"})
        img = self._encode_image(exposure_path)
        if img is None:
            return None
        content.append(img)

        delta_path = screenshots.get("periscope_delta_flow")
        if delta_path is not None:
            delta_img = self._encode_image(delta_path)
            if delta_img is not None:
                content.append({"type": "text", "text": "Delta Flow screenshot:"})
                content.append(delta_img)

        content.append({"type": "text", "text": "Extract the structured data as specified."})

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                temperature=0,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )
            raw = response.content[0].text
            data = self._parse(raw)
        except PeriscopeParseError:
            raise  # propagate — caller decides whether to abort or skip
        except Exception as exc:
            logger.error("PeriscopeReader: Claude call failed: %s", exc)
            return None

        if data is None:
            return None

        tide_path = screenshots.get("flow_overview")
        if tide_path is not None:
            self._enrich_tide(data, tide_path)

        return data

    def _encode_image(self, path: Path) -> dict | None:
        try:
            data = base64.standard_b64encode(path.read_bytes()).decode()
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": data},
            }
        except Exception as exc:
            logger.warning("PeriscopeReader: could not read %s: %s", path, exc)
            return None

    def _enrich_tide(self, data: PeriscopeData, path: Path) -> None:
        """Call Claude Vision on the Market Tide screenshot and merge results into data."""
        img = self._encode_image(path)
        if img is None:
            return
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=256,
                temperature=0,
                system=_TIDE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "Market Tide screenshot:"},
                    img,
                    {"type": "text", "text": "Extract the structured data as specified."},
                ]}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()
            brace_open  = raw.find("{")
            brace_close = raw.rfind("}")
            if brace_open >= 0 and brace_close >= 0:
                raw = raw[brace_open:brace_close + 1]
            tide = json.loads(raw)
        except Exception as exc:
            logger.warning("PeriscopeReader: Market Tide Claude call failed: %s", exc)
            return

        data.tide_direction      = tide.get("tide_direction") or "unknown"
        data.tide_sign           = tide.get("tide_sign") or "unknown"
        data.tide_turning        = tide.get("tide_turning")
        data.spx_tide_divergence = tide.get("spx_tide_divergence")
        data.tide_notes          = tide.get("tide_notes") or ""
        logger.info("Market Tide: %s / %s  turning=%s  divergence=%s",
                    data.tide_direction, data.tide_sign,
                    data.tide_turning, data.spx_tide_divergence)

    def _parse(self, raw: str) -> PeriscopeData | None:
        text = raw.strip()
        if not text:
            logger.warning("PeriscopeReader: Claude returned empty response — chart likely has no GEX data")
            return None
        try:
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            text = text.strip()
            # If Claude prefixed/suffixed with prose, extract just the JSON object
            brace_open  = text.find("{")
            brace_close = text.rfind("}")
            if brace_open > 0 or (brace_close >= 0 and brace_close < len(text) - 1):
                logger.warning(
                    "PeriscopeReader: JSON wrapped in prose (preamble=%d trailing=%d) — extracting",
                    brace_open, len(text) - brace_close - 1,
                )
                text = text[brace_open:brace_close + 1] if brace_open >= 0 and brace_close >= 0 else text
            if not text or not text.startswith("{"):
                logger.warning(
                    "PeriscopeReader: no JSON object in response — chart likely has no GEX data. Raw: %.200s",
                    raw,
                )
                return None
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.error("PeriscopeReader: could not parse JSON response: %s\nRaw: %s", exc, raw[:500])
            raise PeriscopeParseError(str(exc)) from exc

        def floats(key: str) -> list[float]:
            return [float(v) for v in data.get(key) or []]

        return PeriscopeData(
            timestamp=datetime.now(MARKET_TZ),
            spx_price=data.get("spx_price"),
            gamma_flip=data.get("gamma_flip"),
            gex_support=floats("gex_support"),
            gex_resistance=floats("gex_resistance"),
            vanna_support=floats("vanna_support"),
            vanna_resistance=floats("vanna_resistance"),
            charm_support=floats("charm_support"),
            charm_resistance=floats("charm_resistance"),
            charm_bias=data.get("charm_bias") or "neutral",
            positions_support=floats("positions_support"),
            positions_resistance=floats("positions_resistance"),
            positions_bias=data.get("positions_bias") or "neutral",
            above_gamma_flip=data.get("above_gamma_flip"),
            mm_bias=data.get("mm_bias") or "neutral",
            notes=data.get("notes") or "",
            delta_trend=data.get("delta_trend") or "unknown",
            delta_sign=data.get("delta_sign") or "unknown",
            delta_turning=data.get("delta_turning"),
            delta_exhaustion=data.get("delta_exhaustion"),
            delta_notes=data.get("delta_notes") or "",
        )
