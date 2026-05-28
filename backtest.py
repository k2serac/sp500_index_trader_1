"""
backtest.py — Live-capture Periscope data + IBKR bars and replay signal logic.

For each trading day in the requested date range:
  1. Navigate the UW browser to that date/hour and capture Periscope screenshots.
  2. Run PeriscopeReader on those screenshots → all-Greek key levels.
  3. Pull SPY, VIX (5-min) and TICK (1-min) bars from IBKR for that date.
  4. Walk bars through SignalEvaluator; on ≥3/5 conditions simulate entry.
  5. Exit at: nearest key-level resistance above entry (multi-Greek confluence preferred),
     0.50% fallback, or 15:55 time-stop.
  6. Print per-trade results and save JSON.

Requires: Chrome with UW Periscope open (--remote-debugging-port=9222) + IBKR TWS/Gateway.

Usage:
    python backtest.py --mode demo
    python backtest.py --mode demo --start-date 2026-05-20 --end-date 2026-05-23
    python backtest.py --mode demo --start-date 2026-05-22 --start-hour 10 --end-hour 13
    python backtest.py --mode demo --no-browser          # use pre-saved history_YYYYMMDD/ dirs
    python backtest.py --mode demo --log-level DEBUG     # full bar-by-bar trace
    python backtest.py --mode demo --verbose             # alias for --log-level DEBUG
"""

from __future__ import annotations

import argparse
import logging
import time
import tomllib
from collections import defaultdict
from datetime import date, datetime, timedelta, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from ib_async import IB, Index, Stock

from libs import (
    PeriscopeReader, SignalEvaluator,
    open_uw_browser, capture_periscope_for_backtest,
)
from libs.signal_lib import Bar, MarketSnapshot

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_FILE = Path("config/config.toml")
with open(_CONFIG_FILE, "rb") as _f:
    _cfg = tomllib.load(_f)

IBKR_HOST      = _cfg["ibkr"]["host"]
IBKR_PORT_DEMO = _cfg["ibkr"]["port_demo"]
IBKR_PORT_LIVE = _cfg["ibkr"]["port_live"]
IBKR_CLIENT_ID = _cfg["ibkr"]["client_id"]
PERISCOPE_DIR  = Path(_cfg["periscope"]["snapshot_dir"])
CLAUDE_MODEL   = _cfg["claude"]["model"]

TICK_LOW           = _cfg["signals"]["tick_low_threshold"]
TICK_SNAP_THR      = _cfg["signals"]["tick_snap_threshold"]
VIX_DIV_BARS       = _cfg["signals"]["vix_divergence_bars"]
VOL_BARS           = _cfg["signals"]["volume_exhaustion_bars"]
SUPPORT_BUFFER_PCT = _cfg["signals"]["support_buffer_pct"]

MARKET_TZ = ZoneInfo("America/New_York")

FALLBACK_TARGET_PCT  = 0.50   # % above entry when no key level is nearby
FALLBACK_STOP_PCT    = 0.20   # % below entry when no key level is below
MIN_CONDITIONS       = 3
DEFAULT_PERISCOPE_HOUR = 9    # capture GEX snapshot at/before open (9 AM ET)
DEFAULT_START_HOUR   = 10     # skip first 30 min of open chop (matches TRADING_START_MIN)
DEFAULT_END_HOUR     = 15     # simulation stops at 15:55
PRECLOSE_TIME        = dt_time(15, 55)
GREEK_TOLERANCE      = 5.0   # pts — levels within this are treated as the same zone


# ---------------------------------------------------------------------------
# IBKR contracts
# ---------------------------------------------------------------------------

_SPY_CONTRACT  = Stock("SPY", "SMART", "USD")
_TICK_CONTRACT = Index("$TICK", "NYSE")
_VIX_CONTRACT  = Index("VIX", "CBOE")


# ---------------------------------------------------------------------------
# Level helpers
# ---------------------------------------------------------------------------

def _nearest_above(price: float, levels: list[float]) -> float | None:
    candidates = [l for l in levels if l > price]
    return min(candidates) if candidates else None


