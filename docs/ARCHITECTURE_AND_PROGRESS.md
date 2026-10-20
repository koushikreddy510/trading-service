# Trading app: how the four repos fit together, what has been built, and where to look

Purpose of this file: so nobody (human or AI) has to read the whole code base again. Read this, then open only the file
the table in section 9 points to. Last updated 2026-10-19. Nothing here has been committed to git yet.

## 1. The four repos (all under `Desktop/`)

| Repo | What it is | Owns |
|---|---|---|
| `data-store` | Postgres market data + scrapers | `symbols`, `ohlcv_1d` (Fyers daily job), `stock_financials` (screener.in quarterly/annual), `result_announcements` (NSE) |
| `strategy-screener` | FastAPI (port 8000) + React. The backend for the **strategy screens**: filters every stock by indicator conditions on 1D candles | `strategies`, `strategy_conditions`; `/run/{id}` |
| `trading-service` | FastAPI (port 8001 in dev). Broker adapters, execution strategies, orders, **and the new `terminal` package** | `trading_*` tables, `terminal_*` tables |
| `trade` | Go "Atlas" engine (MCX silver) + `web/` (Vite/React). `web/src/workspace` is the new terminal UI; the old Atlas screen is at `?view=atlas` | UI |

```
 data-store ──ohlcv_1d, stock_financials, symbols──┬──▶ strategy-screener  ──GET /strategies, /run/{id}──┐
 (daily jobs)                                      │                                                     ▼
                                                   └──▶ trading-service/terminal ◀── screener_sync (HTTP, STRATEGY_SCREENER_URL)
                                                          ▲        │ REST + websocket /terminal
                         Flattrade websocket ticks ───────┘        ▼
                                                              trade/web  Workspace (React)
```

Convention that makes the joins work: a symbol is **`EXCHANGE:TRADINGSYMBOL`**, e.g. `NSE:SBIN-EQ`, in every repo. There is
no mapping layer.

## 2. What exists now

### Terminal (trading-service/`trading_service/terminal`, mounted at `/terminal` and `/terminal/ws`)

Chart data (1m–1W, IST session clock), live prices over websocket, watchlists, drawings (trendlines / rays / levels), entry
rules, signals, paper positions, financials, a daily-candle screener, and the strategy-screener → watchlist sync. Paper only:
no code path places a broker order. Entries come only from **completed** candles; stop-loss, target and entries are drawn on the chart.

Module map:

| Module | Job |
|---|---|
| `api.py` | All routes (table in section 5) and the websocket; `mount(app)` and `terminal_lifespan` are what `main.py` calls |
| `runtime.py` | `Terminal`: wires feed → candle builder → hub / paper book, the 1-second flush loop, gap repair, demo vs flattrade mode |
| `feed/` | `base.py` (Feed/History protocols), `flattrade.py` (REST + websocket + token resolver), `mock.py` (synthetic market for demo/tests) |
| `builder.py` | Ticks → forming 1m bar; the clock-driven close events |
| `market.py` | `CandleService`: stored 1m bars, resampling to every timeframe, daily + weekly, `backfill`, `repair` |
| `candles.py`, `timeframes.py` | Candle type, resample, bucket maths on the NSE/MCX clock |
| `indicators.py`, `conditions.py`, `rules.py`, `engine.py` | Indicator maths (numpy), the rule DSL (`cross`/`compare`/`all`/`any`; stops fixed/pct/atr/candle/line; targets rr/pct/fixed), rule storage, evaluation on completed bars |
| `paper.py` | Paper positions: open on a signal, close on stop/target tick or manually |
| `drawings.py` | Saved lines per symbol (time, price points) |
| `hub.py` | Websocket fan-out, ref-counted subscriptions, tick conflation |
| `watchlists.py` | Watchlist repo (manual lists) |
| `screener.py` | The terminal's own once-a-day scan over `ohlcv_1d` (22 metrics, 5 presets) + instant SQL screens |
| **`screener_sync.py`** | **New:** strategy-screener strategy → managed watchlist, with near-miss tolerance and optional financial gates |
| `financials.py`, `symbols.py` | Read-only views of the data-store tables (graceful when absent) |
| `db.py`, `schema.sql` | Plain-SQL layer; idempotent schema, created on start |
| `__main__.py` | CLI: `scan`, `schema`, `sync-screener` |
| `README.md` | Terminal-level detail (data flow, env vars, test map) |

