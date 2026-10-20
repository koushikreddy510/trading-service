# Live data: how prices get to the screen, what is saved, and what happens when a connection drops

Scope: the terminal in `trading_service/terminal` (server) and `trade/web/src/workspace` (browser).
Everything below describes the code as it is now, except section 6, which is a design for later and
is **not implemented**.

## 1. The path of one price

```
 broker (Flattrade websocket)
   │  "tk" = full snapshot, "tf" = only the fields that changed      feed/flattrade.py
   ▼
 FlattradeFeed._handle        merges deltas per instrument → one Tick(symbol, price, volume, ts)
   ▼
 Terminal.on_tick   (runtime.py, runs on the asyncio loop, does no I/O)
   ├─▶ CandleBuilder.on_tick   folds the tick into the *forming* 1-minute bar          (memory)
   ├─▶ Hub.on_tick             keeps the latest quote per symbol for each browser        (memory)
   └─▶ PaperBook               did this price touch an open paper position's stop/target? (memory, reads DB only when it does)

 every 1 second:  Terminal.flush
   ├─▶ CandleBuilder closes every minute that has ended → INSERT into terminal_ohlcv_1m  (the only routine DB write)
   ├─▶ for each timeframe whose bar ends on that minute → CloseEvent
   └─▶ RuleEngine evaluates the rules on that completed bar → signal + paper position → Hub

 Hub (hub.py) ──websocket /terminal/ws──▶ browser LiveSocket (api/socket.ts) ─▶ Controller.onMessage ─▶ store ─▶ React
```

Messages the browser receives: `hello`, `tick`, `bar_closed`, `signal`, `position`, `status`, `error`, `pong`.
It sends `subscribe`, `unsubscribe`, `ping`.

## 2. "Is everything written to the database first?"  No.

The database is **not** in the price path. A tick reaches the browser straight from memory.

| Thing | Where it lives | Lost if the process dies? |
|---|---|---|
| Every tick / 1-second price | Not stored anywhere. Streamed, then folded into the forming bar. | Yes, by design |
| The forming 1-minute bar | `CandleBuilder` memory | Yes: up to the last ≤59 s |
| Closed 1-minute bars | Postgres `terminal_ohlcv_1m` (one `INSERT … ON CONFLICT DO UPDATE` per finished minute per symbol) | No |
| 5m … 4H, 1W candles | Never stored; resampled from the 1m rows when asked (`market.py`) | n/a |
| 1D candles | Data-store's `ohlcv_1d` (its own daily job) plus today's bar built from the 1m rows | No |
| Signals, paper positions, rules, drawings, watchlists | Postgres, written when they happen | No |
| Latest quote per symbol, who is subscribed to what | Memory | Rebuilt as clients reconnect |

Order inside the 1-second `flush`: **the finished bar is saved first, then the bar-close event is emitted**,
so the rule engine and the browser always read the finished bar from the database.

Why 1-minute and not 1-second in Postgres: a second-level history of 100 symbols is ~2.3 M rows a day that
no screen or rule reads. Everything the product needs (charts from 1m to 1W, rules on completed candles, the
screener) derives from 1-minute bars.

## 3. When a connection is lost for a few minutes

There are three different connections. Each recovers differently.

### a) Broker ⇄ our server (the one that matters for data)

| What happens | Code |
|---|---|
| Websocket errors or closes → status `disconnected` → wait 1 s (doubling, max 30 s) → status `reconnecting` → connect → authenticate → status `connected` → **resubscribe every wanted symbol** | `FlattradeFeed.run`, `_resubscribe` |
| Every status change is broadcast to browsers (`{"type":"status","feed":…}`), so the top bar shows it | `Terminal._on_feed_status` |
| **Gap repair (added in this round).** On the first `connected` after an outage, every subscribed symbol's 1-minute history is re-fetched from the broker (`TPSeries`) from *2 minutes before the outage began* up to the current minute, and written with `ON CONFLICT DO UPDATE`. It runs again ~70 s later to fix the minute that was forming at the moment the feed came back. | `Terminal.repair_gap`, `CandleService.repair`; tests: `tests/terminal/test_reconnect.py` |

Before this round only `backfill` existed. It fetches from *the newest stored bar + 1 minute*, so it fixed a gap at the
**end** (process was off) but could never fix a hole in the **middle** (feed down 10:01–10:04, bars 10:00 and 10:05+ already
stored). That was a real bug; `repair` is the fix.

What is **not** recoverable: the individual ticks / 1-second prices inside the outage. The broker offers no tick history,
so the on-screen forming candle simply jumps to the first price after reconnect. The 1-minute bars (and so every other
timeframe) are complete after repair.

**Honest limit — rules during an outage.** The 1-second clock keeps emitting bar-close events even with no ticks, and the
rule engine evaluates whatever bars exist at that moment. After a repair fills the hole, the rules are **not re-run** for
bars that closed during the outage. Effects: a crossing that happened inside the outage is *missed* (never invented: the
`(rule_id, bar_time)` unique constraint makes a rule fire at most once per bar). If you would rather get a late signal than
no signal, the next step is a replay after `repair_gap` that calls `engine.on_candle_close` for the bars that closed in the
outage, with a "late" flag and a rule on how stale an entry price may be. That is a trading decision, so it is left for you.

### b) Our server restarts (or was off overnight)

On the first subscription of a symbol and on every `/candles` request (at most once a minute per symbol),
`CandleService.backfill` fetches 1-minute bars from the newest stored bar to now (up to 30 days by default, hard cap 120)
and stores them. The bar-close clock does not replay events after a restart longer than 3 days (`MAX_CATCH_UP`), and it
never replays rule evaluations for the time the process was down.

