"""Wires everything together and runs it: feed -> candles -> rules -> paper book -> browsers.

    broker feed --tick--> Terminal.on_tick --+--> CandleBuilder   (forming 1m bar, in memory)
                                             +--> Hub            (latest price to every browser)
                                             +--> PaperBook      (stop / target touched?)

    1-second timer ----> Terminal.flush  --> persist finished minutes
                                         --> bar-close events --> RuleEngine --> signals / positions
                                                              --> browsers watching that symbol

Everything above runs on the asyncio event loop, so the in-memory candle state needs
no locks. The only blocking work is a few small SQL statements per closed minute.

Two modes (TERMINAL_MODE): `demo` uses a synthetic market on an accelerated clock so
the app is fully usable with no broker; `flattrade` uses real prices.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from .builder import CandleBuilder, CloseEvent
from .candles import Candle
from .db import Database
from .drawings import DrawingRepo
from .engine import RuleEngine
from .feed.base import Feed, History
from .hub import Hub, Subscriptions
from . import instruments
from .instruments import Instruments
from .market import CandleService
from .paper import PaperBook
from .rules import RuleRepo, SignalRepo
from .live_mirror import LiveMirror
from .orders import OrderService
from .sessions import BrokerLogins, SessionStore
from .notify import Notifier
from .ticks import QuoteBoard, Tick
from .timeframes import IST, UTC, session_open
from .watchlists import WatchlistRepo

log = logging.getLogger(__name__)

RULES_SOURCE = "rules"          # symbols that must stay subscribed so rules keep evaluating
POSITIONS_SOURCE = "positions"  # symbols with an open paper position (their stops need ticks)
LIVE_SOURCE = "live"            # symbols this app holds or has open orders in (live P&L needs ticks)
BACKFILL_COOLDOWN = timedelta(seconds=60)
REPAIR_MARGIN = timedelta(minutes=2)   # re-fetch a little before the outage began: the minute it dropped in is a partial bar
ORDER_SYNC_SECONDS = 3.0              # how often open live orders are refreshed from the broker
REPAIR_RECHECK_SECONDS = 70.0          # ...and once more after the minute that was forming at reconnect has closed
DEMO_WATCHLIST = ["NSE:SBIN-EQ", "NSE:TCS-EQ", "NSE:INFY-EQ", "NSE:HDFCBANK-EQ", "NSE:RELIANCE-EQ"]  # so demo mode opens with something to look at


class Terminal:
    def __init__(
        self,
        db: Database,
        feed: Feed,
        history: Optional[History] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        mode: str = "custom",
        hub_interval: float = 0.25,
        flush_interval: float = 1.0,
        notifier: Optional[Notifier] = None,
    ):
        self.db, self.feed, self.history, self.clock, self.mode = db, feed, history, clock, mode
        self.flush_interval = flush_interval
        self.subs = Subscriptions()
        self.quotes = QuoteBoard()
        self.hub = Hub(self.subs, self.quotes, self._feed_control, min_interval=hub_interval)
        self.candles = CandleService(db, history, None, clock)
        self.builder = CandleBuilder(self.candles.store_minute, self._evaluated_symbols)
        self.candles.builder = self.builder
        self.instruments = Instruments(db)
        self.book = PaperBook(db, self.instruments.point_value)
        self.notifier = notifier or Notifier(tag=mode)
        self.hub.listeners.append(self.notifier.on_event)
        self.engine = RuleEngine(db, self.candles.candles, self.book, clock)
        self.rules, self.drawings, self.signals = RuleRepo(db), DrawingRepo(db), SignalRepo(db)
        self.watchlists = WatchlistRepo(db)
        self.feed_status = "disconnected"
        self.broker = None            # account.BrokerAccount in live mode: funds, quotes, margin previews (read-only)
        self.token_listeners: dict[str, list[Callable[[dict], Awaitable[None] | None]]] = {}   # broker -> who needs a new token
        self.logins = BrokerLogins(db, apply=self.apply_tokens)
        self.orders = OrderService(
            db, accounts=lambda: {"flattrade": self.broker} if self.broker is not None else {},
            quote=lambda symbol: self.broker.quote(symbol),   # data always from the data broker
            notify=lambda text, channel="orders": self.notifier.send(text, channel=channel),
            broadcast=self.hub.broadcast,
        )
        self.mirror = LiveMirror(db, self.orders, notify=lambda text: self.notifier.send(text, channel="orders"), clock=clock,
                                 broadcast=self.hub.broadcast)
        self.hub.listeners.append(self.mirror.on_event)
        self._tasks: list[asyncio.Task] = []
        self._backfilled: dict[str, datetime] = {}
        self._lost_at: Optional[datetime] = None      # when the live feed was last seen going down (None = it is up, or never was)
        self.repair_recheck_seconds = REPAIR_RECHECK_SECONDS

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.watchlists.ensure_default(DEMO_WATCHLIST if self.mode == "demo" else ())
        self._seed_indices()
        await self.refresh_background_subscriptions()
        self._tasks = [
            asyncio.create_task(self._run_feed(), name="terminal-feed"),
            asyncio.create_task(self._flush_loop(), name="terminal-flush"),
            asyncio.create_task(self._order_loop(), name="terminal-orders"),
            asyncio.create_task(self._cutoff_loop(), name="terminal-intraday-cutoff"),
        ]

    async def apply_tokens(self, broker: str, tokens: dict) -> None:
        """A new login: hand the token to the feed / broker clients that use it, without a restart."""
        for listener in self.token_listeners.get(broker, []):
            res = listener(tokens)
            if asyncio.iscoroutine(res):
                await res

    def _seed_indices(self) -> None:
        """Index list in place on every start; the "Indices" watchlist only the first time (a deleted one stays deleted)."""
        first = not self.db.scalar("SELECT EXISTS (SELECT 1 FROM terminal_instruments WHERE instrument = 'INDEX')")
        instruments.seed_indices(self.db)
        if first and not any(w["name"].lower() == instruments.INDEX_WATCHLIST.lower() for w in self.watchlists.list()):
            self.watchlists.create(instruments.INDEX_WATCHLIST, [i[0] for i in instruments.INDICES])

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _run_feed(self) -> None:
        if hasattr(self.feed, "on_status"):
            self.feed.on_status = self._on_feed_status  # type: ignore[attr-defined]
        await self.feed.run(self.on_tick)

    def _on_feed_status(self, status: str) -> None:
        was_up = self.feed_status == "connected"
        self.feed_status = status
        self.hub.broadcast({"type": "status", "feed": status})
        if status != "connected" and was_up and self._lost_at is None:
            self._lost_at = self.clock()
        elif status == "connected" and self._lost_at is not None:
            since, self._lost_at = self._lost_at, None
            self._spawn(self.repair_gap(since))

    async def repair_gap(self, since: datetime) -> None:
        """The feed is back after an outage that began at `since`: refill every subscribed symbol's missing
        minutes from the broker's history (twice: now, and again once the minute in progress has closed)."""
        for attempt in range(2):
            for symbol in sorted(self.subs.symbols()):
                try:
                    await asyncio.to_thread(self.candles.repair, symbol, since - REPAIR_MARGIN)
                except Exception:
                    log.exception("gap repair failed for %s", symbol)
            if attempt == 0:
                await asyncio.sleep(self.repair_recheck_seconds)

    async def _order_loop(self) -> None:
        """Follow open live orders until they finish (cheap when there are none: one SQL count)."""
        while True:
            try:
                if self.orders.repo.open():
                    if await asyncio.to_thread(self.orders.sync):
                        await self.refresh_background_subscriptions()
            except Exception:
                log.exception("order sync failed")
            await asyncio.sleep(ORDER_SYNC_SECONDS)

    async def _cutoff_loop(self) -> None:
        while True:
            try:
                self.close_for_cutoff()
                self.mirror.expire_due()                  # approvals nobody decided on in time
            except Exception:
                log.exception("intraday cut-off / approval expiry failed")
            await asyncio.sleep(30)

    def close_for_cutoff(self) -> list[dict]:
        """Close live-mirrored intraday positions at the cut-off: the paper position closes at the last price and the
        mirror closes the live side exactly as for any other exit."""
        events = []
        for link in self.mirror.due_for_cutoff():
            p = next((x for x in self.book.open_positions(link["symbol"]) if x.id == link["position_id"]), None)
            if p is None:
                continue
            q = self.quotes.get(p.symbol)
            closed = self.book.close(p, q.price if q else p.entry_price, "intraday_cutoff", self.clock())
            event = {"type": "position", "position": closed.to_json()}
            self.hub.broadcast(event)
            events.append(event)
        if events:
            self._spawn(self.refresh_background_subscriptions())
        return events

    async def _flush_loop(self) -> None:
        while True:
            try:
                self.flush()
            except Exception:
                log.exception("flush failed")
            await asyncio.sleep(self.flush_interval)

    # --------------------------------------------------------------- subscriptions
    def _rule_symbols(self) -> set[str]:
        return {r.symbol for r in self.rules.list(enabled_only=True)}

    def _evaluated_symbols(self) -> set[str]:
        """Symbols whose bar closes reach the rule engine: enabled rules (entries) and open positions (trailing stop
        and exit rule), so a position keeps being managed after its rule fired `once` or was switched off."""
        return self._rule_symbols() | {p.symbol for p in self.book.open_positions()}

    async def refresh_background_subscriptions(self) -> None:
        """Keep the feed subscribed to symbols the rule engine and open positions need,
        whether or not any browser is connected."""
        rule_syms = self._rule_symbols()
        position_syms = {p.symbol for p in self.book.open_positions()}
        live_syms = {p["symbol"] for p in self.orders.repo.positions() if p["units"]} | {o["symbol"] for o in self.orders.repo.open()}
        for name, wanted in ((RULES_SOURCE, rule_syms), (POSITIONS_SOURCE, position_syms), (LIVE_SOURCE, live_syms)):
            await self._apply(*self.subs.set_source(name, wanted))

    async def _apply(self, newly: list[str], gone: list[str]) -> None:
        await self._feed_control(newly, gone)

    async def _feed_control(self, newly: list[str], gone: list[str]) -> None:
        if newly:
            await self.feed.subscribe(newly)
            for symbol in newly:
                self.ensure_history(symbol)
        if gone:
            await self.feed.unsubscribe(gone)

    def ensure_history(self, symbol: str) -> None:
        """Backfill 1-minute history in the background (at most once a minute per symbol)."""
        if self.history is None:
            return
        now = self.clock()
        last = self._backfilled.get(symbol)
        if last is not None and now - last < BACKFILL_COOLDOWN:
            return
        self._backfilled[symbol] = now
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(asyncio.to_thread(self.candles.backfill, symbol))

    def backfill_due(self, symbol: str) -> bool:
        last = self._backfilled.get(symbol)
        return self.history is not None and (last is None or self.clock() - last >= BACKFILL_COOLDOWN)

    async def backfill_now(self, symbol: str) -> int:
        """Backfill and wait (used by the candles endpoint so the first chart load is complete)."""
        self._backfilled[symbol] = self.clock()
        return await asyncio.to_thread(self.candles.backfill, symbol)

    # ------------------------------------------------------------------- the hot path
    def on_tick(self, tick: Tick) -> None:
        self.builder.on_tick(tick)
        self.hub.on_tick(tick)
        if self.book.would_trigger(tick.symbol, tick.price):
            closed = self.book.on_tick(tick.symbol, tick.price, tick.ts)
            for position in closed:
                self.hub.broadcast({"type": "position", "position": position.to_json()})
            if closed:
                self._spawn(self.refresh_background_subscriptions())

    # ------------------------------------------------------------------ the 1s timer
    def flush(self, now: Optional[datetime] = None) -> list[dict]:
        """Close finished minutes, run the rules for every bar that just closed, and
        announce the results. Returns the events it broadcast (handy in tests)."""
        events: list[dict] = []
        for close in self.builder.flush(now or self.clock()):
            events.extend(self._on_bar_closed(close))
        return events

    def _on_bar_closed(self, close: CloseEvent) -> list[dict]:
        out: list[dict] = []
        try:
            out.extend(self.engine.on_candle_close(close.symbol, close.timeframe, close.bar_start))
        except Exception:
            log.exception("rule engine failed for %s %s", close.symbol, close.timeframe)
        if any(close.symbol in c.symbols for c in self.hub.clients.values()):
            bar = self._closed_bar(close)
            if bar is not None:
                out.append({"type": "bar_closed", "symbol": close.symbol, "timeframe": close.timeframe, "bar": bar.to_json()})
        for event in out:
            is_bar = event["type"] == "bar_closed"
            self.hub.broadcast(event, symbol=close.symbol if is_bar else None)
        if any(e["type"] in ("position", "signal") for e in out):
            self._spawn(self.refresh_background_subscriptions())   # a `once` rule may have switched itself off
        return out

    @staticmethod
    def _spawn(coro: Awaitable) -> None:
        try:
            asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
        except RuntimeError:          # no event loop (plain synchronous test): nothing to schedule
            coro.close()  # type: ignore[attr-defined]

    def _closed_bar(self, close: CloseEvent) -> Optional[Candle]:
        bars = self.candles.candles(close.symbol, close.timeframe, 3)
        return next((b for b in reversed(bars) if b.time == close.bar_start), None)

    # --------------------------------------------------------------------- status
    def status(self) -> dict:
        return {
            "mode": self.mode,
            "feed": getattr(self.feed, "status", self.feed_status),
            "now": int(self.clock().timestamp()),
            "subscribed": sorted(self.subs.symbols()),
            "clients": len(self.hub.clients),
        }


