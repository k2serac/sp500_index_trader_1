# SP500 Index Trader — Bot Context

> Living document. Update this as the bot evolves.

---

## Strategy

Intraday mean-reversion on SPX/SPY. Target: days that open down on a macro catalyst (CPI, PPI, Fed, etc.), sell off for 1-2 hours, find a support level, then reverse. Classic pattern: open → morning selloff → bottom (often 10:30–11:30am ET) → recovery.

Entry is long (SPY shares). Hold intraday. Exit at prior resistance or pre-close sweep at 15:55 ET.

---

## Signal Conditions (5 total, evaluated by `SignalEvaluator`)

| # | Condition | Logic |
|---|---|---|
| 1 | TICK flush + snap | NYSE TICK hits < -800 (selling climax), then snaps back above +200 |
| 2 | VIX divergence | SPX tests lows again but VIX does not make a new high (panic exhausted) |
| 3 | Volume exhaustion | Consecutive down candles with declining volume |
| 4 | Candle structure | 5-min candle closes above the low bar's high (structure shift) |
| 5 | Price at support | Price within buffer % of a known GEX wall / support level |

- **≥ 3/5** conditions → call Claude for go/no-go
- **All 5** → auto-entry if Claude is disabled
- Claude must return `go=True` and `confidence ≥ 6` to place a trade

---

## Architecture Split

| Layer | Responsibility |
|---|---|
| **Python** | Polling IBKR, threshold math, state management, order execution, logging |
| **Claude** | Go/no-go verdict on signal confluence; Periscope screenshot interpretation (GEX extraction); ambiguity resolution |

Claude is called **only when Python flags a potential setup** — not on every loop tick.

---

## Data Sources

| Signal | Source | Status |
|---|---|---|
| SPY price + 5-min bars | IBKR via ib_async (`Stock("SPY", "SMART", "USD")`) | ✅ done |
| SPY bid/ask spread | IBKR — single `reqTickers` call returns price + bid + ask | ✅ done |
| NYSE TICK | IBKR (`Index("$TICK", "NYSE")`) | ✅ done (live only — not available on demo) |
| VIX | IBKR (`Index("VIX", "CBOE")`) | ✅ done |
| ES futures | IBKR (`ContFuture("ES", "GLOBEX", "USD")`) — auto-tracks front month | ✅ done |
| Opening gap % | IBKR — (today_open − prior_close) / prior_close × 100 from 2-day daily bars | ✅ done |
| GEX walls / support levels | UW Periscope screenshot → Claude Vision → `PeriscopeReader` | ✅ done |
| Periscope MM positioning | UW Periscope screenshot → Claude Vision → `PeriscopeReader` | ✅ done |

**Key constraint:** IBKR for all live market data. No UW API calls.
Periscope is screenshot-only — UW support confirmed scraping/automation violates ToS.

**TICK note:** `$TICK` historical bars are not available on IBKR demo accounts. Available on live account. yfinance does NOT carry NYSE TICK. In backtesting on demo, TICK condition never fires (neutral 0.0 substituted); in live-account backtesting it works normally.

---

## Periscope Screenshot Workflow

### Live Mode (`capture_periscope_screenshots`)

1. Chrome opens tabs at startup via `open_uw_browser()` (reads "UnusualWhales" Chrome bookmark folder; `_REQUIRED_PERISCOPE_TABS` ensures market-exposure, delta-flow, and flow/overview are always opened even if absent from bookmarks)
2. Any existing Chrome bot-profile instance is killed before launching fresh
3. Chrome launched with CDP on port 9222: `--remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir=~/.config/chrome-sp500bot --no-first-run --no-default-browser-check --hide-crash-restore-bubble`
4. At startup, `select_periscope_date_all(date.today())` navigates both market-exposure and flow/overview to today's date
5. `capture_periscope_screenshots()` connects via CDP WebSocket, sends `Page.reload` then `Page.captureScreenshot` to each tab — reload advances page to latest 10-min bucket automatically
6. Screenshots taken at :01, :11, :21, :31, :41, :51 (1 min after each Periscope 10-min update), from **9:21 ET → 16:00 ET** only
7. Retry logic: 3 attempts per tab with 5s gap if WebSocket connection fails
8. Must use `127.0.0.1` not `localhost` in WebSocket URL — `localhost` resolves to IPv6 on this machine

### Historical Mode (`capture_periscope_historical`)

Captures Periscope data for a date range for backtesting / analysis.

