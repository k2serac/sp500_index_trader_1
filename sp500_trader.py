"""
S&P 500 Index Trader — main entry point.

Monitors intraday SPY price action via IBKR Market Data, detects mean-reversion
setups using TICK, VIX, volume, and candle structure signals, and optionally
calls Claude for a go/no-go verdict before placing a trade.

Usage:
    python sp500trader.py --mode demo   # paper trading via TWS (port 7497)
    python sp500trader.py --mode live   # live trading via TWS  (port 7496)
    python sp500trader.py --mode demo --dry-run   # full pipeline, no orders
    python sp500trader.py --mode demo --verbose   # DEBUG logging
"""

import argparse
import logging
import time
import tomllib
from datetime import date, datetime, time as dt_time
from pathlib import Path

import pandas as pd
from ib_async import IB
from trading_common.trade_lib import TradeHour, ensure_ibkr_connected

from libs import (
    MarketDataFeed, SignalEvaluator, ClaudeAnalyst,
    TradeManager, TradingJournal, open_uw_browser, capture_periscope_screenshots,
    select_periscope_datetime, select_periscope_date_all,
    PeriscopeReader, PeriscopeParseError,
    is_rth, minutes_since_open, in_trading_window, in_periscope_window,
)

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------

_CONFIG_FILE = Path("config/config.toml")
with open(_CONFIG_FILE, "rb") as _f:
    _cfg = tomllib.load(_f)


class _EasternFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=MARKET_TZ)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S %Z")


_handler = logging.StreamHandler()
_handler.setFormatter(_EasternFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger("sp500trader")

# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------

IBKR_HOST       = _cfg["ibkr"]["host"]
IBKR_PORT_LIVE  = _cfg["ibkr"]["port_live"]
IBKR_PORT_DEMO  = _cfg["ibkr"]["port_demo"]
IBKR_CLIENT_ID  = _cfg["ibkr"]["client_id"]

INSTRUMENT          = _cfg["trading"]["instrument"]
MAX_AMM_PER_TRADE   = _cfg["trading"]["max_amm_per_trade"]
STOP_BUFFER_PCT     = _cfg["trading"]["stop_buffer_pct"]
TARGET_PCT          = _cfg["trading"]["target_pct"]
TRADING_START_MIN   = _cfg["trading"]["trading_start_minutes"]
TRADING_END_MIN     = _cfg["trading"]["trading_end_minutes"]

LOOP_INTERVAL_SECS  = _cfg["signals"]["loop_interval_secs"]
CANDLE_TF           = _cfg["signals"]["candle_timeframe"]
TICK_LOW            = _cfg["signals"]["tick_low_threshold"]
TICK_SNAP           = _cfg["signals"]["tick_snap_threshold"]
VIX_DIV_BARS        = _cfg["signals"]["vix_divergence_bars"]
VOL_BARS            = _cfg["signals"]["volume_exhaustion_bars"]
SUPPORT_BUFFER_PCT  = _cfg["signals"]["support_buffer_pct"]

USE_CLAUDE          = _cfg["claude"]["use_claude"]
CLAUDE_MODEL        = _cfg["claude"]["model"]

PERISCOPE_DIR         = Path(_cfg["periscope"]["snapshot_dir"])
PERISCOPE_READ_RETRIES = _cfg["periscope"].get("read_retries", 1)

# Minimum conditions required to call Claude
MIN_CONDITIONS_FOR_CLAUDE = 3


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _sleep(secs: float) -> None:
    """Sleep in 1-second chunks so Ctrl+C is detected promptly."""
    end = time.monotonic() + secs
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))


def _next_session_wake(now: datetime, trade_hour: "TradeHour") -> datetime:
    """Return the 9:20 ET datetime of the next trading day after now."""
    search_from = 0 if now.time() < dt_time(9, 21) else 1
    for offset in range(search_from, search_from + 8):
        candidate = now + pd.Timedelta(days=offset)
        if trade_hour.is_trading_day(candidate):
            return candidate.replace(hour=9, minute=20, second=0, microsecond=0)
    return now + pd.Timedelta(days=7)  # safety fallback


