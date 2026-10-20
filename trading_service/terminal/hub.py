"""Subscriptions and the websocket fan-out hub.

`Subscriptions` answers "which symbols must the broker feed be subscribed to?".
Several parties need prices and they must not step on each other:

* every connected browser (the symbols in its watchlist and on its chart),
* the rule engine (any symbol with an enabled rule - rules must keep evaluating
  with no browser open),
* open paper positions (their stops must be watched).

A symbol stays subscribed while ANY of them wants it; the feed is only told when
the symbol first becomes needed or stops being needed by everyone.

`Hub` fans events out to websocket clients. Ticks are CONFLATED per client: a
client that is slower than the feed always receives the latest price per symbol
rather than a growing backlog, and a flood of ticks costs at most one message per
symbol per interval. Discrete events (signals, positions, bar closes) are never
dropped.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable, Iterable, Optional

from .ticks import QuoteBoard, Tick

log = logging.getLogger(__name__)

MAX_QUEUED_EVENTS = 1000
MAX_SYMBOLS_PER_CLIENT = 200


class Subscriptions:
    def __init__(self) -> None:
        self._by_source: dict[str, set[str]] = {}

    def symbols(self) -> set[str]:
        return set().union(*self._by_source.values()) if self._by_source else set()

    def source(self, name: str) -> set[str]:
        return set(self._by_source.get(name, ()))

    def set_source(self, name: str, symbols: Iterable[str]) -> tuple[list[str], list[str]]:
        """Replace everything `name` wants. Returns (newly needed, no longer needed)."""
        before = self.symbols()
        wanted = set(symbols)
        if wanted:
            self._by_source[name] = wanted
        else:
            self._by_source.pop(name, None)
        after = self.symbols()
        return sorted(after - before), sorted(before - after)

    def add(self, name: str, symbols: Iterable[str]) -> tuple[list[str], list[str]]:
        return self.set_source(name, self.source(name) | set(symbols))

    def remove(self, name: str, symbols: Iterable[str]) -> tuple[list[str], list[str]]:
        return self.set_source(name, self.source(name) - set(symbols))

    def drop(self, name: str) -> tuple[list[str], list[str]]:
        return self.set_source(name, ())


class Client:
    """One websocket connection's state."""

    _ids = itertools.count(1)

    def __init__(self) -> None:
        self.id = f"client:{next(self._ids)}"
        self.symbols: set[str] = set()
        self.pending_ticks: dict[str, dict] = {}
        self.events: deque[dict] = deque()
        self.wakeup = asyncio.Event()
        self.closed = False

    def push_tick(self, quote_json: dict) -> None:
        self.pending_ticks[quote_json["symbol"]] = quote_json   # conflate: keep only the latest
        self.wakeup.set()

    def push_event(self, event: dict) -> None:
        if len(self.events) >= MAX_QUEUED_EVENTS:
            self.events.popleft()
        self.events.append(event)
        self.wakeup.set()

    def drain(self) -> list[dict]:
        out = list(self.events)
        self.events.clear()
        out.extend({"type": "tick", "quote": q} for q in self.pending_ticks.values())
        self.pending_ticks.clear()
        self.wakeup.clear()
        return out


FeedControl = Callable[[list[str], list[str]], Awaitable[None]]  # (subscribe, unsubscribe)


class Hub:
    def __init__(
        self,
        subscriptions: Subscriptions,
        quotes: QuoteBoard,
        feed_control: Optional[FeedControl] = None,
        min_interval: float = 0.1,
    ):
        self.subs = subscriptions
        self.quotes = quotes
        self.feed_control = feed_control
        self.min_interval = min_interval
        self.clients: dict[str, Client] = {}
        self.listeners: list[Callable[[dict], None]] = []

    # ------------------------------------------------------------ connections
    def connect(self) -> Client:
        client = Client()
        self.clients[client.id] = client
        return client

    async def disconnect(self, client: Client) -> None:
        client.closed = True
        client.wakeup.set()
        self.clients.pop(client.id, None)
        await self._apply(*self.subs.drop(client.id))

    async def subscribe(self, client: Client, symbols: Iterable[str]) -> list[str]:
        """Subscribe `client` to symbols; immediately sends it the last known quote of each."""
        wanted = [s for s in dict.fromkeys(symbols) if isinstance(s, str) and s]
        room = MAX_SYMBOLS_PER_CLIENT - len(client.symbols)
        wanted = [s for s in wanted if s not in client.symbols][: max(room, 0)]
        client.symbols.update(wanted)
        await self._apply(*self.subs.add(client.id, wanted))
        for symbol in wanted:
            quote = self.quotes.get(symbol)
            if quote:
                client.push_tick(quote.to_json())
        return wanted

    async def unsubscribe(self, client: Client, symbols: Iterable[str]) -> None:
        gone = [s for s in symbols if s in client.symbols]
        client.symbols.difference_update(gone)
        for s in gone:
            client.pending_ticks.pop(s, None)
        await self._apply(*self.subs.remove(client.id, gone))

    async def _apply(self, newly_needed: list[str], no_longer_needed: list[str]) -> None:
        if self.feed_control and (newly_needed or no_longer_needed):
            try:
                await self.feed_control(newly_needed, no_longer_needed)
            except Exception:  # a broker hiccup must not break the websocket
                log.exception("feed subscription change failed")

    # ---------------------------------------------------------------- publish
    def on_tick(self, tick: Tick) -> None:
        quote = self.quotes.update(tick).to_json()
        for client in self.clients.values():
            if tick.symbol in client.symbols:
                client.push_tick(quote)

    def broadcast(self, event: dict, symbol: Optional[str] = None) -> None:
        """Send a discrete event to every client (or only those watching `symbol`)."""
        for client in self.clients.values():
            if symbol is None or symbol in client.symbols:
                client.push_event(event)
        for listener in self.listeners:           # e.g. the Slack notifier; a failing one must not stop the others
            try:
                listener(event)
            except Exception:  # noqa: BLE001
                log.exception("event listener failed")

    # ------------------------------------------------------------------ pump
    async def pump(self, client: Client, send: Callable[[dict], Awaitable[None]]) -> None:
        """Deliver queued events to one client until it disconnects."""
        last_sent = 0.0
        while not client.closed:
            await client.wakeup.wait()
            if client.closed:
                return
            wait = self.min_interval - (time.monotonic() - last_sent)
            if wait > 0:
                await asyncio.sleep(wait)
            for message in client.drain():
                await send(message)
            last_sent = time.monotonic()
