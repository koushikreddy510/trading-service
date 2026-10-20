"""Subscriptions, the quote board and the websocket hub.

These explain how many viewers + the rule engine share ONE broker feed, and why a
slow browser can never build up a backlog of stale prices.
"""
import asyncio

import pytest

from tests.terminal.helpers import ist
from trading_service.terminal.hub import MAX_SYMBOLS_PER_CLIENT, Hub, Subscriptions
from trading_service.terminal.ticks import QuoteBoard, Tick

T0 = ist(2026, 10, 19, 10, 0, 0)


def tick(symbol, price, **kw):
    return Tick(symbol, price, T0, **kw)


class TestSubscriptions:
    def test_a_symbol_is_newly_needed_only_the_first_time(self):
        s = Subscriptions()
        assert s.add("browser", ["A", "B"]) == (["A", "B"], [])
        assert s.add("rules", ["B", "C"]) == (["C"], [])      # B is already on the feed

    def test_a_symbol_stays_while_anyone_still_wants_it(self):
        s = Subscriptions()
        s.add("browser", ["A"])
        s.add("rules", ["A"])
        assert s.drop("browser") == ([], [])                    # rules still need A
        assert s.symbols() == {"A"}
        assert s.drop("rules") == ([], ["A"])                   # now nobody does
        assert s.symbols() == set()

    def test_set_source_replaces_what_a_source_wants(self):
        s = Subscriptions()
        s.set_source("rules", ["A", "B"])
        assert s.set_source("rules", ["B", "C"]) == (["C"], ["A"])

    def test_remove_only_affects_one_source(self):
        s = Subscriptions()
        s.add("x", ["A", "B"])
        assert s.remove("x", ["A"]) == ([], ["A"])
        assert s.source("x") == {"B"}


class TestQuoteBoard:
    def test_keeps_the_last_price(self):
        q = QuoteBoard()
        q.update(tick("A", 100))
        q.update(tick("A", 101))
        assert q.get("A").price == 101

    def test_fields_missing_from_a_tick_are_kept_from_earlier_ones(self):
        q = QuoteBoard()
        q.update(tick("A", 100, prev_close=98, volume=500))
        quote = q.update(tick("A", 101))        # a bare last-price tick
        assert (quote.prev_close, quote.volume) == (98, 500)

    def test_change_and_percent_are_measured_from_the_previous_close(self):
        q = QuoteBoard()
        quote = q.update(tick("A", 105, prev_close=100))
        assert quote.change == 5 and quote.change_pct == 5.0

    def test_no_change_without_a_previous_close(self):
        quote = QuoteBoard().update(tick("A", 105))
        assert quote.change is None and quote.change_pct is None

    def test_json_uses_epoch_seconds(self):
        quote = QuoteBoard().update(tick("A", 105))
        assert quote.to_json()["ts"] == int(T0.timestamp())


@pytest.fixture
def calls():
    return []


@pytest.fixture
def hub(calls):
    async def control(sub, unsub):
        calls.append((sub, unsub))

    return Hub(Subscriptions(), QuoteBoard(), control, min_interval=0)


async def test_the_feed_is_told_when_a_symbol_is_first_wanted_and_last_dropped(hub, calls):
    a, b = hub.connect(), hub.connect()
    await hub.subscribe(a, ["X"])
    await hub.subscribe(b, ["X", "Y"])
    assert calls == [(["X"], []), (["Y"], [])]

    await hub.disconnect(a)
    assert calls[-1:] == [(["Y"], [])] or len(calls) == 2    # X is still needed by b
    await hub.disconnect(b)
    assert calls[-1] == ([], ["X", "Y"])


async def test_a_client_only_receives_ticks_for_its_symbols(hub):
    c = hub.connect()
    await hub.subscribe(c, ["X"])
    hub.on_tick(tick("X", 1))
    hub.on_tick(tick("Y", 2))
    assert [m["quote"]["symbol"] for m in c.drain()] == ["X"]


async def test_ticks_are_conflated_to_the_latest_price_per_symbol(hub):
    """A slow client must not accumulate a backlog: it gets the newest price only."""
    c = hub.connect()
    await hub.subscribe(c, ["X", "Y"])
    for p in range(1, 1001):
        hub.on_tick(tick("X", p))
    hub.on_tick(tick("Y", 7))

    out = {m["quote"]["symbol"]: m["quote"]["price"] for m in c.drain()}
    assert out == {"X": 1000, "Y": 7}


async def test_a_new_subscriber_immediately_gets_the_last_known_price(hub):
    hub.on_tick(tick("X", 123))             # nobody is listening yet; the board remembers
    c = hub.connect()
    await hub.subscribe(c, ["X"])
    (msg,) = c.drain()
    assert msg["quote"]["price"] == 123


async def test_discrete_events_are_never_conflated(hub):
    c = hub.connect()
    await hub.subscribe(c, ["X"])
    for i in range(5):
        hub.broadcast({"type": "signal", "n": i})
    assert [m["n"] for m in c.drain()] == [0, 1, 2, 3, 4]


async def test_events_can_be_limited_to_viewers_of_a_symbol(hub):
    a, b = hub.connect(), hub.connect()
    await hub.subscribe(a, ["X"])
    await hub.subscribe(b, ["Y"])
    hub.broadcast({"type": "bar_closed"}, symbol="X")
    assert len(a.drain()) == 1 and b.drain() == []


async def test_unsubscribe_stops_ticks_and_discards_pending_ones(hub):
    c = hub.connect()
    await hub.subscribe(c, ["X"])
    hub.on_tick(tick("X", 1))
    await hub.unsubscribe(c, ["X"])
    hub.on_tick(tick("X", 2))
    assert c.drain() == []


async def test_a_client_cannot_subscribe_to_unlimited_symbols(hub):
    c = hub.connect()
    accepted = await hub.subscribe(c, [f"S{i}" for i in range(MAX_SYMBOLS_PER_CLIENT + 50)])
    assert len(accepted) == MAX_SYMBOLS_PER_CLIENT


async def test_subscribing_twice_is_harmless(hub, calls):
    c = hub.connect()
    await hub.subscribe(c, ["X"])
    assert await hub.subscribe(c, ["X"]) == []
    assert calls == [(["X"], [])]


async def test_a_failing_feed_does_not_break_subscribing():
    async def boom(sub, unsub):
        raise RuntimeError("broker down")

    hub = Hub(Subscriptions(), QuoteBoard(), boom, min_interval=0)
    c = hub.connect()
    assert await hub.subscribe(c, ["X"]) == ["X"]


async def test_pump_delivers_queued_messages_and_stops_on_disconnect(hub):
    c = hub.connect()
    await hub.subscribe(c, ["X"])
    sent = []

    async def send(m):
        sent.append(m)

    task = asyncio.create_task(hub.pump(c, send))
    hub.on_tick(tick("X", 5))
    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0.01)
    assert sent and sent[0]["quote"]["price"] == 5

    await hub.disconnect(c)
    await asyncio.wait_for(task, 1)
