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
from datetime import datetime
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


def capture_periscope_screenshots(snapshot_dir: Path | str) -> dict[str, Path]:
    """Capture a screenshot of each UW tab via Chrome DevTools Protocol.

    Returns a mapping of URL slug → saved file path for each tab captured.
    Logs a warning and skips gracefully if CDP is not reachable or a tab hasn't loaded yet.
    """
    import base64
    import json as _json
    import time as _time
    from urllib.parse import urlparse

    import requests
    import websocket

    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(MARKET_TZ).strftime("%Y%m%d_%H%M")
    results: dict[str, Path] = {}

    try:
        cdp_tabs = requests.get(f"http://localhost:{_CDP_PORT}/json", timeout=5).json()
    except Exception as exc:
        logger.warning("CDP not reachable (Chrome may need --remote-debugging-port): %s", exc)
        return results

    # Build url → websocket debugger url for all open page tabs.
    ws_by_url = {
        t["url"]: t["webSocketDebuggerUrl"]
        for t in cdp_tabs
        if t.get("type") == "page" and "webSocketDebuggerUrl" in t
    }

    for tab_url in _uw_tab_urls:
        # Match by prefix to tolerate query params or trailing slashes added by Chrome.
        ws_url = next((ws for url, ws in ws_by_url.items() if url.startswith(tab_url)), None)
        if ws_url is None:
            logger.warning("UW tab not found in CDP — not yet loaded? (%s)", tab_url)
            continue

        path = urlparse(tab_url).path                               # "/periscope/market-exposure"
        slug = path.strip("/").replace("/", "_").replace("-", "_")  # "periscope_market_exposure"
        out_path = snapshot_dir / f"{timestamp}_{slug}.png"
        ws_url_ip = ws_url.replace("localhost", "127.0.0.1")

        for attempt in range(1, 4):
            try:
                ws = websocket.create_connection(ws_url_ip, timeout=30)
                try:
                    ws.send(_json.dumps({"id": 1, "method": "Page.captureScreenshot",
                                         "params": {"format": "png"}}))
                    while True:
                        msg = _json.loads(ws.recv())
                        if msg.get("id") == 1:
                            if "error" in msg:
                                raise RuntimeError(msg["error"].get("message", "CDP error"))
                            out_path.write_bytes(base64.b64decode(msg["result"]["data"]))
                            logger.info("Periscope screenshot saved: %s", out_path.name)
                            results[slug] = out_path
                            break
                finally:
                    ws.close()
                break  # success — no more retries needed
            except Exception as exc:
                if attempt < 3:
                    logger.warning("Screenshot attempt %d/3 failed for %s: %s — retrying in 5 s.",
                                   attempt, tab_url, exc)
                    _time.sleep(5)
                else:
                    logger.warning("Screenshot failed for %s after 3 attempts: %s", tab_url, exc)

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
