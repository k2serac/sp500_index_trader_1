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
| NYSE TICK | IBKR (`Index("$TICK", "NYSE")`) | ✅ done |
| VIX | IBKR (`Index("VIX", "CBOE")`) | ✅ done |
| GEX walls / support levels | UW Periscope screenshot → Claude Vision → `PeriscopeReader` | ✅ done |
| Periscope MM positioning | UW Periscope screenshot → Claude Vision → `PeriscopeReader` | ✅ done |

**Key constraint:** IBKR for all live market data. No UW API calls.
Periscope is screenshot-only — UW support confirmed scraping/automation violates ToS.

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
- Navigates to each date via prev/next day button clicks (`button[aria-label="Next day"]` / `button[aria-label="Previous day"]`), 1.2s per click
- Queries `<a>` anchor labels on the chart x-axis (`_JS_GET_CHART_HOUR_LINKS`) to get available hour anchors (typically 10 AM, 11 AM, 12 PM ... 4 PM — rendered by UW, not guaranteed at fixed intervals)
- Clicks each anchor in range via CDP `Input.dispatchMouseEvent` (fire-and-forget — no ACK wait; React heavy render blocks Chrome main thread 10-30s after click)
- Waits 15s after each click (data fetch + chart render), then screenshots with up to 3 retries × 5s
- Keys in result dict: `"YYYYMMDD_HHh"` → file `YYYYMMDD_HHh_periscope_market_exposure.png`

**Market-tide / Flow Overview (date navigation WIP ⚠️):**
- URL: `https://unusualwhales.com/flow/overview`
- Captures one screenshot per day (no hour anchors — date only)
- Date navigation fails: `_JS_GET_PERISCOPE_DATE` selectors don't match flow/overview DOM
- **TODO: probe flow/overview DOM to find correct date picker selectors**
- In live mode: flow/overview is reloaded and screenshotted without date navigation → works fine

### CDP Key Details

- `_cdp_evaluate(ws_url, js)` — `Runtime.evaluate` with 30s timeout; **blocked 30+ s after anchor click** (React sync render)
- `_cdp_click_xy(ws_url, x, y)` — fire-and-forget `Input.dispatchMouseEvent`; no ACK wait; TCP guarantees delivery
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
- `flow_overview` — Market Tide chart (captured but not yet parsed)

---

## PeriscopeReader & PeriscopeData

`libs/periscope_lib.py`

`PeriscopeReader.read(screenshots: dict[str, Path]) -> PeriscopeData | None`

Sends `periscope_market_exposure` (+optional `periscope_delta_flow`) to Claude Vision.
Extracts structured JSON with:
- `spx_price`, `gamma_flip`, `above_gamma_flip`
- `gex_support`, `gex_resistance` — significant GREEN/RED Gamma bars below/above price
- `vanna_support`, `vanna_resistance` — significant Vanna levels
- `charm_bias`, `positions_bias` — "bullish"/"bearish"/"neutral" / "long"/"short"/"neutral"
- `mm_bias` — overall MM bias combining all four Greeks
- `notes` — one or two sentence summary

`PeriscopeData` methods:
- `all_gex_levels()` — GEX support + resistance combined
- `all_key_levels()` — GEX + Vanna levels combined (fed to `SignalEvaluator`)
- `summary()` — human-readable string for logging and Claude context

**Precision note:** Claude Vision extracts Vanna bar centres to 10-point precision (e.g. 7300, 7320, 7340). Interpolation to finer granularity was attempted and dropped — accepted as sufficient within `support_buffer_pct=0.30%` tolerance.

---

## Main Loop Flow (`sp500_trader.py`)

```
startup:
  open_uw_browser()                  # kill old Chrome, launch fresh with CDP
  time.sleep(15)                     # let Chrome load
  select_periscope_date_all(today)   # navigate market-exposure + flow/overview to today

loop:
  if not in_periscope_window or not trading day → idle sleep until 9:20 ET next trading day

  if :X1 minute mark and new 10-min window:
    shots = capture_periscope_screenshots()   # reload all 3 tabs, screenshot
    periscope_data = PeriscopeReader.read(shots)
    latest_periscope_data = periscope_data
    latest_periscope_shots = shots
    evaluator.update_support_levels(periscope_data.all_key_levels())

  snap = MarketDataFeed.snapshot()
  trader.check_stops_and_targets(snap.spy_price)  # if position open

  if 15:55 ET → trader.close_all("pre-close sweep")

  if outside trading window or has_position → sleep, continue

  signal = SignalEvaluator.evaluate(snap)
  if signal.conditions_met >= 3:
    verdict = ClaudeAnalyst.analyze(
        signal,
        periscope_data=latest_periscope_data,
        periscope_screenshot_path=latest_periscope_shots["periscope_market_exposure"],
    )
    if verdict.go and verdict.confidence >= 6 → trader.enter(signal, verdict)
```

