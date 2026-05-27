# TODO — SP500 Index Trader Development Backlog

## High Priority

### Signal Enrichment (Periscope layer)
- [x] **Charm + Positions as structured price levels** — added `charm_support`, `charm_resistance`, `positions_support`, `positions_resistance` arrays to `PeriscopeData`; all four Greeks now contribute to `all_key_levels()` — levels shared across GEX + Vanna + Charm + Positions = strong confluence zones
- [x] **Delta Flow parsing** — extended `_SYSTEM_PROMPT` to extract `delta_trend`, `delta_sign`, `delta_turning`, `delta_exhaustion`, `delta_notes` from the Delta Flow screenshot in the existing Claude call; fields added to `PeriscopeData` and `summary()`
- [x] **Market Tide parsing** — `flow_overview` screenshot captured but not parsed at all; extract tide direction, slope, net volume sign; reversal with tide turning green = much higher confidence

### IBKR Data Layer
- [x] **Bid/Ask spread** — add SPY bid/ask to `MarketSnapshot`; widening spread during a selloff is a stress/capitulation signal that confirms reversal candidates
- [x] **ES Futures** — add ES front-month futures contract to `MarketDataFeed` (`ContFuture("ES", "GLOBEX", "USD")`); useful for pre-market gap context and futures vs. SPY divergence at signal time

---

## Medium Priority

### Strategy & Entry Logic
- [x] **Opening gap context** — compute (today_open − prior_close) / prior_close % from daily IBKR bars; added to `MarketSnapshot.spy_opening_gap_pct` and surfaced in `ReversalSignal.summary()` so Claude can weigh catalyst-driven gap-down vs quiet support bounce differently in confidence score
- [x] **Market Tide date navigation** — DOM probe confirmed `[data-testid="date-picker-button"]` and `button[aria-label="Previous/Next day"]` exist on flow/overview; `select_periscope_datetime` works for tide tab with existing selectors

### Reliability
- [x] **Interruptible sleep** — `_sleep()` breaks all sleeps into 1-second chunks so Ctrl+C responds immediately even during the multi-hour overnight idle sleep
- [x] **Inner loop exception handler** — unhandled errors in the main loop are caught, logged with full traceback, and retried after one interval instead of crashing the bot mid-session
- [x] **TWS session timeout detection** — `ensure_ibkr_connected()` in `trading_common` calls `reqCurrentTime()` as a heartbeat each loop; catches stale sessions that `isConnected()` alone misses; used in both sp500 and us_stock_trader_1
- [ ] **Disable TWS auto-logoff on live account** *(manual/config, not code)* — TWS: Edit → Global Configuration → Lock and Exit → Auto-Logoff Timer → "No logoff"; live account 24h reconnect requires phone 2FA so can't be handled in code; demo account first, revisit when going live

---

## Low Priority / Future Ideas

### Backtesting
- [x] **Backtesting framework** — `backtest.py`: runs `PeriscopeReader` on each `history_YYYYMMDD/` dir, pulls SPY/VIX (5-min) + TICK (1-min) from IBKR, walks bars through `SignalEvaluator`, exits at nearest multi-Greek resistance level (or 0.50% fallback) and stop at nearest support; prints per-trade P&L table + summary stats

### Infrastructure
- [ ] **Pre-market checklist log** — before RTH open each day, log the GEX structure, gamma flip level, and key support/resistance so there is a reference for the session; currently the first Periscope capture is at 9:21 ET but it is not summarised separately
- [ ] **Structured logging** — add JSON-lines log output so sessions can be aggregated and searched; currently plain text only
- [ ] **Health-check endpoint** — simple HTTP server so the bot can be monitored externally without SSHing in
- [ ] **Containerise** — Docker + docker-compose (TWS Gateway + bot) for reproducible deployment

### Reporting
- [ ] **End-of-day analysis** — post-session Claude review of the journal: did signal conditions fire on valid setups, where was the entry vs. optimal, what was P&L vs. the move available; modelled on `us_stock_trader_1` end-of-day analyzer
- [ ] **Weekly performance summary** — aggregate journal data across sessions and generate a Claude-written summary of win rate, average R, best/worst setups