def _nearest_below(price: float, levels: list[float]) -> float | None:
    candidates = [l for l in levels if l < price]
    return max(candidates) if candidates else None


def _confluence(level: float, resistance_lists: list[list[float]]) -> int:
    """Count how many Greek categories have a resistance level near this price."""
    return sum(
        1 for lst in resistance_lists
        if any(abs(x - level) <= GREEK_TOLERANCE for x in lst)
    )


def _level_sources(level: float, named_lists: list[tuple[str, list[float]]]) -> list[str]:
    """Return names of Greek categories that have a level within tolerance."""
    return [name for name, lst in named_lists if any(abs(x - level) <= GREEK_TOLERANCE for x in lst)]


def _spy_levels(spx_list: list[float], pdata) -> list[float]:
    """Convert a list of SPX levels to SPY scale."""
    return [pdata.to_spy(l) for l in spx_list]


def pick_target(entry: float, pdata) -> tuple[float, int, list[str], str]:
    """Nearest resistance level above entry (SPY scale); fallback to fixed %.
    Returns (price, confluence_count, greek_sources, method).
    """
    named = [
        ("gex",       _spy_levels(pdata.gex_resistance,       pdata)),
        ("vanna",     _spy_levels(pdata.vanna_resistance,     pdata)),
        ("charm",     _spy_levels(pdata.charm_resistance,     pdata)),
        ("positions", _spy_levels(pdata.positions_resistance, pdata)),
    ]
    all_res = [l for _, lst in named for l in lst]
    level = _nearest_above(entry, all_res)
    if level is None:
        fallback = round(entry * (1 + FALLBACK_TARGET_PCT / 100), 2)
        return fallback, 0, [], f"fallback_{FALLBACK_TARGET_PCT}pct"
    sources = _level_sources(level, named)
    return level, len(sources), sources, "key_level"


def pick_stop(entry: float, pdata) -> tuple[float, list[str], str]:
    """Nearest support level below entry (SPY scale); fallback to fixed %.
    Returns (price, greek_sources, method).
    """
    named = [
        ("gex",       _spy_levels(pdata.gex_support,       pdata)),
        ("vanna",     _spy_levels(pdata.vanna_support,     pdata)),
        ("charm",     _spy_levels(pdata.charm_support,     pdata)),
        ("positions", _spy_levels(pdata.positions_support, pdata)),
    ]
    all_sup = [l for _, lst in named for l in lst]
    level = _nearest_below(entry, all_sup)
    if level is None:
        fallback = round(entry * (1 - FALLBACK_STOP_PCT / 100), 2)
        return fallback, [], f"fallback_{FALLBACK_STOP_PCT}pct"
    sources = _level_sources(level, named)
    return level, sources, "key_level"


# ---------------------------------------------------------------------------
# IBKR data fetching
# ---------------------------------------------------------------------------

def _fetch_bars(ib: IB, contract, date_str: str, bar_size: str, what: str) -> list:
    """Fetch RTH bars for a specific trading date (date_str = YYYYMMDD)."""
    end_dt = f"{date_str} 16:00:00"
    try:
        ib.qualifyContracts(contract)
        return ib.reqHistoricalData(
            contract,
            endDateTime=end_dt,
            durationStr="1 D",
            barSizeSetting=bar_size,
            whatToShow=what,
            useRTH=True,
            formatDate=1,
        )
    except Exception as exc:
        logger.warning("Could not fetch %s %s bars for %s: %s", what, bar_size, date_str, exc)
        return []


def _to_bar(b) -> Bar:
    dt = b.date
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MARKET_TZ)
    return Bar(time=dt, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)


def _bar_time(b: Bar) -> datetime:
    dt = b.time
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MARKET_TZ)
    return dt


# ---------------------------------------------------------------------------
# Single-day simulation
# ---------------------------------------------------------------------------