# ------------------------------------------------------------------- construction
def last_session_start(now: datetime) -> datetime:
    """The most recent weekday 09:30 IST at or before `now` (demo mode starts there)."""
    local = now.astimezone(IST)
    day = local.date()
    if local.hour < 9 or (local.hour == 9 and local.minute < 30):
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return session_open(day).astimezone(UTC) + timedelta(minutes=15)


def build_terminal(db: Database, mode: Optional[str] = None) -> Terminal:
    mode = (mode or os.getenv("TERMINAL_MODE", "demo")).lower()
    if mode == "demo":
        from .feed.mock import MockFeed, MockHistory, MockMarket, SimClock

        market = MockMarket()
        speed = float(os.getenv("TERMINAL_DEMO_SPEED", "20"))
        clock = SimClock(last_session_start(datetime.now(UTC)), speed=speed)
        feed = MockFeed(clock, market)
        return Terminal(db, feed, MockHistory(market), clock.now, mode="demo")
    if mode == "flattrade":
        from .feed.flattrade import FlattradeApi, FlattradeFeed, FlattradeHistory, TokenResolver, config_from_env

        cfg = config_from_env(SessionStore(db).env(), require_token=False)   # a stored login beats the environment
        api = FlattradeApi(cfg["user_id"], cfg["token"])
        resolver = TokenResolver(api, db)
        feed = FlattradeFeed(cfg["user_id"], cfg["account_id"], cfg["token"], resolver)
        from .account import BrokerAccount

        instruments = Instruments(db)
        terminal = Terminal(db, feed, FlattradeHistory(api, resolver, instruments), mode="flattrade")
        terminal.broker = BrokerAccount(api, resolver, instruments, cfg["account_id"])

        async def use_flattrade_token(tokens: dict) -> None:
            api.token = tokens["FLATTRADE_TOKEN"]
            await feed.use_token(tokens["FLATTRADE_TOKEN"])

        terminal.token_listeners.setdefault("flattrade", []).append(use_flattrade_token)
        return terminal
    raise ValueError(f"TERMINAL_MODE must be 'demo' or 'flattrade', got {mode!r}")
