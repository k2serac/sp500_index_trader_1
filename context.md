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

1. Chrome opens 3 UW tabs at startup via `open_uw_browser()` (reads from "UnusualWhales" Chrome bookmark folder)
2. Any existing Chrome bot-profile instance is killed before launching fresh
3. Chrome launched with CDP on port 9222: `--remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir=~/.config/chrome-sp500bot --no-first-run --no-default-browser-check --hide-crash-restore-bubble`
4. `capture_periscope_screenshots()` connects via CDP WebSocket (`127.0.0.1:9222`), sends `Page.captureScreenshot` to each tab, saves to `periscope_snapshots/YYYYMMDD_HHMM_<slug>.png`
5. Screenshots taken at :01, :11, :21, :31, :41, :51 (1 min after each Periscope 10-min update), from **9:21 ET → 16:00 ET** only
6. Retry logic: 3 attempts per tab with 5s gap if WebSocket connection fails
7. Must use `127.0.0.1` not `localhost` in WebSocket URL — `localhost` resolves to IPv6 on this machine

**Periscope data by session phase:**
- **Pre-market (before 9:30):** Initial straddle price + foundational MM net positioning benchmarks based on prior session's final open interest — captured at 9:21 ET
- **Intraday:** Live GEX walls, gamma flip level, net MM positioning — updated every 10 min

**Tab slugs** (keys in `shots` dict / PNG filename suffixes):
- `periscope_market_exposure` — Gamma, Vanna, Charm, Positions columns; gamma flip dotted line (required)
- `periscope_delta_flow` — intraday delta accumulation/distribution (optional)
- `periscope_options_flow` — third tab (captured but not yet parsed)

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

loop:
  if not in_periscope_window or not trading day → idle sleep until 9:20 ET next trading day

  if :X1 minute mark and new 10-min window:
    shots = capture_periscope_screenshots()
    periscope_data = PeriscopeReader.read(shots)
    latest_periscope_data = periscope_data      # persists between cycles
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
test_screenshots.py           Dev test: open Chrome + capture all 3 UW tabs
test_periscope_reader.py      Dev test: run PeriscopeReader against saved snapshot files
config/config.toml            All tunable parameters (incl. [periscope] snapshot_dir)
config/prompts/reversal.txt   Claude system prompt for go/no-go verdict
config/context/reversal.txt   Static market context injected into every Claude call
libs/
  __init__.py                 Re-exports all public symbols
  signal_lib.py               MarketDataFeed, SignalEvaluator, ClaudeAnalyst, ReversalSignal, ClaudeVerdict
  trade_lib.py                TradeManager, open_uw_browser(), capture_periscope_screenshots()
  periscope_lib.py            PeriscopeReader, PeriscopeData
  journal_lib.py              TradingJournal
  utils.py                    is_rth, minutes_since_open, in_trading_window, in_periscope_window
periscope_snapshots/          Screenshot storage (YYYYMMDD_HHMM_<slug>.png)
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

---

## What's Next

- [ ] Date selection on Periscope via CDP `Runtime.evaluate` (JS click) for historical data
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