def simulate_day(
    date_str: str,
    pdata,
    spy_raw: list,
    vix_raw: list,
    tick_raw: list,
    start_hour: int = DEFAULT_START_HOUR,
    end_hour: int = DEFAULT_END_HOUR,
) -> dict | None:
    """
    Walk through a day's bars and simulate one trade within [start_hour, end_hour].
    Returns a result dict if a signal fired, None if no signal.
    """
    trading_start = dt_time(start_hour, 0)
    end_time      = dt_time(end_hour, 55) if end_hour == 15 else dt_time(end_hour, 0)

    spy_bars  = [_to_bar(b) for b in spy_raw]
    vix_bars  = [_to_bar(b) for b in vix_raw]

    # Group 1-min TICK bars into 5-min buckets keyed by window start time.
    tick_by_window: dict[datetime, list[float]] = defaultdict(list)
    for t in tick_raw:
        dt = _bar_time(_to_bar(t))
        window = dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)
        tick_by_window[window].append(t.close)

    evaluator = SignalEvaluator(
        tick_low=TICK_LOW,
        tick_snap=TICK_SNAP_THR,
        vix_divergence_bars=VIX_DIV_BARS,
        volume_exhaustion_bars=VOL_BARS,
        support_buffer_pct=SUPPORT_BUFFER_PCT,
        support_levels=pdata.all_key_levels_spy(),  # SPX ÷ 10 → SPY scale
    )

    vix_by_time = {_bar_time(b): b for b in vix_bars}
    vix_closes: list[float] = []
    position: dict | None = None
    best_signal = None  # track highest conditions_met bar even if below threshold

    for i, bar in enumerate(spy_bars):
        bt = _bar_time(bar)

        # --- Monitor open position ---
        if position:
            if bar.low <= position["stop"]:
                return _result(date_str, position, position["stop"], "stop_hit", bt, pdata)
            if bar.high >= position["target"]:
                return _result(date_str, position, position["target"], "target_hit", bt, pdata)
            if bt.time() >= end_time:
                return _result(date_str, position, bar.close, "time_stop", bt, pdata)
            continue

        # Only evaluate within the requested window
        if bt.time() < trading_start or bt.time() >= end_time:
            vix_b = vix_by_time.get(bt)
            if vix_b:
                vix_closes.append(vix_b.close)
            continue

        # Feed 1-min TICK values for this 5-min window into the evaluator history
        window_key = bt.replace(minute=(bt.minute // 5) * 5, second=0, microsecond=0)
        tick_values = tick_by_window.get(window_key, [])
        if tick_values:
            evaluator.feed_tick_history(tick_values)

        # Use neutral TICK (0) when historical data is unavailable — bars still evaluated,
        # TICK flush+snap will be False, but the other 4 conditions can fire.
        tick_now = tick_values[-1] if tick_values else 0.0

        vix_b = vix_by_time.get(bt)
        if vix_b:
            vix_closes.append(vix_b.close)
        vix_now = vix_b.close if vix_b else None
        if vix_now is None:
            continue

        snap = MarketSnapshot(
            timestamp=bt,
            spy_price=bar.close,
            spy_bars=spy_bars[max(0, i - 6):i + 1],
            tick=tick_now,
            vix=vix_now,
            vix_bars=vix_closes[-6:],
        )

        signal = evaluator.evaluate(snap)
        if signal is None:
            continue
        if signal.conditions_met > 0:
            logger.debug(
                "  %s  conds=%d/5  tick=%s/%s  vix=%s  vol=%s  candle=%s  sup=%s",
                bt.strftime("%H:%M"), signal.conditions_met,
                "✓" if signal.tick_flush else "✗", "✓" if signal.tick_snap else "✗",
                "✓" if signal.vix_divergence else "✗",
                "✓" if signal.volume_exhaustion else "✗",
                "✓" if signal.candle_structure else "✗",
                f"{signal.support_level:.2f}" if signal.support_level else "✗",
            )
        if best_signal is None or signal.conditions_met > best_signal.conditions_met:
            best_signal = signal
        if signal.conditions_met < MIN_CONDITIONS:
            continue

        # Rules-based Periscope filter (approximates Claude's veto logic):
        # - Never go long when MMs are positioned bearishly overall
        # - Never go long below gamma flip with bearish Charm (amplified downside)
        if pdata is not None:
            if pdata.mm_bias == "bearish":
                logger.info("  %s SKIPPED (mm_bias=bearish)", bt.strftime("%H:%M"))
                continue
            if pdata.above_gamma_flip is False and pdata.charm_bias == "bearish":
                logger.info("  %s SKIPPED (below gamma flip + charm_bias=bearish)", bt.strftime("%H:%M"))
                continue

        # Signal fired — open simulated position
        entry = bar.close
        target, t_confluence, t_sources, t_method = pick_target(entry, pdata)
        stop,   s_sources,   s_method              = pick_stop(entry, pdata)

        position = {
            "entry_time":       bt,
            "entry_price":      entry,
            "stop":             stop,
            "stop_sources":     s_sources,
            "stop_method":      s_method,
            "target":           target,
            "target_confluence": t_confluence,
            "target_sources":   t_sources,
            "target_method":    t_method,
            "conditions_met":   signal.conditions_met,
            "tick_flush":       signal.tick_flush,
            "tick_snap":        signal.tick_snap,
            "vix_div":          signal.vix_divergence,
            "vol_exhaust":      signal.volume_exhaustion,
            "candle_ok":        signal.candle_structure,
            "support_level":    signal.support_level,
        }
        logger.info(
            "  %s ENTRY %.2f  stop=%.2f [%s]  target=%.2f [%s conf=%d]  conds=%d/5",
            bt.strftime("%H:%M"), entry,
            stop, "+".join(s_sources) or s_method,
            target, "+".join(t_sources) or t_method, t_confluence,
            signal.conditions_met,
        )

    # End of bars without an exit (shouldn't happen after the 15:55 check, but just in case)
    if position and spy_bars:
        last = spy_bars[-1]
        return _result(date_str, position, last.close, "eod_close", _bar_time(last), pdata)

    if best_signal is not None:
        s = best_signal
        logger.info(
            "  No signal (best: %d/5 — tick=%s/%s vix=%s vol=%s candle=%s sup=%s)",
            s.conditions_met,
            "✓" if s.tick_flush else "✗", "✓" if s.tick_snap else "✗",
            "✓" if s.vix_divergence else "✗",
            "✓" if s.volume_exhaustion else "✗",
            "✓" if s.candle_structure else "✗",
            f"{s.support_level:.2f}" if s.support_level else "✗",
        )
    else:
        logger.info("  No signal (no conditions met on any bar — TICK data likely unavailable)")
    return None


def _result(
    date_str: str,
    pos: dict,
    exit_price: float,
    reason: str,
    exit_time: datetime,
    pdata=None,
) -> dict:
    entry = pos["entry_price"]
    r = {
        # --- Identity ---
        "date":               date_str,
        "entry_time":         pos["entry_time"].strftime("%H:%M"),
        "exit_time":          exit_time.strftime("%H:%M"),
        # --- Prices ---
        "entry_price":        round(entry, 2),
        "stop":               round(pos["stop"], 2),
        "stop_sources":       pos["stop_sources"],
        "stop_method":        pos["stop_method"],
        "target":             round(pos["target"], 2),
        "target_sources":     pos["target_sources"],
        "target_method":      pos["target_method"],
        "target_confluence":  pos["target_confluence"],
        "exit_price":         round(exit_price, 2),
        "exit_reason":        reason,
        # --- Outcome ---
        "pnl_per_share":      round(exit_price - entry, 2),
        "pnl_pct":            round((exit_price - entry) / entry * 100, 3),
        # --- Signal conditions ---
        "conditions_met":     pos["conditions_met"],
        "tick_flush":         pos["tick_flush"],
        "tick_snap":          pos["tick_snap"],
        "vix_div":            pos["vix_div"],
        "vol_exhaust":        pos["vol_exhaust"],
        "candle_ok":          pos["candle_ok"],
        "support_level":      pos["support_level"],
        # --- Periscope context at signal time ---
        "periscope": None,
    }
    if pdata is not None:
        r["periscope"] = {
            "spx_price":         pdata.spx_price,
            "gamma_flip":        pdata.gamma_flip,
            "above_gamma_flip":  pdata.above_gamma_flip,
            "mm_bias":           pdata.mm_bias,
            "charm_bias":        pdata.charm_bias,
            "positions_bias":    pdata.positions_bias,
            "delta_trend":       pdata.delta_trend,
            "delta_sign":        pdata.delta_sign,
            "delta_turning":     pdata.delta_turning,
            "delta_exhaustion":  pdata.delta_exhaustion,
            "tide_direction":    pdata.tide_direction,
            "tide_sign":         pdata.tide_sign,
            "tide_turning":      pdata.tide_turning,
            "key_levels":        pdata.all_key_levels(),
            "summary":           pdata.summary(),
        }
    return r


# ---------------------------------------------------------------------------
# Screenshot selection
# ---------------------------------------------------------------------------

def _pick_exposure_shot(history_dir: Path, date_str: str, start_hour: int) -> Path | None:
    """
    Return the market_exposure screenshot whose hour is closest to (and not after)
    start_hour. Falls back to the earliest available if none precede start_hour.
    e.g. start_hour=11 → prefers 11h, then 10h, then whatever is earliest.
    """
    shots = sorted(history_dir.glob(f"{date_str}_*h_periscope_market_exposure.png"))
    if not shots:
        return None

    def _hour(p: Path) -> int:
        # filename: YYYYMMDD_10h_periscope_market_exposure.png
        part = p.name.split("_")[1]   # "10h"
        return int(part.rstrip("h"))

    candidates = [p for p in shots if _hour(p) <= start_hour]
    if candidates:
        return max(candidates, key=_hour)   # latest hour that doesn't exceed start_hour
    return shots[0]   # all shots are after start_hour — use earliest available


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backtest")


def _date_range(start: date, end: date) -> list[date]:
    """All weekdays (Mon-Fri) from start to end inclusive."""
    days = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def main() -> None:
    parser = argparse.ArgumentParser(description="SP500 Reversal Backtest")
    parser.add_argument("--mode", choices=["demo", "live"], default="demo")
    parser.add_argument("--start-date", metavar="YYYY-MM-DD", help="First date to backtest (inclusive)")
    parser.add_argument("--end-date",   metavar="YYYY-MM-DD", help="Last date to backtest (inclusive)")
    parser.add_argument("--periscope-hour", type=int, default=DEFAULT_PERISCOPE_HOUR, metavar="H",
                        help=f"Hour ET for the Periscope GEX snapshot (default {DEFAULT_PERISCOPE_HOUR} = pre/at open)")
    parser.add_argument("--start-hour", type=int, default=DEFAULT_START_HOUR, metavar="H",
                        help=f"Hour ET to start signal evaluation (default {DEFAULT_START_HOUR})")
    parser.add_argument("--end-hour",   type=int, default=DEFAULT_END_HOUR,   metavar="H",
                        help=f"Hour ET to stop entries / force exit (default {DEFAULT_END_HOUR})")
    parser.add_argument("--no-browser", action="store_true",
                        help="Skip browser capture; read from pre-saved history_YYYYMMDD/ dirs")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging verbosity (default INFO)")
    parser.add_argument("--verbose", action="store_true",
                        help="Shorthand for --log-level DEBUG")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else getattr(logging, args.log_level)
    logging.getLogger().setLevel(level)

    port = IBKR_PORT_DEMO if args.mode == "demo" else IBKR_PORT_LIVE

    # Resolve date range
    today = date.today()
    start_date = date.fromisoformat(args.start_date) if args.start_date else today
    end_date   = date.fromisoformat(args.end_date)   if args.end_date   else today
    trading_days = _date_range(start_date, end_date)

    if not trading_days:
        logger.error("No trading days in the requested range.")
        return

    logger.info(
        "Backtesting %d day(s) [%s → %s]  periscope=%02d:00  eval=%02d:00–%02d:00 ET%s",
        len(trading_days), start_date, end_date,
        args.periscope_hour, args.start_hour, args.end_hour,
        "  [no-browser: using pre-saved dirs]" if args.no_browser else "",
    )

    # Open browser and initialise Chrome CDP connection unless --no-browser
    if not args.no_browser:
        open_uw_browser()
        logger.info("Browser opened — waiting 15 s for tabs to load...")
        time.sleep(15)

    ib = IB()
    ib.connect(IBKR_HOST, port, clientId=IBKR_CLIENT_ID)
    logger.info("IBKR connected.")

    reader  = PeriscopeReader(model=CLAUDE_MODEL)
    results: list[dict] = []
    skipped: list[str] = []
    no_signal: list[str] = []

    try:
        for target_date in trading_days:
            date_str = target_date.strftime("%Y%m%d")
            date_fmt = target_date.isoformat()
            logger.info("--- %s ---", date_fmt)

            history_dir = PERISCOPE_DIR / f"history_{date_str}"

            ph = args.periscope_hour  # shorthand: hour for GEX snapshot

            if args.no_browser:
                # --- Offline mode: read from pre-saved history dir ---
                exposure_shot = _pick_exposure_shot(history_dir, date_str, ph)
                if not exposure_shot:
                    logger.warning("No market_exposure screenshot for %s — skipping.", date_fmt)
                    skipped.append(date_fmt)
                    continue
                logger.info("  Using screenshot: %s", exposure_shot.name)
                hour_prefix = exposure_shot.name.rsplit("periscope_market_exposure", 1)[0]
                shots: dict = {"periscope_market_exposure": exposure_shot}
                for slug, suffix in [
                    ("periscope_delta_flow", "periscope_delta_flow.png"),
                    ("flow_overview",        "flow_overview.png"),
                ]:
                    sibling = history_dir / f"{hour_prefix}{suffix}"
                    if sibling.exists():
                        shots[slug] = sibling
            else:
                # --- Live capture mode: navigate browser to date/hour ---
                # Re-use existing screenshot if already captured for this date+hour
                existing = _pick_exposure_shot(history_dir, date_str, ph)
                if existing:
                    logger.info("  Re-using existing screenshot: %s", existing.name)
                    hour_prefix = existing.name.rsplit("periscope_market_exposure", 1)[0]
                    shots = {"periscope_market_exposure": existing}
                    for slug, suffix in [
                        ("periscope_delta_flow", "periscope_delta_flow.png"),
                        ("flow_overview",        "flow_overview.png"),
                    ]:
                        sibling = history_dir / f"{hour_prefix}{suffix}"
                        if sibling.exists():
                            shots[slug] = sibling
                    tide_path = history_dir / f"{date_str}_market_tide.png"
                    if tide_path.exists() and "flow_overview" not in shots:
                        shots["flow_overview"] = tide_path
                else:
                    logger.info("  Capturing Periscope at %s %02d:00...", date_fmt, ph)
                    shots = capture_periscope_for_backtest(history_dir, target_date, ph)
                    if not shots.get("periscope_market_exposure"):
                        logger.warning("Browser capture failed for %s — skipping.", date_fmt)
                        skipped.append(date_fmt)
                        continue

            pdata = reader.read(shots)
            if pdata is None and not args.no_browser:
                # 9am slot may be sparse; retry at 10am before giving up.
                fallback_hour = 10
                if fallback_hour != ph:
                    logger.info(
                        "  PeriscopeReader returned None at %02d:00 — retrying at %02d:00",
                        ph, fallback_hour,
                    )
                    retry_shots = capture_periscope_for_backtest(history_dir, target_date, fallback_hour)
                    if retry_shots.get("periscope_market_exposure"):
                        pdata = reader.read(retry_shots)
            if pdata is None:
                logger.warning("PeriscopeReader returned None for %s — skipping.", date_fmt)
                skipped.append(date_fmt)
                continue

            key_levels = pdata.all_key_levels()
            logger.info("  Key levels (%d): %s", len(key_levels), key_levels)

            # Fetch IBKR bars
            spy_raw  = _fetch_bars(ib, _SPY_CONTRACT,  date_str, "5 mins", "TRADES")
            vix_raw  = _fetch_bars(ib, _VIX_CONTRACT,  date_str, "5 mins", "TRADES")
            tick_raw = _fetch_bars(ib, _TICK_CONTRACT, date_str, "1 min",  "TRADES")

            if not spy_raw:
                logger.warning("No SPY bars for %s — skipping.", date_fmt)
                skipped.append(date_fmt)
                continue

            if not tick_raw:
                logger.warning("No TICK bars for %s — TICK condition will not fire.", date_fmt)

            result = simulate_day(
                date_str, pdata, spy_raw, vix_raw, tick_raw,
                start_hour=args.start_hour,
                end_hour=args.end_hour,
            )
            if result:
                results.append(result)
                sign = "+" if result["pnl_per_share"] >= 0 else ""
                logger.info(
                    "  %s → %s  P&L %s%.2f/sh (%s%.3f%%)",
                    date_fmt, result["exit_reason"],
                    sign, result["pnl_per_share"],
                    sign, result["pnl_pct"],
                )
            else:
                logger.info("  %s → no signal fired.", date_fmt)
                no_signal.append(date_fmt)

    finally:
        ib.disconnect()
        logger.info("IBKR disconnected.")

    _save_json(results, no_signal, skipped, args)
    _print_summary(results, no_signal, skipped)


def _save_json(results: list[dict], no_signal: list[str], skipped: list[str], args) -> None:
    import json
    out_dir = Path("journal") / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now(MARKET_TZ).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"backtest_{run_ts}.json"
    payload = {
        "run_at":       datetime.now(MARKET_TZ).isoformat(),
        "params": {
            "start_date":  args.start_date,
            "end_date":    args.end_date,
            "start_hour":  args.start_hour,
            "end_hour":    args.end_hour,
        },
        "trades":       results,
        "no_signal_days": no_signal,
        "skipped_days":   skipped,
        "summary": {
            "total_trades": len(results),
            "wins":   sum(1 for r in results if r["pnl_per_share"] > 0),
            "losses": sum(1 for r in results if r["pnl_per_share"] <= 0),
            "total_pnl_per_share": round(sum(r["pnl_per_share"] for r in results), 2),
        } if results else {},
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("Results saved → %s", out_path)


def _print_summary(results: list[dict], no_signal: list[str], skipped: list[str]) -> None:
    W = 90
    print("\n" + "=" * W)
    print("BACKTEST RESULTS")
    print("=" * W)

    if results:
        hdr = (
            f"{'Date':<12} {'In':>5} {'Out':>5} {'Entry':>7} {'Stop':>7} "
            f"{'Target':>7} {'Exit':>7} {'Reason':<12} {'P&L/sh':>7} {'P&L%':>7} "
            f"{'Conds':>5} {'Conf':>5}"
        )
        print(hdr)
        print("-" * W)
        for r in results:
            sign = "+" if r["pnl_per_share"] >= 0 else ""
            print(
                f"{r['date']:<12} {r['entry_time']:>5} {r['exit_time']:>5} "
                f"{r['entry_price']:>7.2f} {r['stop']:>7.2f} {r['target']:>7.2f} "
                f"{r['exit_price']:>7.2f} {r['exit_reason']:<12} "
                f"{sign}{r['pnl_per_share']:>6.2f} {sign}{r['pnl_pct']:>6.3f}% "
                f"{r['conditions_met']:>4}/5 {r['target_confluence']:>5}"
            )
        print("-" * W)

        wins   = [r for r in results if r["pnl_per_share"] > 0]
        losses = [r for r in results if r["pnl_per_share"] <= 0]
        total  = sum(r["pnl_per_share"] for r in results)

        print(f"\n  Trades   : {len(results)}")
        print(f"  Win rate : {len(wins)}/{len(results)} ({len(wins)/len(results)*100:.0f}%)")
        print(f"  Total P&L: {total:+.2f} per share")
        print(f"  Avg P&L  : {total/len(results):+.2f} per share")
        if wins:
            print(f"  Avg win  : {sum(r['pnl_per_share'] for r in wins)/len(wins):+.2f}")
        if losses:
            print(f"  Avg loss : {sum(r['pnl_per_share'] for r in losses)/len(losses):+.2f}")
    else:
        print("  No trades fired across all days.")

    if no_signal:
        print(f"\n  No signal : {len(no_signal)} day(s) — {', '.join(no_signal)}")
    if skipped:
        print(f"  Skipped   : {len(skipped)} day(s) — {', '.join(skipped)}")
    print("=" * W)


if __name__ == "__main__":
    main()