### c) Browser ⇄ our server

| What happens | Code |
|---|---|
| Socket closes → reconnect after 1 s, 2 s, 5 s, 10 s, then every 10 s; on open it re-sends `subscribe` for everything it wants | `api/socket.ts` |
| **Added in this round.** On the first reconnect the controller re-reads candles, signals and positions over REST, because the server does not replay what it pushed while the socket was down | `Controller.onConnection` → `resync`; tests in `store/controller.test.ts` |
| A browser that falls behind: ticks are *conflated* (it gets the latest price per symbol, at most ~4/s), discrete events queue up to 1000 per client and then the oldest are dropped | `Hub`, `Client.push_event` |

Before this round the chart kept its old candles after a reconnect until you changed symbol or timeframe.

## 4. WebSocket or SSE?

The terminal uses a **WebSocket** (`/terminal/ws`), and the Go Atlas terminal does too (`internal/terminal/hub.go`).
The reason is that the browser changes what it listens to all the time (open a chart, add a watchlist symbol), and a
WebSocket carries those `subscribe` / `unsubscribe` messages on the same connection.

Server-Sent Events would be a fit for the one-way part (prices, signals) with a separate REST call for subscribing. Its real
advantage is built-in replay: the browser reconnects automatically and sends `Last-Event-ID`, so the server can resend
the discrete events it missed. Its costs: one-way only, a browser limit of about six HTTP/1.1 connections per origin, and
text only. Switching is not needed for correctness; the replay it gives can be added to the WebSocket with an event id
(see 6c).

## 5. "What if I want live prices for many more scripts?"  (not implemented)

Today a browser can watch up to 200 symbols (`MAX_SYMBOLS_PER_CLIENT`) and the server subscribes the broker feed to the
union of every open browser, every enabled rule and every open position. A 100–200 symbol watchlist works now. This
section is about thousands, e.g. the whole screener universe.

Where the limits are, in the order you will hit them:

1. **The broker.** One websocket carries a limited number of instruments and the broker may rate-limit subscribes.
   Find the exact figure in the broker's documentation before designing; everything else is secondary to it.
2. **The browser.** 200 symbols × 4 updates/s is ~800 messages/s into one tab, which React can render but should not be asked
   to. A screen only needs to stream what is *visible*: the symbols on screen plus the open chart. Subscribe on scroll,
   not for the whole list.
3. **One process.** Ingest, candle building, rules and fan-out share one event loop. The minute-close write is one
   `INSERT` per symbol in its own transaction, so 2,000 symbols means a burst of 2,000 small transactions every minute.
   Batching them into one transaction per flush is a small change and removes this limit.
4. **Storage** is not a limit: 2,000 symbols × 375 minutes ≈ 750 k rows a day.

## 6. Design for later: when to add Redis (or similar)

**You do not need Redis for one backend process.** The in-memory `Hub` is the right tool until you run more than one
API worker, or you want the broker connection to survive a deploy of the web tier.

Add a bus when you split the process in two:

```
 ingest service ── owns the broker websocket, builds candles, runs rules, writes Postgres
        │  publishes
        ▼
      Redis ──────────────────────────────┐
        │ subscribe                        │ subscribe
        ▼                                  ▼
 gateway worker 1 … N  (many browsers each; websockets; no broker, no DB writes)
```

What goes over the bus, and which Redis feature fits each kind of message:

| Message | Nature | Use |
|---|---|---|
| Ticks / last price | Disposable: only the newest value matters, a missed one is harmless | **Pub/Sub**, sent as a **batch** every ~250 ms (`{symbol: [price, ts], …}`) rather than one message per tick; shard channels (`ticks:0 … ticks:15` by hash of the symbol) so a gateway subscribes to what it needs. Also keep the latest quote in a Redis hash so a new gateway or browser has a snapshot immediately. |
| `bar_closed`, `signal`, `position` | Must not be lost; clients must be able to catch up | **Streams** (`XADD` with a per-stream id; gateways `XREAD` and keep the last id). A reconnecting browser sends the last id it saw and the gateway replays from it — this is the proper fix for "pushes while the socket was down are lost" and replaces the REST resync. |
| 1-minute bar writes | Durable | Either the ingest service writes Postgres directly (simplest), or a Streams consumer group does it so a slow database never slows ingest. |

NATS JetStream or Kafka do the same jobs. They are worth their cost only if several independent consumers (analytics,
backtesting, alerting) must replay the same history at scale. For a trading terminal with one broker feed, Redis is
already in the Go stack (Atlas stores drawings there), so it adds no new operational piece.

What would change in this codebase (small, because the seams already exist):

* Introduce a `Bus` interface with `publish_tick_batch`, `publish_event`, `subscribe`. `InProcessBus` wraps today's `Hub`
  (default; behaviour unchanged). `RedisBus` implements the table above.
* `Terminal.on_tick` and `Terminal._on_bar_closed` publish to the bus instead of calling the hub directly.
* A gateway entry point (`python -m trading_service.terminal gateway`) runs only the websocket API and the bus subscriber.
* Add `seq` / event id to every discrete event; `LiveSocket` sends `since=<last id>` on reconnect.
* `Subscriptions` becomes the union across gateways (each gateway reports its symbols to the ingest service through a Redis set
  with a TTL, so a crashed gateway's symbols expire).

Rough decision rule: one process and fewer than ~300 symbols → keep as is. More symbols but one process → batch the writes,
stream only what is visible. Several API workers, or zero-downtime deploys → add the bus.
