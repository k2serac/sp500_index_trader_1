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

_SYSTEM_PROMPT = """\
You are a quantitative analyst reading UnusualWhales Periscope screenshots to extract
structured market-maker positioning data for an intraday SPX/SPY mean-reversion strategy.

You will receive one or two screenshots:
1. Market Maker Exposure (always present) — shows four columns of bars per price level:
   Gamma, Vanna, Charm, and Positions. A horizontal dotted line marks the gamma flip level.
2. Delta Flow (optional) — shows intraday delta accumulation/distribution.

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
  "charm_bias": "<bullish|bearish|neutral>",    // dominant Charm direction across all levels
  "positions_bias": "<long|short|neutral>",     // dominant net MM Positions across all levels
  "mm_bias": "<bullish|bearish|neutral>",       // overall bias combining all four Greeks + delta flow
  "notes": "<one or two sentences on the most notable features>"
}

Rules:
- Include only levels where the bar is clearly significant (not noise).
- For each distinct, significant bar report exactly ONE price level — the visual centre of
  that bar. Do not report the same bar as multiple values.
  The chart has labeled grid lines at fixed intervals (e.g. every 20 points: 7280, 7300, 7320 …).
  If a bar's centre sits between two grid labels, report the interpolated midpoint
  (e.g. centre halfway between 7320 and 7340 → 7330). Use 10-point precision.
- List all price level arrays in ascending order.
- If a value cannot be determined from the screenshot, use null.
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
    charm_bias: str = "neutral"     # "bullish" | "bearish" | "neutral"
    positions_bias: str = "neutral" # "long" | "short" | "neutral"
    above_gamma_flip: bool | None = None
    mm_bias: str = "neutral"        # "bullish" | "bearish" | "neutral"
    notes: str = ""

    def all_gex_levels(self) -> list[float]:
        """Combined GEX support + resistance levels for SignalEvaluator."""
        return sorted(set(self.gex_support + self.gex_resistance))

    def all_key_levels(self) -> list[float]:
        """All significant levels across Gamma and Vanna for SignalEvaluator."""
        return sorted(set(
            self.gex_support + self.gex_resistance +
            self.vanna_support + self.vanna_resistance
        ))

    def summary(self) -> str:
        flip = f"{self.gamma_flip:.0f}" if self.gamma_flip else "?"
        side = "ABOVE" if self.above_gamma_flip else "BELOW"
        fmt  = lambda levels: ", ".join(f"{v:.0f}" for v in levels) or "none"
        return (
            f"SPX={self.spx_price}  GammaFlip={flip} ({side})  bias={self.mm_bias}\n"
            f"  GEX support    : {fmt(self.gex_support)}\n"
            f"  GEX resistance : {fmt(self.gex_resistance)}\n"
            f"  Vanna support  : {fmt(self.vanna_support)}\n"
            f"  Vanna resist.  : {fmt(self.vanna_resistance)}\n"
            f"  Charm={self.charm_bias}  Positions={self.positions_bias}\n"
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
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )
            raw = response.content[0].text
            return self._parse(raw)
        except Exception as exc:
            logger.error("PeriscopeReader: Claude call failed: %s", exc)
            return None

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

    def _parse(self, raw: str) -> PeriscopeData | None:
        try:
            # Strip markdown code fences if present
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text.strip())
        except json.JSONDecodeError as exc:
            logger.error("PeriscopeReader: could not parse JSON response: %s\n%s", exc, raw)
            return None

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
            charm_bias=data.get("charm_bias") or "neutral",
            positions_bias=data.get("positions_bias") or "neutral",
            above_gamma_flip=data.get("above_gamma_flip"),
            mm_bias=data.get("mm_bias") or "neutral",
            notes=data.get("notes") or "",
        )