### Web (trade/`web/src/workspace`)

`Workspace.tsx` (shell) · `store/` (`useSyncExternalStore` store + `Controller` class that owns every action) · `api/`
(typed REST client, resilient `LiveSocket`) · `chart/` (imperative lightweight-charts wrapper + plain-data overlay builders) ·
`lib/` (TS twins of the Python maths, checked against `golden.json`) · `components/` (thin: read the store, call the
controller) · `views/` (`ScreenerView`, **`StrategiesView`**, `ResultsView`). Tabs: Chart · Screener · **Strategies** · Results.

## 3. Strategy-screener → watchlist (the newest piece)

**Question it answers:** "run our strategy screener and keep every stock that qualifies, plus the ones that are nearly there."

**Where the watchlist content comes from, in one table**

| Watchlist | Source |
|---|---|
| "Watchlist" (the default) | Manual. Created empty on first use (demo mode seeds SBIN, TCS, INFY, HDFCBANK, RELIANCE once). You add/remove symbols. |
| "Screen: <strategy>" | The sync (below). Matched stocks first, then near-misses, closest first. |
| Execution-strategy universes in `trading_service/universe_resolver.py` (`screener_filtered`, `pattern_52w`, `union_screener_screen`, …) | A **separate** mechanism: it calls the same screener over HTTP for the order-placing pipeline (`strategy_consumer.run_entry_candidate_scan`). It does not touch terminal watchlists. |
| strategy-screener itself | Has no watchlist concept yet (its README lists "watchlist and approval actions" as Phase 2). The terminal now provides one. |

**How the sync works** (`screener_sync.py`, no change to the strategy-screener repo)

1. `GET /strategies/{id}` → the conditions (`indicator_type`, `params`, `operator`, `threshold`).
2. `GET /run/{id}?matched_only=false&page_size=50`, paged → every scanned stock with its latest indicator values and a `matched` flag.
3. For a stock the screener did **not** match, measure each failing condition:
   `gap % = |threshold − value| ÷ |threshold| × 100`. A stock is a **near-miss** when at most `max_failing` conditions
   fail (default 1) and each is within `tolerance_pct` (default 3). Example: RSI(14) 58.8 against `> 60` is a 2 % gap.
4. Optional financial gates (see below) drop stocks whose latest quarter / year / ratios fail; a stock with no data is kept by default.
5. Cap at `max_symbols` (default 100, matched stocks keep their places first), write the managed watchlist.

How the measuring treats each operator (`measure()`; read `TestMeasure` in the tests, it is the specification):

* `>`, `>=`, `<`, `<=`: exact distance. A threshold of 0 uses the indicator's own scale: Bollinger %B is a fraction of the band, RSI/stochastic are 0–100 points, MACD/ATR use the close.
* `cross_above` / `cross_below`: `/run` does not return the previous candle, so a cross can only be judged from the level. Below the line (for `cross_above`) counts as "about to cross"; already past it counts as failed.
* `==` (supertrend / parabolic SAR direction): holds or not; no distance, so never a near-miss.
* A value that is missing, or a condition that cannot be measured, is never "near".

**Provenance and safety rules** (tables `terminal_screener_links`, `terminal_screener_members`; tests in `TestApply`)

* One managed list per strategy, named `Screen: <strategy name>` (or your own name). A name already used by an ordinary list is refused, never overwritten.
* Each member records why it is there (`matched`/`near`, gap %, reason text).
* A later sync **adds** new qualifiers, **removes** ex-qualifiers, **re-labels** a near-miss that became a match, and **re-orders** (matches first).
* Symbols you added by hand are never removed, including one you had before the screen also picked it.
* A symbol with an **open paper position or an enabled rule** is never removed even if it stops qualifying (reported as `kept_for_trading`).
* Unlinking keeps the list and stops updating it. Deleting the list removes its link.

