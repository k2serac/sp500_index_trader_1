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
| GEX walls / support levels | UW Periscope screenshot → Claude Vision | 🔲 next |
| Periscope MM positioning | UW Periscope screenshot → Claude Vision | 🔲 next |

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

**Periscope data by session phase:**
- **Pre-market (before 9:30):** Initial straddle price + foundational MM net positioning benchmarks based on prior session's final open interest — captured at 9:21 ET
- **Intraday:** Live GEX walls, gamma flip level, net MM positioning — updated every 10 min

**Next steps:**
- Claude Vision reads screenshot → extracts GEX walls, gamma flip level, net MM positioning
- Extracted levels fed into `SignalEvaluator.update_support_levels()`
- Same screenshot passed to `ClaudeAnalyst.analyze(signal, periscope_screenshot_path=...)` for go/no-go
- Date selection via CDP `Runtime.evaluate` (JS click) to query historical Periscope data

---

## File Map

```
sp500_trader.py               Main loop, config loading, signal→trade orchestration
test_screenshots.py           Standalone test: open Chrome + capture all 3 UW tabs
config/config.toml            All tunable parameters
config/prompts/reversal.txt   Claude system prompt
config/context/reversal.txt   Static context injected into every Claude call
libs/
  signal_lib.py               MarketDataFeed, SignalEvaluator, ClaudeAnalyst
  trade_lib.py                TradeManager, open_uw_browser(), capture_periscope_screenshots()
  journal_lib.py              TradingJournal
  utils.py                    is_rth, minutes_since_open, in_trading_window,
                              in_periscope_window, derive_support_levels
periscope_snapshots/          Screenshot storage; snapshot_example.png is a reference image
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
- [x] ClaudeAnalyst with optional `periscope_screenshot_path` support
- [x] TradeManager (entry sizing, stop/target monitoring, pre-close sweep)
- [x] TradingJournal
- [x] Chrome launch via "UnusualWhales" Chrome bookmark folder (with CDP on port 9222)
- [x] CDP screenshot capture of all 3 UW tabs (`capture_periscope_screenshots()`)
- [x] Screenshot schedule: 9:21–16:00 ET at :X1 min marks (`in_periscope_window()`)
- [x] Helper utils extracted to `libs/utils.py`

---

## What's Next

- [ ] `PeriscopeReader`: Claude Vision function — screenshot path → list of GEX levels + gamma flip price
- [ ] Wire GEX levels into main loop (replace `derive_support_levels()` placeholder)
- [ ] Pass latest screenshot to `ClaudeAnalyst.analyze()` in main loop
- [ ] Date selection on Periscope via CDP `Runtime.evaluate` (JS click) for historical data
- [ ] Regime filter: detect trending-down day vs mean-reversion day to avoid false entries
- [ ] Pre-market checklist: identify key GEX levels before RTH open
- [ ] Backtesting framework on historical data

---

## Key Decisions

- No UW API — screenshot-only for Periscope data
- IBKR is the sole live data source
- User is the final confirmation layer for Periscope MM attribution before entry
- 1 position at a time; max $ per trade set in `config.toml`
- Pre-close sweep at 15:55 ET closes any open position regardless of P&L
