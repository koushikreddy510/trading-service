# Terminal backend (`/terminal`)

> Start with [`docs/ARCHITECTURE_AND_PROGRESS.md`](../../docs/ARCHITECTURE_AND_PROGRESS.md) (the whole picture, file map, what is done / open) and
> [`docs/LIVE_DATA.md`](../../docs/LIVE_DATA.md) (live prices, persistence, reconnects, scaling). This file is the module-level detail.

The server side of the **Workspace** screens in `trade/web` (chart, watchlists, screener, rules,
live paper positions). It lives inside `trading-service`, mounted at `/terminal` (REST) and
`/terminal/ws` (websocket).

## How the data flows

```
Flattrade websocket ──tick──▶ Terminal.on_tick ─┬─▶ CandleBuilder   forming 1m bar, in memory
 (or demo feed)                                 ├─▶ Hub             latest price to every browser (≈4/s per symbol)
                                                └─▶ PaperBook       was a stop / target touched?

1-second timer ─▶ Terminal.flush ─▶ closed 1m bars saved ─▶ bar-close events ─▶ RuleEngine ─▶ signals + paper positions
                                                                              └▶ browsers watching that symbol
```

### What is stored, and what is not (the "do we need 1s data?" question)

| Data | Stored? | Why |
|---|---|---|
| 1-second ticks | **No**, streamed only | They move the *forming* candle and the price shown on screen. Storing them would be huge and nothing needs them later. |
| 1-minute candles | **Yes**, closed bars only (`terminal_ohlcv_1m`) | The base for every intraday timeframe. |
| 5m / 15m / 30m / 1H / 2H / 4H | No — built from 1m when asked | One source of truth, no drift between timeframes. |
| 1D | data-store's `ohlcv_1d`, plus today's live bar built from 1m | Your existing daily job stays the owner. |
| 1W | built from 1D | |

Bars are cut on the exchange clock (NSE 09:15–15:30 IST, MCX 09:00–23:30 IST), so an hourly bar
starts at :15 for NSE.

### Entries only from completed candles

A rule is evaluated **once, when a candle closes**, and only against completed candles
(`completed()` drops the forming bar). A unique constraint on `(rule_id, bar_time)` means a bar can
fire a rule at most once even if the close event is delivered twice, and a rule never stacks
positions. See `tests/terminal/test_engine.py` first — it is the best description of the behaviour.

### The screener is a once-a-day job, not realtime

`screener.run_scan` reads every symbol's 1D candles, computes 22 metrics (EMA/SMA, RSI, ATR, 52-week
distance, returns, volume ratio, Supertrend, …) and stores them in `terminal_screener_metrics`.
Screens (filters + sort) are then plain SQL over that table, so they are instant. Run the scan
after the data-store's daily OHLCV job:

```bash
python -m trading_service.terminal scan          # e.g. cron: weekdays 19:00 IST
```

There is also a "Run daily scan" button in the UI for on-demand runs.

### Strategy screener → watchlist

`screener_sync.py` turns a strategy from the separate `strategy-screener` service into a managed watchlist: every stock that
meets all conditions on the latest daily candle, plus near-misses within a tolerance (default: one condition missed by at most 3 %
of its threshold), optionally gated by quarterly / yearly financials. Preview first, then sync; symbols you added by hand and
symbols you are trading are never removed.

```bash
python -m trading_service.terminal sync-screener --strategy 7 --tolerance 3     # one strategy
python -m trading_service.terminal sync-screener --all                           # refresh every linked list (cron, after `scan`)
```

Set `STRATEGY_SCREENER_URL` if the screener is not on `http://127.0.0.1:8000`. Details: ARCHITECTURE_AND_PROGRESS.md §3.

## Running it

| Env var | Meaning |
|---|---|
| `DATABASE_URL` | The Postgres holding the data-store tables (`symbols`, `ohlcv_1d`, `stock_financials`, `result_announcements`). Terminal tables (`terminal_*`) are created automatically. |
| `TERMINAL_MODE` | `demo` (default): synthetic market on an accelerated clock — the whole app works with no broker. `flattrade`: real prices. |
| `TERMINAL_DEMO_SPEED` | Demo clock speed-up, default `20`. |
| `FLATTRADE_USER_ID`, `FLATTRADE_TOKEN`, `FLATTRADE_ACCOUNT_ID` (optional) | Flattrade session. |

```bash
pip install -r requirements.txt
TERMINAL_MODE=demo DATABASE_URL=postgresql+psycopg2://... uvicorn main:app --port 8001
```

The router is added to `main.py` with two lines (`mount_terminal(app)` and the lifespan hook).

**Demo mode prices are generated.** The 1D data (screener, daily chart) comes from your real
`ohlcv_1d`, while demo intraday prices are synthetic and will not match it. Use `flattrade` for live work.

**Paytm Money** is a stub only; the feed interface (`feed/base.py`) is the single place to add it.

## Reading the tests as documentation

Each file opens with the scenario it describes. Good order:

| File | Story it tells |
|---|---|
| `test_engine.py` | A line + a rule + candles → signal → paper position, and no repainting |
| `test_conditions.py`, `test_rules.py` | The rule language: crosses / compare / all / any, stops, targets |
| `test_drawings.py` | Trendlines are evaluated in bar-index space and extend to the right |
| `test_builder.py` | Ticks → forming bar → closed 1m bar → bar-close events |
| `test_market.py`, `test_timeframes.py`, `test_candles.py` | Any timeframe from 1m / 1D, on the exchange clock |
| `test_hub.py`, `test_runtime.py` | Many viewers share one broker feed; slow browsers never queue stale prices |
| `test_paper.py` | Stops and targets fill at the touching price; one position per rule |
| `test_feed_flattrade.py`, `test_feed_mock.py` | Broker protocol (auth, tk/tf merging, reconnect) and the demo market |
| `test_screener.py` | One daily scan, then instant filters; metrics hand-checked on tiny series |
| `test_screener_sync.py` | Near-miss maths, financial gates, the managed watchlist (provenance, protection of traded symbols), the API and the CLI, against a fake strategy-screener |
| `test_reconnect.py` | After a feed outage the missing minutes are refilled from the broker's history |
| `test_symbols_financials.py` | Search and the financials panel's data |
| `test_watchlists.py`, `test_api.py` | Every REST route, including the error cases |
| `test_golden.py` | Keeps the TypeScript indicator maths identical to Python (see below) |

Run: `python -m pytest tests/terminal -q`. Tests use a real Postgres: `TEST_DATABASE_URL=...`
(each test gets its own throwaway schema) or, if that is unset, a temporary local cluster started
from the PostgreSQL binaries on your machine.

### Keeping the web and Python maths identical

Indicators, timeframe bucketing and line maths exist in both Python and TypeScript. Golden vectors
computed by Python are replayed by the web tests:

```bash
python scripts/make_golden_fixtures.py --also ../trade/web/src/workspace/lib/golden.json   # rewrites both copies
```

`test_golden.py` fails if you change the Python without regenerating.
