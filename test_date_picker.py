"""
Test date + hour anchor navigation on UnusualWhales Periscope via CDP.

Usage:
    python test_date_picker.py              # probe anchors + date navigation
    python test_date_picker.py --history    # capture today at each available anchor hour
"""

import sys
import time
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent))

from libs.trade_lib import (
    _cdp_tabs, _cdp_evaluate,
    select_periscope_datetime, capture_periscope_historical,
    _JS_GET_PERISCOPE_DATE, _JS_GET_CHART_HOUR_LINKS,
    _PERISCOPE_MARKET_EXPOSURE_URL, _chart_label_to_hour,
)

JS_GET_TIMEFRAME = r"""
(function() {
    for (const el of document.querySelectorAll('span')) {
        const t = el.innerText?.trim();
        if (t && /^\d{1,2}:\d{2} - \d{1,2}:\d{2}/.test(t)) return t;
    }
    return null;
})()
"""


def get_ws_url():
    tabs = _cdp_tabs()
    return next((ws for url, ws in tabs.items() if url.startswith(_PERISCOPE_MARKET_EXPOSURE_URL)), None)


def safe_eval(js, label=""):
    ws = get_ws_url()
    if not ws:
        return None
    try:
        return _cdp_evaluate(ws, js)
    except Exception as e:
        print(f"  [warn] {label}: {e}")
        return None


def wait_for_cdp(timeout_secs: int = 60) -> str | None:
    import time as _t
    deadline = _t.monotonic() + timeout_secs
    attempt = 0
    while _t.monotonic() < deadline:
        ws = get_ws_url()
        if ws:
            try:
                if _cdp_evaluate(ws, "1+1") == 2:
                    print(f"  CDP ready (attempt {attempt + 1})")
                    return ws
            except Exception:
                pass
        attempt += 1
        print(f"  Waiting for CDP... (attempt {attempt})")
        _t.sleep(3)
    return None


def probe(ws_url: str) -> None:
    today = date.today()
    print(f"\n=== State ===")
    print(f"  Date:      {safe_eval(_JS_GET_PERISCOPE_DATE, 'date')}")
    print(f"  Timeframe: {safe_eval(JS_GET_TIMEFRAME, 'tf')}")

    ws = get_ws_url()
    links = _cdp_evaluate(ws, _JS_GET_CHART_HOUR_LINKS) or []
    print(f"\n  Available hour anchors ({len(links)}):")
    for lnk in links:
        h = _chart_label_to_hour(lnk["text"])
        print(f"    {lnk['text']:12s}  → hour {h:02d}  x={lnk['x']}  y={lnk['y']}")

    print(f"\n=== Date navigation: today ({today}) ===")
    ok = select_periscope_datetime(target_date=today)
    print(f"  ok={ok}  date={safe_eval(_JS_GET_PERISCOPE_DATE, 'date')}")

    print("\n=== Hour anchor clicks ===")
    for hour in [10, 12, 14, 16]:
        print(f"  Clicking hour {hour:02d}:00 ...")
        ok = select_periscope_datetime(target_hour=hour)
        # Runtime.evaluate is blocked ~30s after anchor click; just report ok
        print(f"    ok={ok}")
        # No JS read here — React blocks Runtime.evaluate during re-render.
        # Verify by looking at the screenshots instead.


def test_historical_today() -> None:
    today = date.today()
    out_dir = Path("periscope_snapshots") / f"history_{today.strftime('%Y%m%d')}"

    print(f"\n=== Historical capture: {today} at each anchor hour ===")
    print(f"  Output dir: {out_dir}")

    results = capture_periscope_historical(
        snapshot_dir=out_dir,
        start_date=today,
        end_date=today,
        start_hour=10,
        end_hour=16,
    )

    print(f"\n  Captured {len(results)} screenshots:")
    for key, path in sorted(results.items()):
        print(f"    {key}  →  {path.name}")


def main():
    print("Waiting for Periscope tab to be CDP-ready...")
    ws_url = wait_for_cdp(timeout_secs=60)
    if not ws_url:
        print("CDP not ready after 60s — is Chrome open with --remote-debugging-port?")
        return

    if "--history" in sys.argv:
        test_historical_today()
    else:
        probe(ws_url)


if __name__ == "__main__":
    main()