def main() -> None:
    parser = argparse.ArgumentParser(description="S&P 500 Index Trader")
    parser.add_argument("--mode", choices=["demo", "live"], default="demo")
    parser.add_argument("--dry-run", action="store_true", help="Run pipeline without placing orders")
    parser.add_argument("--verbose", action="store_true", help="DEBUG logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    port = IBKR_PORT_DEMO if args.mode == "demo" else IBKR_PORT_LIVE
    logger.info("Starting in %s mode (IBKR port %d)%s.", args.mode.upper(), port,
                " [DRY RUN]" if args.dry_run else "")

    open_uw_browser()
    # Give Chrome time to load before we try to interact with it via CDP.
    _sleep(15)
    select_periscope_date_all(target_date=date.today())

    # Connect to IBKR
    ib = IB()
    ib.connect(IBKR_HOST, port, clientId=IBKR_CLIENT_ID)
    logger.info("IBKR connected.")

    # Initialise components
    journal  = TradingJournal(bot_name="sp500trader")
    feed     = MarketDataFeed(ib, candle_timeframe=CANDLE_TF)
    evaluator = SignalEvaluator(
        tick_low=TICK_LOW,
        tick_snap=TICK_SNAP,
        vix_divergence_bars=VIX_DIV_BARS,
        volume_exhaustion_bars=VOL_BARS,
        support_buffer_pct=SUPPORT_BUFFER_PCT,
    )
    analyst  = ClaudeAnalyst(model=CLAUDE_MODEL) if USE_CLAUDE else None
    periscope_reader = PeriscopeReader(model=CLAUDE_MODEL)
    trader   = TradeManager(
        ib=ib,
        instrument=INSTRUMENT,
        max_amm_per_trade=MAX_AMM_PER_TRADE,
        stop_buffer_pct=STOP_BUFFER_PCT,
        target_pct=TARGET_PCT,
        journal=journal,
    )

    trade_hour = TradeHour()
    journal.log_session_start()
    loop_count = 0
    last_screenshot_window: datetime | None = None  # tracks the last 10-min bucket screenshotted
    latest_periscope_data = None
    latest_periscope_date: date | None = None       # date of the last successful Periscope read
    latest_periscope_shots: dict = {}

    try:
        while True:
            try:
                loop_count += 1
                now = datetime.now(MARKET_TZ)

                # --- Idle sleep: only active 9:21–16:00 ET on trading days ---
                if not in_periscope_window(now) or not trade_hour.is_trading_day():
                    wake_at = _next_session_wake(now, trade_hour)
                    secs = (wake_at - now).total_seconds()
                    if secs > LOOP_INTERVAL_SECS:
                        logger.info("Market inactive — sleeping until %s (%.1f hr).",
                                    wake_at.strftime("%Y-%m-%d %H:%M ET"), secs / 3600)
                        _sleep(secs)
                        continue

                # --- Periscope screenshots at :01, :11, :21 ... from 9:21 until market close ---
                # Periscope updates on the 10-min mark; we wait 1 min for data to settle.
                current_window = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)
                if in_periscope_window(now) and now.minute % 10 == 1 and current_window != last_screenshot_window:
                    shots = capture_periscope_screenshots(PERISCOPE_DIR)
                    last_screenshot_window = current_window
                    if shots:
                        periscope_data = None
                        max_attempts = 1 + PERISCOPE_READ_RETRIES
                        for attempt in range(1, max_attempts + 1):
                            try:
                                periscope_data = periscope_reader.read(shots)
                            except PeriscopeParseError as exc:
                                logger.warning(
                                    "Periscope parse error (attempt %d/%d): %s",
                                    attempt, max_attempts, exc,
                                )
                                periscope_data = None
                            if periscope_data is not None:
                                break
                            if attempt < max_attempts:
                                logger.info(
                                    "Periscope read returned no data (attempt %d/%d) — retaking screenshot...",
                                    attempt, max_attempts,
                                )
                                shots = capture_periscope_screenshots(PERISCOPE_DIR)
                        if periscope_data is None:
                            if latest_periscope_date != now.date():
                                # First read of the day failed — no GEX basis to trade on.
                                wake_at = _next_session_wake(now, trade_hour)
                                secs = (wake_at - now).total_seconds()
                                logger.critical(
                                    "Periscope first read of the day failed after %d attempt(s) "
                                    "— skipping today's session. Resuming %s.",
                                    max_attempts,
                                    wake_at.strftime("%Y-%m-%d %H:%M ET"),
                                )
                                _sleep(secs)
                            else:
                                logger.error(
                                    "Periscope read failed after %d attempt(s) — keeping last known data.",
                                    max_attempts,
                                )
                        else:
                            latest_periscope_data = periscope_data
                            latest_periscope_date = now.date()
                            latest_periscope_shots = shots
                            logger.info("Periscope update:\n%s", periscope_data.summary())
                            evaluator.update_support_levels(periscope_data.all_key_levels_spy())
                logger.info("Loop #%d — %s", loop_count, now.strftime("%H:%M:%S ET"))

                ensure_ibkr_connected(ib, IBKR_HOST, port, IBKR_CLIENT_ID)

                # --- Fetch market snapshot ---
                snap = feed.snapshot()

                # --- Monitor open position stops/targets ---
                if trader.has_position and snap.spy_price is not None:
                    trader.check_stops_and_targets(snap.spy_price)

                # --- Pre-close sweep: close all positions 5 min before RTH close ---
                if now.time() >= dt_time(15, 55):
                    if snap.spy_price is not None:
                        journal.log_eod_snapshot(snap.spy_price, es_price=snap.es_price)
                    if trader.has_position:
                        logger.info("Pre-close sweep — closing all positions.")
                        trader.close_all(reason="pre-close sweep")

                # --- Only look for new entries within the trading window ---
                if latest_periscope_data is None:
                    logger.debug("No Periscope data yet — skipping signal evaluation.")
                    _sleep(LOOP_INTERVAL_SECS)
                    continue

                if not in_trading_window(now, TRADING_START_MIN, TRADING_END_MIN):
                    logger.debug("Outside trading window — skipping signal evaluation.")
                    _sleep(LOOP_INTERVAL_SECS)
                    continue

                if trader.has_position:
                    _sleep(LOOP_INTERVAL_SECS)
                    continue

                # --- Evaluate reversal signal ---
                signal = evaluator.evaluate(snap)
                if signal is None:
                    _sleep(LOOP_INTERVAL_SECS)
                    continue

                logger.debug("Signal: %d/5 conditions\n%s", signal.conditions_met, signal.summary())

                if signal.conditions_met < MIN_CONDITIONS_FOR_CLAUDE:
                    _sleep(LOOP_INTERVAL_SECS)
                    continue

                logger.info("Potential reversal — %d/5 conditions met. Logging signal.", signal.conditions_met)
                journal.log_signal(signal, periscope_data=latest_periscope_data)

                # --- Call Claude for go/no-go ---
                if analyst is not None:
                    logger.info("Calling Claude for verdict...")
                    exposure_path = latest_periscope_shots.get("periscope_market_exposure")
                    verdict = analyst.analyze(
                        signal,
                        periscope_data=latest_periscope_data,
                        periscope_screenshot_path=str(exposure_path) if exposure_path else None,
                    )
                    journal.log_claude_verdict(signal, verdict)
                    logger.info(
                        "Claude verdict: GO=%s  confidence=%d/10  %s",
                        verdict.go, verdict.confidence, verdict.reasoning,
                    )

                    if verdict.go and verdict.confidence >= 6:
                        if args.dry_run:
                            logger.info("[DRY RUN] Would place entry order.")
                        else:
                            trader.enter(signal, verdict)
                    else:
                        logger.info("Claude said no — standing down.")
                else:
                    # No Claude — apply a stricter threshold
                    if signal.conditions_met >= 5:
                        logger.info("All 5 conditions met — placing trade without Claude.")
                        if not args.dry_run:
                            from libs.signal_lib import ClaudeVerdict
                            auto_verdict = ClaudeVerdict(
                                go=True, confidence=7,
                                support_level=signal.support_level,
                                stop_level=None, target_level=None,
                                reasoning="All 5 conditions met — auto entry.",
                                raw_response="",
                            )
                            trader.enter(signal, auto_verdict)

                _sleep(LOOP_INTERVAL_SECS)

            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                logger.error("Unhandled error in main loop — will retry: %s", exc, exc_info=True)
                _sleep(LOOP_INTERVAL_SECS)

    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down.")
    finally:
        trader.close_all(reason="bot shutdown")
        journal.log_session_stop()
        ib.disconnect()
        logger.info("Disconnected.")


if __name__ == "__main__":
    main()
