"""
trade_lib.py — Trade execution and risk management for the S&P 500 reversal bot.

TradeManager   — Places, monitors, and closes SPY limit orders via IBKR.
open_uw_browser — Opens UnusualWhales tabs in Firefox at bot startup.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder

logger = logging.getLogger(__name__)

_UW_BOOKMARK_FOLDER = "UnusualWhales"
_CHROME_BOOKMARKS = Path.home() / ".config/google-chrome/Default/Bookmarks"
_CDP_PORT = 9222

_FALLBACK_TABS = [
    "https://unusualwhales.com/flow/overview",
    "https://unusualwhales.com/periscope/market-exposure",
    "https://unusualwhales.com/periscope/delta-flow",
]

# Populated by open_uw_browser() so capture_periscope_screenshots() knows which URLs to target.
_uw_tab_urls: list[str] = []


def _load_uw_bookmark_urls() -> list[str]:
    """Return URLs from the Chrome bookmark folder named UnusualWhales."""
    try:
        data = json.loads(_CHROME_BOOKMARKS.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read Chrome bookmarks: %s", exc)
        return _FALLBACK_TABS

    def find_folder(node: dict, name: str) -> dict | None:
        if node.get("type") == "folder" and node.get("name") == name:
            return node
        for child in node.get("children", []):
            result = find_folder(child, name)
            if result:
                return result
        return None

    for root in data.get("roots", {}).values():
        folder = find_folder(root, _UW_BOOKMARK_FOLDER)
        if folder:
            urls = [c["url"] for c in folder.get("children", []) if c.get("type") == "url"]
            if urls:
                return urls

    logger.warning("Bookmark folder '%s' not found — using fallback URLs.", _UW_BOOKMARK_FOLDER)
    return _FALLBACK_TABS


def open_uw_browser() -> None:
    global _uw_tab_urls
    tabs = _load_uw_bookmark_urls()
    _uw_tab_urls = tabs

    # Kill any leftover Chrome instance using the bot profile before starting fresh.
    subprocess.run(
        ["pkill", "-f", "chrome-sp500bot"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        subprocess.Popen(
            ["google-chrome", "--new-window", f"--remote-debugging-port={_CDP_PORT}",
             "--remote-allow-origins=*",
             "--user-data-dir=" + str(Path.home() / ".config/chrome-sp500bot"),
             "--no-first-run", "--no-default-browser-check",
             "--hide-crash-restore-bubble"] + tabs
        )
        logger.info("Opened UnusualWhales in Chrome (%d tabs, CDP port %d).", len(tabs), _CDP_PORT)
    except FileNotFoundError:
        logger.warning("Chrome not found — open UnusualWhales manually.")
    except Exception as exc:
        logger.warning("Could not open Chrome: %s", exc)


def _cdp_tabs() -> dict[str, str]:
    """Return {page_url: websocket_debugger_url} for all open CDP page tabs."""
    import requests
    try:
        tabs = requests.get(f"http://localhost:{_CDP_PORT}/json", timeout=5).json()
    except Exception as exc:
        logger.warning("CDP not reachable: %s", exc)
        return {}
    return {
        t["url"]: t["webSocketDebuggerUrl"]
        for t in tabs
        if t.get("type") == "page" and "webSocketDebuggerUrl" in t
    }


def _cdp_evaluate(ws_url: str, js: str, timeout: int = 30):
    """Execute JS in a tab via CDP Runtime.evaluate and return the result value."""
    import json as _json
    import websocket

    ws_url = ws_url.replace("localhost", "127.0.0.1")
    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        ws.send(_json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": js, "returnByValue": True},
        }))
        while True:
            msg = _json.loads(ws.recv())
            if msg.get("id") == 1:
                if "exceptionDetails" in msg.get("result", {}):
                    raise RuntimeError(msg["result"]["exceptionDetails"])
                return msg.get("result", {}).get("result", {}).get("value")
    finally:
        ws.close()


def _cdp_ws_for_tab(tab_url: str) -> str | None:
    """Return the CDP WebSocket URL for the first tab whose URL starts with tab_url."""
    tabs = _cdp_tabs()
    ws = next((ws for url, ws in tabs.items() if url.startswith(tab_url)), None)
    if ws is None:
        logger.warning("Tab not found in CDP: %s", tab_url)
    return ws


_PERISCOPE_MARKET_EXPOSURE_URL = "https://unusualwhales.com/periscope/market-exposure"

_JS_GET_PERISCOPE_DATE = """
(function() {
    const span = document.querySelector('[data-testid="market-exposures-tick-chart"] span[role="button"]');
    if (span) return span.innerText.trim();
    const btn = document.querySelector('[data-testid="date-picker-button"]');
    return btn ? btn.innerText.trim() : null;
})()
"""

_JS_GET_CHART_HOUR_LINKS = r"""
(function() {
    const result = [];
    document.querySelectorAll('a').forEach(el => {
        const text = (el.innerText || '').trim();
        if (!/^\d{1,2}:\d{2} [AP]M$/.test(text)) return;
        const r = el.getBoundingClientRect();
        if (r.width === 0) return;
        result.push({text, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)});
    });
    return result;
})()
"""

_JS_GET_ELEM_CENTRE = "(function(s){const e=document.querySelector(s);if(!e)return null;const r=e.getBoundingClientRect();return{x:r.left+r.width/2,y:r.top+r.height/2}})(%s)"


def _parse_periscope_date(text: str) -> date | None:
    """Parse 'Wed, May 20' → date (assumes current year)."""
    try:
        parsed = datetime.strptime(text.strip(), "%a, %b %d")
        return parsed.replace(year=date.today().year).date()
    except ValueError:
        return None


def _hour_to_chart_label(hour: int) -> str:
    """Convert 24h hour to chart x-axis label: 14 → '2:00 PM', 10 → '10:00 AM'."""
    if hour == 0:
        return "12:00 AM"
    elif hour < 12:
        return f"{hour}:00 AM"
    elif hour == 12:
        return "12:00 PM"
    else:
        return f"{hour - 12}:00 PM"


def _hour_minute_to_chart_x(hour: int, minute: int, hour_links: list[dict]) -> float | None:
    """Interpolate the chart x-coordinate for hour:minute using the known hour-label positions.

    hour_links is the list returned by _JS_GET_CHART_HOUR_LINKS.
    Returns None if there are fewer than two anchor points to interpolate from.
    """
    import re

    def _label_to_mins(label: str) -> int:
        m = re.match(r"(\d+):(\d+) ([AP]M)", label)
        if not m:
            return -1
        h, mn, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm == "PM" and h != 12:
            h += 12
        elif ampm == "AM" and h == 12:
            h = 0
        return h * 60 + mn

    points = sorted(
        [(t, lnk["x"]) for lnk in hour_links if (t := _label_to_mins(lnk["text"])) >= 0],
        key=lambda p: p[0],
    )
    if len(points) < 2:
        return None

    target = hour * 60 + minute
    for i in range(len(points) - 1):
        t1, x1 = points[i]
        t2, x2 = points[i + 1]
        if t1 <= target <= t2:
            return x1 + (target - t1) / (t2 - t1) * (x2 - x1)
    # Extrapolate beyond last anchor using the last segment's slope
    t1, x1 = points[-2]
    t2, x2 = points[-1]
    return x1 + (target - t2) / (t2 - t1) * (x2 - x1)


def _cdp_click_xy(ws_url: str, x: float, y: float) -> None:
    """Click at page coordinates via CDP Input.dispatchMouseEvent (fire-and-forget).

    We do NOT wait for Chrome to ACK the dispatches: clicking a chart anchor
    triggers a heavy synchronous React re-render that blocks Chrome's main thread
    (and therefore the CDP response loop) for 30+ seconds. The TCP layer guarantees
    our messages are delivered to Chrome before any close frame, so the events
    are queued and executed even though we close the socket immediately.
    """
    import json as _json
    import websocket as _ws

    ws_url = ws_url.replace("localhost", "127.0.0.1")
    ws = _ws.create_connection(ws_url, timeout=10)
    try:
        for mid, etype in [(1, "mousePressed"), (2, "mouseReleased")]:
            ws.send(_json.dumps({
                "id": mid,
                "method": "Input.dispatchMouseEvent",
                "params": {"type": etype, "x": x, "y": y, "button": "left", "clickCount": 1},
            }))
    finally:
        ws.close()


def _cdp_click(ws_url: str, selector: str) -> bool:
    """Click the centre of a DOM element identified by CSS selector via CDP.

    Returns True if the element was found and clicked.
    """
    import json as _json

    rect = _cdp_evaluate(ws_url, _JS_GET_ELEM_CENTRE % _json.dumps(selector))
    if not rect:
        logger.warning("CDP click: element not found: %s", selector)
        return False
    _cdp_click_xy(ws_url, rect["x"], rect["y"])
    return True


def _cdp_reload(ws_url: str, wait_secs: float = 10.0) -> None:
    """Reload a CDP tab and wait for Page.loadEventFired (or timeout)."""
    import json as _json
    import time as _time
    import websocket as _ws

    ws_url = ws_url.replace("localhost", "127.0.0.1")
    ws = _ws.create_connection(ws_url, timeout=60)
    try:
        ws.send(_json.dumps({"id": 1, "method": "Page.enable"}))
        while True:
            if _json.loads(ws.recv()).get("id") == 1:
                break
        ws.send(_json.dumps({"id": 2, "method": "Page.reload", "params": {}}))
        while True:
            if _json.loads(ws.recv()).get("id") == 2:
                break
        deadline = _time.monotonic() + wait_secs
        while _time.monotonic() < deadline:
            ws.settimeout(max(0.5, deadline - _time.monotonic()))
            try:
                if _json.loads(ws.recv()).get("method") == "Page.loadEventFired":
                    break
            except Exception:
                break
    finally:
        ws.close()


def _cdp_screenshot_tab(ws_url: str, out_path: Path) -> bool:
    """Capture a PNG screenshot of a CDP tab and write it to out_path. Returns True on success."""
    import base64 as _b64
    import json as _json
    import websocket as _ws

    ws_url = ws_url.replace("localhost", "127.0.0.1")
    try:
        ws = _ws.create_connection(ws_url, timeout=30)
        try:
            ws.send(_json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
            while True:
                msg = _json.loads(ws.recv())
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise RuntimeError(msg["error"].get("message", "CDP error"))
                    out_path.write_bytes(_b64.b64decode(msg["result"]["data"]))
                    return True
        finally:
            ws.close()
    except Exception as exc:
        logger.warning("CDP screenshot failed for %s: %s", out_path.name, exc)
        return False


def _chart_label_to_hour(label: str) -> int | None:
    """Parse a chart x-axis label to a 24h hour: '10:00 AM' → 10, '2:00 PM' → 14."""
    import re
    m = re.match(r"(\d+):\d+ ([AP]M)", label)
    if not m:
        return None
    h, ampm = int(m.group(1)), m.group(2)
    if ampm == "PM" and h != 12:
        h += 12
    elif ampm == "AM" and h == 12:
        h = 0
    return h


def select_periscope_datetime(
    target_date: date | None = None,
    target_hour: int | None = None,
) -> bool:
    """Navigate the Periscope market-exposure page to a specific date and/or hour anchor.

    target_date: trading date to display; None = leave as-is.
    target_hour: 24h hour (10-16 ET). Clicks the nearest rendered anchor ≤ target_hour.
                 The chart renders anchor labels every 1-2 hours (10:00 AM, 12:00 PM …).
                 Each anchor shows the 10-minute bucket ending at that hour.
    Returns True if all requested navigation succeeded.
    """
    import time as _time

    ws_url = _cdp_ws_for_tab(_PERISCOPE_MARKET_EXPOSURE_URL)
    if not ws_url:
        return False

    # --- Date navigation ---
    if target_date is not None:
        for _ in range(30):  # safety cap: max 30 single-day clicks
            raw = _cdp_evaluate(ws_url, _JS_GET_PERISCOPE_DATE)
            if not raw:
                logger.warning("select_periscope_datetime: cannot read current date from page")
                return False
            current = _parse_periscope_date(raw)
            if current is None:
                logger.warning("select_periscope_datetime: unparseable date string %r", raw)
                return False
            delta = (target_date - current).days
            if delta == 0:
                break
            selector = ('button[aria-label="Next day"]' if delta > 0
                        else 'button[aria-label="Previous day"]')
            _cdp_click(ws_url, selector)
            _time.sleep(1.2)
        else:
            logger.warning("select_periscope_datetime: could not reach %s after 30 clicks", target_date)
            return False
        logger.info("Periscope date set to %s", target_date)

    # --- Hour anchor selection ---
    if target_hour is not None:
        links = _cdp_evaluate(ws_url, _JS_GET_CHART_HOUR_LINKS)
        if not links:
            logger.warning("select_periscope_datetime: no hour anchors visible in chart")
            return False

        # Build (link, parsed_hour) pairs
        anchors = [(lnk, h) for lnk in links if (h := _chart_label_to_hour(lnk["text"])) is not None]

        # Prefer exact match, else nearest anchor ≤ target_hour
        match = next((lnk for lnk, h in anchors if h == target_hour), None)
        if match is None:
            candidates = [(lnk, h) for lnk, h in anchors if h <= target_hour]
            if not candidates:
                logger.warning("select_periscope_datetime: no anchor ≤ %02d:00; available: %s",
                               target_hour, [h for _, h in anchors])
                return False
            match, snapped = max(candidates, key=lambda t: t[1])
            logger.info("select_periscope_datetime: snapped %02d:00 → %02d:00", target_hour, snapped)

        _cdp_click_xy(ws_url, match["x"], match["y"])
        # React re-renders chart data after anchor click; Runtime.evaluate is blocked
        # during this period but Page.captureScreenshot still works.
        _time.sleep(8.0)
        logger.info("Periscope hour anchor clicked: %s", match["text"])

    return True


def capture_periscope_screenshots(snapshot_dir: Path | str) -> dict[str, Path]:
    """Reload each UW tab, then capture a screenshot via CDP.

    Returns a mapping of URL slug → saved file path for each tab captured.
    Logs a warning and skips gracefully if CDP is not reachable or a tab hasn't loaded yet.
    """
    import time as _time
    from urllib.parse import urlparse

    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(MARKET_TZ).strftime("%Y%m%d_%H%M")
    results: dict[str, Path] = {}

    ws_by_url = _cdp_tabs()
    if not ws_by_url:
        logger.warning("CDP not reachable (Chrome may need --remote-debugging-port).")
        return results

    for tab_url in _uw_tab_urls:
        ws_url = next((ws for url, ws in ws_by_url.items() if url.startswith(tab_url)), None)
        if ws_url is None:
            logger.warning("UW tab not found in CDP — not yet loaded? (%s)", tab_url)
            continue

        path = urlparse(tab_url).path
        slug = path.strip("/").replace("/", "_").replace("-", "_")
        out_path = snapshot_dir / f"{timestamp}_{slug}.png"

        # Reload so the page advances to the latest 10-minute bucket.
        _cdp_reload(ws_url)

        for attempt in range(1, 4):
            if _cdp_screenshot_tab(ws_url, out_path):
                logger.info("Periscope screenshot saved: %s", out_path.name)
                results[slug] = out_path
                break
            if attempt < 3:
                logger.warning("Screenshot attempt %d/3 failed for %s — retrying in 5 s.",
                               attempt, tab_url)
                _time.sleep(5)
            else:
                logger.warning("Screenshot failed for %s after 3 attempts.", tab_url)

    return results


def capture_periscope_historical(
    snapshot_dir: Path | str,
    start_date: date,
    end_date: date,
    start_hour: int = 10,
    end_hour: int = 16,
) -> dict[str, Path]:
    """Capture Periscope screenshots for a historical date range at each available hour anchor.

    Navigates day-by-day from start_date to end_date. On each day, queries the chart for
    rendered hour anchor labels (typically every 1-2 hours: 10 AM, 12 PM, 2 PM, 4 PM) and
    captures a screenshot at each anchor within [start_hour, end_hour]. Weekends are skipped.

    After each anchor click the function waits 8 s for React to re-render the chart before
    taking the screenshot (Runtime.evaluate is blocked during this period; screenshots work).

    Returns {"YYYYMMDD_HHh": Path} for every screenshot captured.
    """
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path] = {}

    current = start_date
    while current <= end_date:
        if current.weekday() >= 5:
            current += timedelta(days=1)
            continue

        if not select_periscope_datetime(target_date=current):
            logger.warning("capture_periscope_historical: could not navigate to %s — skipping.", current)
            current += timedelta(days=1)
            continue

        ws_url = _cdp_ws_for_tab(_PERISCOPE_MARKET_EXPOSURE_URL)
        if not ws_url:
            current += timedelta(days=1)
            continue

        links = _cdp_evaluate(ws_url, _JS_GET_CHART_HOUR_LINKS) or []
        anchors = [(lnk, h) for lnk in links
                   if (h := _chart_label_to_hour(lnk["text"])) is not None
                   and (current > start_date or h >= start_hour)
                   and (current < end_date or h <= end_hour)]

        for lnk, hour in anchors:
            _cdp_click_xy(ws_url, lnk["x"], lnk["y"])
            import time as _time; _time.sleep(8.0)  # wait for React chart re-render

            key = f"{current.strftime('%Y%m%d')}_{hour:02d}h"
            out_path = snapshot_dir / f"{key}_periscope_market_exposure.png"

            ws_url = _cdp_ws_for_tab(_PERISCOPE_MARKET_EXPOSURE_URL)
            if ws_url and _cdp_screenshot_tab(ws_url, out_path):
                logger.info("Historical screenshot: %s", out_path.name)
                results[key] = out_path
            else:
                logger.warning("capture_periscope_historical: screenshot failed for %s %02dh.", current, hour)

        current += timedelta(days=1)

    return results

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