---

## File Map

```
sp500_trader.py               Main loop, config loading, signal→trade orchestration
test_date_picker.py           CDP test: probe date/hour anchors; --history captures today
test_screenshots.py           Dev test: open Chrome + capture all 3 UW tabs
test_periscope_reader.py      Dev test: run PeriscopeReader against saved snapshot files
config/config.toml            All tunable parameters (incl. [periscope] snapshot_dir)
config/prompts/reversal.txt   Claude system prompt for go/no-go verdict
config/context/reversal.txt   Static market context injected into every Claude call
libs/
  __init__.py                 Re-exports all public symbols
  signal_lib.py               MarketDataFeed, SignalEvaluator, ClaudeAnalyst, ReversalSignal, ClaudeVerdict
  trade_lib.py                TradeManager, open_uw_browser(), capture_periscope_screenshots(),
                              capture_periscope_historical(), select_periscope_datetime(),
                              select_periscope_date_all(), _cdp_* helpers
  periscope_lib.py            PeriscopeReader, PeriscopeData
  journal_lib.py              TradingJournal
  utils.py                    is_rth, minutes_since_open, in_trading_window, in_periscope_window
periscope_snapshots/          Screenshot storage
  YYYYMMDD_HHMM_<slug>.png    Live mode screenshots
  history_YYYYMMDD/           Historical capture output
    YYYYMMDD_HHh_periscope_market_exposure.png
    YYYYMMDD_market_tide.png  (not yet working — date selector TODO)
journal/                      Trade logs
logs/                         Bot logs
```

## Shared Library

`/home/nicu/repos/trading_common/` — always check here before reimplementing:
- `TradeHour` — market status (`"rth"/"ext"/"closed"`), trading day checks, market open/close times
- `IBapi` — IBKR ib_async wrapper (prices, orders, positions)
- `ClaudeSentiment` — headline sentiment with per-strategy prompts
- `TradingJournal`, `OpenTradeRegistry` — journal and cross-restart position tracking

---

## What's Built

- [x] IBKR connection + market data feed (SPY, TICK, VIX)
- [x] 5-condition signal evaluator
- [x] ClaudeAnalyst with `periscope_data` + `periscope_screenshot_path` support
- [x] TradeManager (entry sizing, stop/target monitoring, pre-close sweep)
- [x] TradingJournal
- [x] Chrome launch via "UnusualWhales" Chrome bookmark folder (CDP port 9222)
- [x] CDP screenshot capture of all 3 UW tabs (`capture_periscope_screenshots()`)
- [x] Screenshot schedule: 9:21–16:00 ET at :X1 min marks (`in_periscope_window()`)
- [x] Idle sleep (wakes at 9:20 ET next trading day)
- [x] PeriscopeReader: Claude Vision → PeriscopeData (GEX, Vanna, Charm, Positions, mm_bias)
- [x] GEX + Vanna levels wired into SignalEvaluator.update_support_levels()
- [x] Latest PeriscopeData + market_exposure screenshot passed to ClaudeAnalyst at go/no-go
- [x] CDP date navigation on market-exposure via prev/next day button clicks
- [x] CDP hour anchor navigation on market-exposure (fire-and-forget click + 15s wait)
- [x] Historical capture: `capture_periscope_historical(start_date, end_date, start_hour, end_hour)`
- [x] Live mode: all 3 required tabs guaranteed open; reload before screenshot
- [x] `select_periscope_date_all()` navigates both periscope tabs to today at startup
- [x] `_cdp_open_tab()` auto-opens missing tabs via Target.createTarget

---

## What's Next

- [ ] **Market Tide (flow/overview) date navigation**: probe DOM to find correct date picker selectors for `_JS_GET_PERISCOPE_DATE`; run `test_date_picker.py` and inspect what element holds the date on that page
- [ ] Regime filter: detect trending-down day vs mean-reversion day to avoid false entries
- [ ] Pre-market checklist: identify key GEX levels before RTH open
- [ ] Backtesting framework on historical data

---

## Key Decisions

- No UW API — screenshot-only for Periscope data
- IBKR is the sole live data source
- `derive_support_levels()` (round-number heuristic) removed — GEX+Vanna walls from Periscope are the sole source of support levels
- 1 position at a time; max $ per trade set in `config.toml`
- Pre-close sweep at 15:55 ET closes any open position regardless of P&L
- CDP `Input.dispatchMouseEvent` used for all clicks (React ignores JS `element.click()`)
- `Runtime.evaluate` is blocked 30+ s after anchor clicks; screenshots work immediately via `Page.captureScreenshot`
- Anchor clicks fire-and-forget (no ACK wait) — Chrome main thread is locked during React re-render so ACK never arrives within timeout
- Only rendered `<a>` anchor labels change the chart timeframe; interpolated x-coordinate clicks and arrow keys do not work