**Financial gates** (the "quarterly / yearly results" idea, first version). A filter is
`{source: ratio | quarter | year, metric, op: > >= < <=, value, on_missing: keep | drop}`. `quarter`/`year` read the *latest* row
of `stock_financials` (metrics: revenue, net_profit, opm_pct, npm_pct, eps, revenue_growth_pct, profit_growth_pct …);
`ratio` reads the newest non-null of pe_ratio, pb_ratio, roce_pct, roe_pct, dividend_yield, market_cap_cr. Not yet there:
multi-period rules such as "profit grew four quarters in a row" or "3-year CAGR above 15" (the strategy-screener has
`get_cagr_for_symbols` if you want to reuse it).

**Ways to run it**

| How | Command / place |
|---|---|
| UI | Strategies tab → pick strategy, tolerance, allowed misses, optional financial filters → **Preview** (writes nothing) → **Sync to watchlist**. "Refresh all" and "Unlink" per list. |
| CLI, one strategy | `python -m trading_service.terminal sync-screener --strategy 7 --tolerance 3 --max-failing 1 --max-symbols 100 [--name "Swing"] [--filter '{"source":"ratio","metric":"roce_pct","op":">=","value":15}']` |
| CLI, everything already linked | `python -m trading_service.terminal sync-screener --all` (exit code 1 if any list failed) |
| Schedule | After the data-store's 1D job: `terminal scan`, then `sync-screener --all`. Weekdays after market close (e.g. 19:00 IST scan, 19:15 sync). |

**Known limits of the sync**

* `/run` recomputes financial enrichment for every page when it has ≥ 10 rows, so reading ~2,000 unmatched stocks 50 at a time is slower than it needs to be. The screener caches the run for 300 s. A lean `GET /run/{id}/candidates` endpoint in the strategy-screener (no financials, no paging) would fix it; the client would only need a different path.
* The screener's `matched` flag uses the latest candle only (`scan_days` is not used), so a stock that matched "within the last N days" is not a match here.
* Data is 1D, so the list is as fresh as the data-store's daily job.

## 4. Live data in one paragraph

Broker websocket → `on_tick` → in-memory forming 1m bar + straight to browsers (conflated to ≈4/s per symbol). Nothing is written to
Postgres per tick; a finished minute is written once per symbol by a 1-second timer, then rules run on completed bars. Gaps are
repaired from the broker's 1-minute history on reconnect; the browser re-reads state when its own socket reconnects. Full
explanation, the honest limits, and the Redis design for many symbols (not built): [LIVE_DATA.md](LIVE_DATA.md).

## 5. API surface (`/terminal`)

| Area | Routes |
|---|---|
| Meta | `GET /status`, `/indicators`, `/symbols/search?q=` |
| Candles | `GET /candles?symbol=&timeframe=&limit=` (backfills first if due; `forming` flag) |
| Watchlists | `GET/POST /watchlists` · `PATCH/DELETE /watchlists/{id}` · `POST /watchlists/{id}/symbols` · `DELETE …/symbols/{symbol}` · `PUT …/order` |
| Drawings | `GET/POST /drawings` · `PUT/DELETE /drawings/{id}` (delete → 409 if a rule uses the line) |
| Rules | `GET/POST /rules` · `PUT/DELETE /rules/{id}` · `PATCH /rules/{id}/enabled` · `GET /signals` |
| Positions | `GET /positions` · `POST /positions/{id}/close` |
| Financials | `GET /financials?symbol=` · `GET /results/recent` |
| Daily screener | `GET /screener/columns`, `/screener/presets`, `/screener/preset/{name}` · `POST /screener/run`, `/screener/scan` |
| **Strategy sync** | `GET /screener-sync/strategies`, `/screener-sync/links` · `POST /screener-sync/preview`, `/apply`, `/refresh` · `DELETE /screener-sync/links/{watchlist_id}` (screener down → 502, bad input → 400) |
| Live | websocket `/terminal/ws`: send `subscribe` / `unsubscribe` / `ping`; receive `hello` `tick` `bar_closed` `signal` `position` `status` `error` `pong` |

## 6. Database

Terminal-owned tables (created by `schema.sql`, safe to re-run): `terminal_watchlists`, `terminal_watchlist_items`,
`terminal_drawings`, `terminal_rules`, `terminal_signals` (unique `(rule_id, bar_time)`), `terminal_paper_positions`,
`terminal_ohlcv_1m`, `terminal_broker_tokens`, `terminal_screener_metrics`, **`terminal_screener_links`**, **`terminal_screener_members`**.
Read-only, from the data-store (optional; every reader degrades to "no data"): `symbols`, `ohlcv_1d`, `stock_financials`, `result_announcements`.