**Market-exposure (fully working ✅):**
- Navigates to each date via prev/next day button clicks, polling DOM until date actually changes (up to 15s per click — market-exposure renders slowly)
- Date navigation uses `_JS_CLICK_SELECTOR` (JS `dispatchEvent(new MouseEvent('click', ...))`) — coordinate-based `Input.dispatchMouseEvent` does NOT work for date picker on market-exposure (button may be off-screen or behind overlay)
- Removed `[data-testid="market-exposures-tick-chart"] span[role="button"]` from date-reading JS — matched a stale chart element, not the date picker
- Queries `<a>` anchor labels on the chart x-axis (`_JS_GET_CHART_HOUR_LINKS`) to get available hour anchors (typically 10 AM, 11 AM, 12 PM ... 4 PM)
- Clicks each anchor via CDP `Input.dispatchMouseEvent` (fire-and-forget); waits 15s for React re-render
- Keys in result dict: `"YYYYMMDD_HHh"` → file `YYYYMMDD_HHh_periscope_market_exposure.png`

**Market-tide / Flow Overview (date navigation ✅):**
- URL: `https://unusualwhales.com/flow/overview`
- Captures one screenshot per day (no hour anchors — date only)
- Date navigation works: `[data-testid="date-picker-button"]` reads current date; same `_JS_CLICK_SELECTOR` click approach

**`capture_periscope_for_backtest(snapshot_dir, target_date, target_hour) → dict[str, Path]`:**
- New function for backtest use — navigates to one specific date/hour, captures market-exposure + market-tide WITHOUT reloading (reload resets to today)
- If `target_hour` has no chart anchor ≤ it, falls back to earliest available anchor (typically 10h)
- Saves to `history_YYYYMMDD/YYYYMMDD_HHh_periscope_market_exposure.png` and `YYYYMMDD_market_tide.png`
- Screenshots cached — re-runs reuse existing files

### CDP Key Details