## 7. Configuration and running

| Variable | Used for |
|---|---|
| `DATABASE_URL` | Terminal starts only when set (otherwise `/terminal/*` answers 503 and the rest of the service runs) |
| `TERMINAL_MODE` | `demo` (default; synthetic market, no broker) or `flattrade` |
| `TERMINAL_DEMO_SPEED` | Demo clock speed-up, default 20 |
| `FLATTRADE_USER_ID`, `FLATTRADE_TOKEN`, optional `FLATTRADE_ACCOUNT_ID` | Live prices and history |
| `STRATEGY_SCREENER_URL` | Where `screener_sync` finds the strategy-screener, default `http://127.0.0.1:8000` |

```bash
# backend (demo)         cd trading-service && TERMINAL_MODE=demo DATABASE_URL=postgresql+psycopg2://… uvicorn main:app --port 8001
# web                    cd trade/web && npm install && npm run dev        # /terminal is proxied to :8001 (vite.config.ts)
# daily jobs             python -m trading_service.terminal scan ; python -m trading_service.terminal sync-screener --all
# schema only            python -m trading_service.terminal schema
```

## 8. Tests (they are the specification; names are sentences)

| Suite | Count (2026-10-19) | Run |
|---|---|---|
| `trading-service/tests/terminal` | 438 passing | `pip install -r requirements.txt -r requirements-dev.txt && pytest tests/terminal` — needs Postgres: set `TEST_DATABASE_URL`, or have `initdb`/`pg_ctl` installed (a throwaway cluster is started). Each test gets its own schema. |
| `trade/web` | 303 passing, `tsc` clean | `npm test` |

Read first: `test_engine.py` (rules fire on completed candles only), `test_runtime.py` (tick → bar → signal → position → stop), `test_screener_sync.py`
(near-miss rules, list provenance, API, CLI), `test_reconnect.py` (gap repair), `StrategiesView.test.tsx`, `controller.test.ts`.
Not covered by automated tests: the real Flattrade endpoints (the protocol is tested against fakes that follow its message shapes), the real strategy-screener process
(its API is faked with the same paths and paging), and canvas rendering (checked once by hand in a browser against the demo backend).
After changing indicator maths or timeframes, regenerate the shared vectors: `python scripts/make_golden_fixtures.py --also ../trade/web/src/workspace/lib/golden.json` (run from `trading-service`).

## 9. Where to look for…

| Question | Open |
|---|---|
| Why did a rule fire / not fire? | `engine.py`, `conditions.py`; `test_engine.py` |
| Where does a price come from? Where is the DB written? | [LIVE_DATA.md](LIVE_DATA.md); `runtime.py`, `builder.py` |
| How is a timeframe built? | `market.py`, `candles.py`, `timeframes.py` |
| How is a stock a "near-miss"? | `screener_sync.py` (`measure`, `classify`); `test_screener_sync.py::TestMeasure/TestClassify` |
| How does the UI react to a message? | `web/src/workspace/store/controller.ts` (`onMessage`) |
| What does the chart draw for SL / entries? | `web/src/workspace/chart/overlays.ts` |
| Which strategies / conditions exist? | strategy-screener `strategies.sql`, `scripts/seed_trend_strategies.py`, `/strategies/` |
| How do execution strategies pick symbols? | `trading_service/universe_resolver.py`, `strategy_consumer.py` |

## 10. Decisions already made (do not re-ask)

Frontend lives in `trade/web` · chart entries are **paper signals** only · Flattrade is the broker (Paytm is a stub) · entries only from completed candles ·
SL/entries visible on the chart · live prices over websockets · only closed 1m bars are stored · the daily screener runs once a day, not realtime ·
the strategy-screener repo is read, not modified.

## 11. Open items / next steps (none started)