- `_cdp_evaluate(ws_url, js)` — `Runtime.evaluate` with 30s timeout; **blocked 30+ s after anchor click** (React sync render)
- `_cdp_click_xy(ws_url, x, y)` — fire-and-forget `Input.dispatchMouseEvent`; used for chart hour anchor clicks
- `_JS_CLICK_SELECTOR` — JS `dispatchEvent(new MouseEvent('click', {bubbles:true}))` directly on element; used for date picker navigation (works when coordinate click doesn't)
- `_cdp_reload(ws_url)` — `Page.enable` → `Page.reload` → wait for `Page.loadEventFired`
- `_cdp_screenshot_tab(ws_url, path)` — `Page.captureScreenshot`; works even during React re-render
- `_cdp_open_tab(url)` — `Target.createTarget` on any existing tab; used to open missing tabs
- `_cdp_ws_for_tab(tab_url)` — finds WebSocket URL for tab whose URL starts with `tab_url`

### URL Constants (`libs/trade_lib.py`)

```python
_PERISCOPE_MARKET_EXPOSURE_URL = "https://unusualwhales.com/periscope/market-exposure"
_PERISCOPE_MARKET_TIDE_URL    = "https://unusualwhales.com/flow/overview"   # Market Tide chart

_REQUIRED_PERISCOPE_TABS = [
    "https://unusualwhales.com/periscope/market-exposure",
    "https://unusualwhales.com/periscope/delta-flow",
    "https://unusualwhales.com/flow/overview",
]
```

**Periscope data by session phase:**
- **Pre-market (before 9:30):** Initial straddle price + foundational MM net positioning benchmarks based on prior session's final open interest — captured at 9:21 ET
- **Intraday:** Live GEX walls, gamma flip level, net MM positioning — updated every 10 min

**Tab slugs** (keys in `shots` dict / PNG filename suffixes):
- `periscope_market_exposure` — Gamma, Vanna, Charm, Positions columns; gamma flip dotted line (required)
- `periscope_delta_flow` — intraday delta accumulation/distribution (optional)
- `flow_overview` — Market Tide chart (optional)

---

## PeriscopeReader & PeriscopeData

`libs/periscope_lib.py`

`PeriscopeReader.read(screenshots: dict[str, Path]) -> PeriscopeData | None`

Sends `periscope_market_exposure` (+optional `periscope_delta_flow`) to Claude Vision.
Extracts structured JSON with:
- `spx_price`, `gamma_flip`, `above_gamma_flip`
- `gex_support`, `gex_resistance` — significant GREEN/RED Gamma bars below/above price
- `vanna_support`, `vanna_resistance` — significant Vanna levels
- `charm_support`, `charm_resistance`, `charm_bias` — Charm levels + dominant direction
- `positions_support`, `positions_resistance`, `positions_bias` — net MM open interest levels
- `mm_bias` — overall bias combining all four Greeks + delta flow
- `delta_trend`, `delta_sign`, `delta_turning`, `delta_exhaustion`, `delta_notes` — from Delta Flow screenshot
- `tide_direction`, `tide_sign`, `tide_turning`, `spx_tide_divergence`, `tide_notes` — separate Claude call on flow_overview

`PeriscopeData` methods:
- `all_gex_levels()` — GEX support + resistance combined (SPX scale)
- `all_key_levels()` — all 4 Greeks combined (SPX scale, ~5400 range)
- `all_key_levels_spy()` — all key levels ÷ 10 → SPY scale (~540 range); fed to `SignalEvaluator`
- `to_spy(spx_level)` — convert single SPX level to SPY scale
- `summary()` — human-readable string for logging and Claude context

**Critical scale note:** Periscope shows SPX-denominated levels (~5400). SPY trades at ~1/10th scale (~540). The ratio is exactly 10:1. `SignalEvaluator` compares against SPY prices so must use `all_key_levels_spy()`. Market Tide is SPY-based (qualitative fields only — no scale issue).

---

## Backtesting Framework (`backtest.py`)

Replays historical Periscope GEX data against IBKR bar data to validate signal thresholds and P&L.

### How it works

1. For each trading day in the date range, navigate the UW browser to that date/hour and capture Periscope screenshots (market-exposure + market-tide) via `capture_periscope_for_backtest()`
2. Screenshots saved to `periscope_snapshots/history_YYYYMMDD/` — re-runs reuse cached files
3. Run `PeriscopeReader` on the screenshots → `PeriscopeData` with GEX key levels
4. Fetch SPY (5-min), VIX (5-min), TICK (1-min) bars from IBKR for that date
5. Walk 5-min SPY bars through `SignalEvaluator`; feed 1-min TICK values via `evaluator.feed_tick_history()`
6. On ≥3/5 conditions: apply rules-based Periscope filter, then simulate entry at bar close
7. Exit at: nearest key-level resistance (multi-Greek confluence preferred), 0.50% fallback, or 15:55 time-stop
8. Save per-trade JSON to `journal/backtest/backtest_YYYYMMDD_HHMMSS.json`

### Rules-based Periscope filter (pre-entry gate)

Mimics Claude's most consistent vetoes — applied before every simulated entry:
- **Skip** if `mm_bias == "bearish"` — MMs positioned against long
- **Skip** if `above_gamma_flip == False` AND `charm_bias == "bearish"` — negative gamma + bearish charm = amplified downside

Validated on 2026-05-19/20 backtest: filtered the losing trade (mm_bias=bearish, charm_bias=bearish), kept the winning trade (mm_bias=neutral).

### Stop / Target selection

- **Target**: nearest resistance level above entry, across all 4 Greeks (SPY scale via `to_spy()`); prefer multi-Greek confluence; fallback = 0.50% above entry
- **Stop**: nearest support level below entry across all 4 Greeks; fallback = 0.20% below entry
- `target_confluence`: count of Greek categories agreeing on that level (4 = strongest)

### Arguments

```
--mode demo/live          IBKR port (7497/7496)
--start-date YYYY-MM-DD   First date (default: today)
--end-date YYYY-MM-DD     Last date (default: today)
--periscope-hour H        Hour ET for GEX snapshot (default: 9, snaps to earliest anchor ~10h)
--start-hour H            Hour ET to begin signal evaluation (default: 10)
--end-hour H              Hour ET to stop entries / force exit (default: 15)
--no-browser              Use pre-saved history_YYYYMMDD/ dirs, skip browser capture
--verbose                 DEBUG logging (shows per-bar condition breakdown)
```

### Known limitations

- TICK historical data not available on IBKR demo (available on live account); backtest substitutes 0.0 so bars are still evaluated
- IBKR demo may return incomplete intraday bars (e.g., only to noon)
- Claude Vision interpretation of same screenshot can vary slightly between runs (charm_bias "bearish"↔"neutral" borderline)
- One Periscope snapshot per day — does not update levels mid-session

---

## Main Loop Flow (`sp500_trader.py`)

```
startup:
  open_uw_browser()                  # kill old Chrome, launch fresh with CDP
  _sleep(15)                         # let Chrome load (interruptible 1-sec chunks)
  select_periscope_date_all(today)   # navigate market-exposure + flow/overview to today

loop (inner exceptions caught → retry after interval):
  if not in_periscope_window or not trading day → idle sleep until 9:20 ET next trading day

  if :X1 minute mark and new 10-min window:
    shots = capture_periscope_screenshots()   # reload all 3 tabs, screenshot
    periscope_data = PeriscopeReader.read(shots)
    evaluator.update_support_levels(periscope_data.all_key_levels_spy())  # SPY scale!

  ensure_ibkr_connected(ib, ...)     # reqCurrentTime() heartbeat; reconnects if stale

  snap = MarketDataFeed.snapshot()   # SPY price+bars+bid/ask+gap, TICK, VIX, ES
  trader.check_stops_and_targets(snap.spy_price)  # if position open

  if 15:55 ET → log_eod_snapshot(); trader.close_all("pre-close sweep")
  if outside trading window or has_position → sleep, continue

  signal = SignalEvaluator.evaluate(snap)
  if signal.conditions_met >= 3:
    journal.log_signal(signal, periscope_data=latest_periscope_data)
    verdict = ClaudeAnalyst.analyze(signal, periscope_data=..., periscope_screenshot_path=...)
    journal.log_claude_verdict(signal, verdict)
    if verdict.go and verdict.confidence >= 6 → trader.enter(signal, verdict)
```

---

## File Map

```
sp500_trader.py               Main loop, config loading, signal→trade orchestration
backtest.py                   Backtesting framework — browser capture + IBKR bars + simulation
test_date_picker.py           CDP test: probe date/hour anchors; --history captures today
test_screenshots.py           Dev test: open Chrome + capture all 3 UW tabs
test_periscope_reader.py      Dev test: run PeriscopeReader against saved snapshot files
config/config.toml            All tunable parameters (incl. [periscope] snapshot_dir)
config/prompts/reversal.txt   Claude system prompt for go/no-go verdict
config/context/reversal.txt   Static market context: [OPENING_GAP] and [ES_FUTURES] sections
libs/
  __init__.py                 Re-exports all public symbols
  signal_lib.py               MarketDataFeed, SignalEvaluator, ClaudeAnalyst, ReversalSignal, ClaudeVerdict
  trade_lib.py                TradeManager, open_uw_browser(), capture_periscope_screenshots(),
                              capture_periscope_historical(), capture_periscope_for_backtest(),
                              select_periscope_datetime(), select_periscope_date_all(), _cdp_* helpers
  periscope_lib.py            PeriscopeReader, PeriscopeData
  journal_lib.py              TradingJournal
  utils.py                    is_rth, minutes_since_open, in_trading_window, in_periscope_window
periscope_snapshots/          Screenshot storage
  YYYYMMDD_HHMM_<slug>.png    Live mode screenshots
  history_YYYYMMDD/           Historical capture output (backtest screenshots cached here)
    YYYYMMDD_HHh_periscope_market_exposure.png
    YYYYMMDD_market_tide.png
journal/                      Trade logs
  backtest/                   Backtest JSON results (backtest_YYYYMMDD_HHMMSS.json)
logs/                         Bot logs
```

---

## Shared Library

`/home/nicu/repos/trading_common/` — always check here before reimplementing:
- `TradeHour` — market status (`"rth"/"ext"/"closed"`), trading day checks, market open/close times
- `IBapi` — IBKR ib_async wrapper (prices, orders, positions)
- `ClaudeSentiment` — headline sentiment with per-strategy prompts
- `TradingJournal`, `OpenTradeRegistry` — journal and cross-restart position tracking
- `ensure_ibkr_connected(ib, host, port, client_id)` — `reqCurrentTime()` heartbeat; reconnects if stale; used by both sp500 and us_stock_trader_1

---

## What's Built

- [x] IBKR connection + market data feed (SPY price+bars+bid/ask, TICK, VIX, ES futures, opening gap %)
- [x] 5-condition signal evaluator with rolling TICK history + `feed_tick_history()` for backtest
- [x] ClaudeAnalyst with `periscope_data` + `periscope_screenshot_path` support
- [x] TradeManager (entry sizing, stop/target monitoring, pre-close sweep)
- [x] TradingJournal with enhanced logging: signal context, Claude verdict, EOD snapshot, ES price, opening gap, Periscope summary
- [x] Chrome launch via "UnusualWhales" Chrome bookmark folder (CDP port 9222)
- [x] CDP screenshot capture of all 3 UW tabs (`capture_periscope_screenshots()`)
- [x] Screenshot schedule: 9:21–16:00 ET at :X1 min marks (`in_periscope_window()`)
- [x] Idle sleep — wakes at 9:20 ET next trading day (1-sec interruptible chunks via `_sleep()`)
- [x] Inner loop exception handler — unhandled errors caught, logged with traceback, retried
- [x] TWS session timeout detection — `ensure_ibkr_connected()` heartbeat via `reqCurrentTime()`
- [x] PeriscopeReader: Claude Vision → PeriscopeData — full four-Greek structured extraction:
  - GEX support/resistance (Gamma)
  - Vanna support/resistance
  - Charm support/resistance + charm_bias
  - Positions support/resistance + positions_bias
  - Delta Flow: delta_trend, delta_sign, delta_turning, delta_exhaustion, delta_notes
  - Market Tide: tide_direction, tide_sign, tide_turning, spx_tide_divergence via separate Claude call
- [x] SPX/SPY scale fix: `all_key_levels_spy()` and `to_spy()` on PeriscopeData (÷10); `SignalEvaluator` fed SPY-scale levels
- [x] All four Greeks contribute to `all_key_levels()` — levels shared across Greeks = high-confidence confluence zones
- [x] Periscope context in Claude calls: `periscope_data.summary()` + market_exposure screenshot
- [x] `[OPENING_GAP]` and `[ES_FUTURES]` sections in Claude reversal context
- [x] CDP date navigation: polling until DOM changes (up to 15s/click); JS `dispatchEvent` click (not coordinate-based) for date picker reliability
- [x] Historical capture: `capture_periscope_historical(start_date, end_date, start_hour, end_hour)`
- [x] `capture_periscope_for_backtest(snapshot_dir, date, hour)` — single-date/hour capture for backtest
- [x] `select_periscope_date_all()` navigates both periscope tabs to today at startup
- [x] `_cdp_open_tab()` auto-opens missing tabs via Target.createTarget
- [x] **Backtesting framework** (`backtest.py`) — browser capture + IBKR bars + simulation + JSON output
  - Rules-based Periscope filter (mm_bias, gamma flip + charm)
  - Multi-Greek stop/target selection with confluence scoring
  - Per-bar diagnostic logging (`--verbose`)
  - `--no-browser` mode for re-runs on cached screenshots
  - Separates `--periscope-hour` (GEX snapshot time) from `--start-hour` (signal eval start)

---

## What's Next

### Backtesting
- [ ] **Capture more history** — run `backtest.py` on more dates to build statistics; current data: 2026-05-19, 2026-05-20
- [ ] **Live account backtest** — run with `--mode live` for real TICK data + full intraday bars (demo returns incomplete bars)
- [ ] **Tune rules-based filter** — update mm_bias / gamma flip filter thresholds based on wider backtest results
- [ ] **Review JSON output** — after each run, trace results to tune signal thresholds or Claude prompt

### Reliability
- [ ] **Disable TWS auto-logoff on live account** *(manual/config, not code)* — TWS: Edit → Global Configuration → Lock and Exit → Auto-Logoff Timer → "No logoff"; live account 24h reconnect requires phone 2FA so can't be handled in code; demo account first, revisit when going live

### Infrastructure (low priority)
- [ ] **Pre-market checklist log** — before RTH open each day, log GEX structure, gamma flip level, and key support/resistance
- [ ] **Structured logging** — JSON-lines log output for session aggregation
- [ ] **Health-check endpoint** — simple HTTP server for external monitoring
- [ ] **Containerise** — Docker + docker-compose (TWS Gateway + bot)

### Reporting (low priority)
- [ ] **End-of-day analysis** — post-session Claude review of journal
- [ ] **Weekly performance summary** — aggregate journal data, Claude-written win rate / avg R summary

---

## Key Decisions

- No UW API — screenshot-only for Periscope data
- IBKR is the sole live data source
- `derive_support_levels()` (round-number heuristic) removed — GEX+Vanna+Charm+Positions walls from Periscope are the sole source of support levels
- 1 position at a time; max $ per trade set in `config.toml`
- Pre-close sweep at 15:55 ET closes any open position regardless of P&L
- CDP `Input.dispatchMouseEvent` used for chart anchor clicks; `_JS_CLICK_SELECTOR` (JS dispatchEvent) used for date picker navigation
- `Runtime.evaluate` is blocked 30+ s after anchor clicks; screenshots work immediately via `Page.captureScreenshot`
- Anchor clicks fire-and-forget (no ACK wait) — Chrome main thread is locked during React re-render so ACK never arrives within timeout
- Only rendered `<a>` anchor labels change the chart timeframe; interpolated x-coordinate clicks and arrow keys do not work
- Periscope is SPX-denominated (~5400); SPY is ~1/10th (~540); ratio is exactly 10:1; Market Tide is SPY-based
- TICK historical not available on IBKR demo; backtest substitutes 0.0 (neutral) so other 4 conditions still evaluated