1. Replay rule evaluation for bars that closed during a feed outage (needs a stale-entry policy; see LIVE_DATA §3a).
2. Lean `/run/{id}/candidates` in the strategy-screener, then point `fetch_entries` at it.
3. Financial rules over several periods (growth streaks, CAGR).
4. Schedule the two daily jobs (cron or the existing scheduler), and add a per-list "sync on schedule" flag if wanted.
5. Batch the minute-close inserts into one transaction per flush (needed before ~1,000+ symbols).
6. Bus (Redis Pub/Sub + Streams) when more than one API worker or zero-downtime deploys are needed; design in LIVE_DATA §6.
7. Auth: the terminal API has none yet (`user_id` is the constant `local`); bind it to a trusted network until it does.
8. Broker orders from signals (explicitly out of scope so far).

## 12. Fundamentals on the chart (added Oct 2026)

`GET /terminal/financials` now also returns two lists for the chart overlays (`terminal/financials.py::fundamentals`):

* `events` - one per result announcement on file: date, quarter, profit/revenue growth and `reaction_pct`
  (next session's close vs the close on result day). Shown as a square marker on the candle ("Results" toggle).
* `eps_ttm` - trailing-twelve-month EPS as a step series, effective the day AFTER the result (announcement date if
  known, else the SEBI deadline: 45 days, 60 for March; flagged `estimated`). Drives the "P/E" pane (price / TTM EPS
  as known at the time). Four consecutive quarters with EPS are required.

Limits: markers exist only for announcements in `result_announcements` (it fills up from when the scraper started, so
history is sparse until NSE's results-history is scraped - open item); screener.in quarterly EPS is as reported there.
Frontend: `web/src/workspace/chart/fundamentals.ts` (+ tests), toggles in `ChartToolbar`, state `showResults`/`showPe`.
Tests: `tests/terminal/test_fundamentals.py` is the best description of the rules.

## 13. Trailing stops and exit conditions

A rule may carry an **exit plan** next to its entry condition and stop/target:

| Field | Meaning |
|---|---|
| `trail` | `candle` (last N bars' low/high ± buffer), `atr` (mult × ATR from the close), `pct`, `indicator` (follow e.g. an EMA) or `line` (follow a drawn line). Optional `timeframe`; default is the rule's. |
| `exit_condition` | Same condition tree as the entry. When it becomes true on a completed candle the position closes at that candle's close (reason `exit_rule`). |
| `exit_timeframe` | Candle size the exit condition is read on; default the rule's. |

Behaviour worth knowing (all pinned by `tests/terminal/test_exits.py`):

* The plan is **copied onto the position at entry** (with timeframes resolved), so editing, disabling or deleting the rule never changes a trade that is running. This is also why a position keeps following the strategy it was opened under while the chart is viewed on another timeframe.
* Both are evaluated only on **completed candles of their own timeframe** (a 1H trail on a position opened from a 15m rule moves once an hour). The candle in which the position opened is skipped.
* A trail only **tightens** (`PaperBook.move_stop`); a level on the wrong side of the last close is ignored rather than triggering an instant stop-out. `initial_stop` keeps where the stop began.
* On the same candle the **exit condition wins** over the trail.
* Drawn lines used by a trail or exit condition are protected from deletion, like entry lines.

UI: the rule dialog's "After entry" section; the chart shows `Trail SL` (and a faint `Initial SL`) once the stop has moved, plus exit arrows (`SL`, `TP`, `Rule exit`, `Exit`) on closed trades; the positions table marks trailing stops with ↗ and explains them on hover.

## 14. Chart look, legend and drawing tools

* **Theme.** The workspace uses TradingView's dark palette (canvas `#131722`, panels `#1e222d`, lines `#2a2e39`, teal/red candles). Chart canvas colours live in one `THEME` constant in `chart/overlays.ts`; page colours are CSS variables at the top of `styles.css`.
* **Legend.** OHLC, change, volume and every indicator's value for the candle under the crosshair, top-left of the chart (newest candle when the pointer is elsewhere). Pure logic in `chart/legend.ts`, tested in `legend.test.ts`.
* **Tool rail.** Drawing tools are a vertical strip at the chart's left edge (`DrawingRail`): cursor, trend line, ray, level, **zone** (rectangle) and **text**.
* **Zones.** A rectangle is two flat levels. A rule, stop or trailing stop may read its `top` or `bottom` edge (`{"kind":"line","drawing_id":…,"edge":"top"}`); the API rejects a zone reference without an edge and any reference to a text note (which has no price). Breakout rules default to the top edge, breakdowns to the bottom. The chart extends both edges to the right with dashes, showing what a rule reads.
* **Text notes** start labelled "Note"; rename them in the Drawings tab.
* **Schema.** `terminal_drawings.kind` now allows `rectangle` and `text`; existing databases are migrated on start-up (the old inline CHECK is dropped and re-added).

## 15. Fyers-style triggers (range operands, time window, square-off)
Built from the Fyers strategy-builder screenshots. Everything is evaluated on completed candles only, like all other rules.
* **Range operands.** `{"kind":"highest"|"lowest","bars":N,"source":"high|low|close|open|volume"}` (N = 1-500, default 20; highest defaults to `high`, lowest to `low`). It looks at the **previous** N candles, so the current candle can break out of it ("close crosses above 20-candle high"). The first N candles have no value.
* **Time window.** `{"type":"time_between","start":"09:20","end":"15:00"}` is true when the candle's **closing** minute (IST, from `bucket_end(last candle, timeframe, exchange)`) lies in the inclusive window. Use it inside an `all` group to trade only inside a session. Midnight-crossing windows are rejected (start must not be after end).
* **Square-off.** A time window used as the exit condition is the intraday square-off: e.g. exit when the candle closes between 15:15 and 15:30. MCX candles are measured against MCX session buckets (`exchange_of(symbol)` is passed to `EvalContext`).
* **UI.** Rule dialog: operand type "Highest/Lowest of last N candles"; condition type "Time window" (two time inputs, default 09:20-15:00).
* **Not built.** Options-specific triggers, webhooks, order-book/portfolio checks from the Fyers screens.
* **Tests.** `tests/terminal/test_fyers_triggers.py` (28), plus `rules.test.ts` and `RuleDialog.test.tsx`.

## 16. Sectors and themes
* **Data.** `symbols.sector` holds about 185 NSE industries; `sector_groups.py` (copied from strategy-screener, see its docstring) folds them into about 25 groups (Banking & Finance, IT & Technology, ...).
* **API.** `GET /sectors` lists groups and their industries with counts. `GET /sectors/compare?group=…|sector=…&limit=60` returns every member (biggest first) with market cap, P/E, P/B, ROE, ROCE, growth, returns and RSI, a **rank within the peers** for each column and the group median. Ties share a rank; P/E and P/B must be positive to be ranked (loss-makers stay unranked); the table shows the `limit` largest but `symbols` carries up to 500 for the watchlist.
* **Sources.** Fundamentals = newest non-null ratio per symbol; growth = latest quarter; prices = latest `terminal_screener_metrics` run (run the screener scan once).
* **UI.** Top bar "Sectors": group list, industry chips, comparison table (best third green, worst third red, sortable columns, median row). "Make watchlist" creates a watchlist of the whole group/industry and opens it; click a symbol to open its chart.
* **Tests.** `tests/terminal/test_sectors.py` (27), `SectorsView.test.tsx` (9), controller test for pre-filled watchlists.

## 17. When things are evaluated (entries, stops, trails, exits)
| What | When |
|---|---|
| Entry rule | on the **close** of a candle of the rule's own timeframe; never on the forming candle (no repainting) |
| Hard stop / target | on **every live tick**; closes at the tick price, so a gap through the stop fills at the gap price |
| Trailing stop | on the close of a candle of the **trail's own timeframe**; only ever tightens; applies from the next candle |
| Exit condition | on the close of the **exit's own timeframe**; wins over the trail; the entry candle is skipped |
Entries are deliberately not evaluated tick by tick: a price that triggers mid-candle can be taken back by the close, and a backtest cannot reproduce it. To react sooner, put the rule on a shorter timeframe (5m/15m instead of 1H).

## 18. Backtesting (`backtest.py`, `backtest_jobs.py`, `/terminal/backtests`)
* **Same engine as live.** The replay walks every candle close in time order and calls `RuleEngine.on_candle_close` with a clock set to that close, inside a throw-away Postgres schema (`bt_<id>`, a private copy of the terminal tables, dropped afterwards; the real tables are never written). Entry, trail and exit logic are therefore identical to live. The only new hook in the engine is an optional `sizer` (live uses the rule's own quantity).
* **Stops/targets** (live: tick by tick) are found by sweeping the 1-minute bars (intraday rules) or the daily bars between candle closes. A candle that touches both stop and target takes the **stop**; a gap through a stop fills at the open; the entry candle cannot stop itself out.
* **Money.** `capital`, `allocation_pct` of current equity per trade (limited by cash), optional `max_positions`, optional `cost_pct` per side. Positions still open at the end stay open and are marked at the last close (no square-off).
* **Which stocks.** exactly one of `symbols`, `watchlist_id`, or `screen` (`{"preset": name}` or `{"filters": [...]}`, optional `limit`, optional `base` list to screen within) re-run every `rescreen_days` using only data up to the previous day. A stock that leaves the screen gets no new entries but keeps managing its open position. Rules are templates: given inline (`rules`) or by saved-rule id (`rule_ids`); a template that reads a drawn line only runs on its own symbol.
* **Rules apply per stock**: each template is copied per symbol, one open position per template per symbol.
* **Run it.** `POST /terminal/backtests` returns an id at once (a background thread does the work; at most 2 at a time); poll `GET /terminal/backtests/{id}` (`status` running/done/failed, `progress` = current date). `GET /terminal/backtests` lists runs (summary only), `DELETE` removes one. Reports hold summary, daily equity curve, trades, open positions, skipped-signal reasons, universe changes and notes. A restart marks interrupted runs failed.
* **UI.** Top bar "Backtests": pick saved rules, stocks (watchlist / screener preset / symbols), dates, capital and % per trade.
* **Data limits.** Daily candles come from `ohlcv_1d` (Oct 2025 onward if the data-store has it). Intraday needs `terminal_ohlcv_1m`, which only holds what the live feed has stored or backfilled (<= 120 days), so a year-long run should use 1D/1W rules. Screens use price metrics as of the previous day; market-cap/sector filters use today's values (look-ahead).
* **Speed.** About 40 s for 40 stocks x 1 year on daily candles; an hourly rule on 30 stocks x 1 year is roughly 3-4 minutes.
* **Not built.** Short-selling costs/margin, slippage, dividends/splits adjustment (uses the stored prices as they are), portfolio-level rules beyond `max_positions`.
* **Accounts.** `account` is stored on each backtest and passed to Slack; nothing else reads it yet. Per-account rules, positions and brokers are a later change (add an `account` column to rules/positions and a Notifier per account).

## 19. Slack (`notify.py`, `scheduler/main.py`)
* Set `SLACK_WEBHOOK_URL` (or `TRADING_SLACK_WEBHOOK_URL`) for the trading-service **and** the scheduler container. `SLACK_WEBHOOK_URL_<ACCOUNT>` overrides it for that account. No webhook = silent no-op; a Slack outage never affects trading (messages go through a background worker).
* **Trades:** every paper entry (side, quantity, price, stop, target, timeframe, why) and exit (reason, entry -> exit, P&L, %), whichever path closed it (candle close, tick on a stop/target, manual close). Skipped signals are reported too. Trail moves are not. Demo runs are tagged `[demo]`.
* **Backtests:** a summary when one finishes or fails. **Screener sync:** what was added/removed from the managed watchlist (on apply and on the daily refresh).
* **Scheduled jobs:** the scheduler posts every job's result (what the endpoint returned, duration) or failure, a "did not run at its scheduled time" notice for missed runs, and "scheduler started". `SLACK_JOBS_NOTIFY=all|failures|off` (default all).
* **Daily data digest** (`POST /terminal/notify/data-digest`, scheduled 21:00 IST weekdays): symbols added/removed since the previous digest (the first run only records the list), latest daily-candle date and coverage, financials and result-announcement freshness, last screener scan.
* **New scheduled jobs** in `scheduler/schedules.yaml`: screener scan 16:15, linked-watchlist sync 16:30, data digest 21:00 (IST, weekdays). Adjust to follow your OHLCV job.

## 20. Backtest trades on the chart, and the black theme

- **Show on chart.** In Backtests, the symbol in the "Trades" and "Still open" tables is a link. It opens that stock's chart with the
  backtest drawn on it, next to whatever indicators are on: an up/down arrow at each entry (`BUY 100 @ 800.00`), an exit arrow coloured by
  the result (`TP 890.00 +9,000.00`, `SL …`, `Rule exit …`), and a dashed entry→exit line per trade. A banner on the chart shows the run,
  the number of trades and net P&L for that stock, steps through trades with ‹ ›, and has Hide.
- Events are placed on the candle they belong to: entries are stamped at their signal candle's close, so they sit on that candle. The chart
  asks for up to 5000 candles while a backtest is shown. A trade older than the loaded candles is simply not drawn (switch to a larger timeframe).
- State is `btOverlay` in the store (`showBacktest` / `focusBacktest` / `hideBacktest` on the controller); drawing logic is
  `backtestMarkers` / `backtestSegments` in `chart/overlays.ts`.
- **Bug fixed:** exit arrows of already-closed live positions were never drawn (the chart only received open positions).
- **Theme:** pure black canvas (`#000`), neutral greys, white accent for active tabs/chips/buttons; teal/red candles unchanged. No blue left.

## 21. Theme settings and the Data screen

- **Theme:** the chart's TradingView logo is off (a "Charts by TradingView" text link in the top bar keeps the library's attribution). **Theme** in the top bar
  has presets (Black default, Midnight, Light) and a colour picker for each of eight colours; saved in the browser (`workspace.prefs.v1`).
- **Data screen** (top bar → Data): data health (stocks, how many have a sector / market cap, newest 1D candle and how many stocks have it, financials
  coverage and last scrape, results announced), a "needs attention" list, the scrape jobs with Run buttons and live progress, and the symbol list changes
  (added/removed vs the last digest). Backend: `terminal/datajobs.py`, `GET /terminal/data/status`, `POST /terminal/data/run`.
- The jobs are the ones the strategy-screener already has (`/admin/sync-symbols`, `update-ohlcv`, `update-financials`, `scrape-latest-results`,
  `scrape-screener-latest`, `enrich-sectors`); the terminal only starts them and reads `/admin/jobs`. Nothing is scraped twice or re-implemented.
  Health numbers come straight from the market tables, so they show even when the screener service is down.
- **To get real data in:** start the stack (`run_local_stack.sh start`), open Data and run, in this order: Sync symbol master → Sectors and market cap →
  Daily candles (needs a valid Fyers token in the strategy-screener) → Latest results (NSE) → Financials with mode `announcements_only`, then `incremental`/`full`.

## 22. Financial screens and more chart indicators

**Screens.** The daily scan (`run_scan`) now also computes ~25 fundamental columns per symbol from `stock_financials` (`terminal/fundamentals.py`) and stores them in `terminal_screener_metrics` next to the technical ones, so one screen can mix both (`pe_ratio < 25 AND rsi_14 > 50`). There are 19 presets in three groups (Price and volume, Value and quality, Quarterly results): e.g. Low PE + high ROCE, Magic-formula approximation, Growth at a fair price (PEG), Quarterly profit turnaround, Profit growth streak, Margin expansion, Interest burden falling, Golden cross, Volume surge. "Save as watchlist" on the Screener turns any result into a watchlist.

**What the data cannot do.** There is no balance sheet, cash flow or shareholding data, so Piotroski, FCF yield, FII/DII buying and true debt screens are not possible. Magic formula uses earnings yield = 100/PE; "debt reduction" is proxied by interest burden. Banks/NBFCs are excluded from the magic-formula rank. Adding balance sheet / shareholding scrapers unlocks those.

**Backtests.** A fundamental screen inside a backtest uses *today's* fundamentals for every historic date (look-ahead). The backtest report says so.

**Indicators.** Added Stochastic, CCI, Williams %R, ADX (+DI/-DI), Donchian, Keltner, ROC and Parabolic SAR. Python (`indicators.py`) and TypeScript (`lib/indicators.ts`) are kept identical by the golden vectors (`scripts/make_golden_fixtures.py` -> `golden.json` on both sides). Skipped: VWAP, OBV, MFI and Ichimoku, because `compute()` only receives high/low/close.

**Unit note.** `symbols.market_cap` (data-store) is in rupees; the screener divides by 1e7 so the Market cap column and filters are in crore.
