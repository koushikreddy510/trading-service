"""The whole live pipeline, driven tick by tick on a fake clock.

    ticks -> forming 1m bar -> (minute ends) stored -> (5m bar ends) rule runs
          -> signal + paper position -> a later tick touches the stop -> position closed

Every step is also announced to a connected browser; the tests read that message
stream because it is exactly what the UI renders.
"""
import asyncio

import pytest

from tests.terminal.helpers import ist
from trading_service.terminal.drawings import Drawing, DrawingRepo
from trading_service.terminal.rules import rule_from_payload
from trading_service.terminal.runtime import Terminal, last_session_start
from trading_service.terminal.ticks import Tick

SYM = "NSE:SBIN-EQ"


class RecordingFeed:
    """Stands in for the broker: remembers what it was asked to subscribe to."""

    def __init__(self):
        self.subscribed: set[str] = set()
        self.log: list[tuple[str, list[str]]] = []

    async def subscribe(self, symbols):
        self.subscribed.update(symbols)
        self.log.append(("subscribe", list(symbols)))

    async def unsubscribe(self, symbols):
        self.subscribed.difference_update(symbols)
        self.log.append(("unsubscribe", list(symbols)))

    async def run(self, on_tick):
        await asyncio.sleep(3600)


class Live:
    """A terminal with a hand-driven clock, one browser watching SBIN, and the breakout rule."""

    def __init__(self, db):
        self.now = ist(2026, 10, 19, 9, 15, 0)
        self.feed = RecordingFeed()
        self.t = Terminal(db, self.feed, history=None, clock=lambda: self.now, mode="test", hub_interval=0)
        self.client = self.t.hub.connect()

    async def ready(self, with_rule=True):
        await self.t.hub.subscribe(self.client, [SYM])
        if with_rule:
            DrawingRepo(self.t.db).save(Drawing(SYM, "horizontal_line", [{"time": 1, "price": 100.0}], id="res"))
            self.t.rules.save(rule_from_payload({
                "symbol": SYM, "timeframe": "5m", "side": "BUY", "quantity": 10, "name": "Breakout 100",
                "condition": {"type": "cross", "direction": "above", "a": {"kind": "close"}, "b": {"kind": "line", "drawing_id": "res"}},
                "stop": {"type": "candle"}, "target": {"type": "rr", "ratio": 2},
            }))
        await self.t.refresh_background_subscriptions()
        self.t.flush()                      # start the bar-close clock at 09:15:00
        self.client.drain()

    def at(self, h, m, s=0):
        self.now = ist(2026, 10, 19, h, m, s)
        return self

    def tick(self, price, volume=None):
        self.t.on_tick(Tick(SYM, price, self.now, volume=volume))

    def flush(self):
        return self.t.flush()

    def messages(self, kind=None):
        out = self.client.drain()
        return [m for m in out if kind is None or m["type"] == kind]


@pytest.fixture
async def live(db):
    harness = Live(db)
    await harness.ready()
    return harness


async def test_a_breakout_on_a_closed_five_minute_candle_opens_a_paper_position(live):
    # first 5-minute candle (09:15-09:20) stays below the line
    for (m, s), price in [((15, 5), 98.0), ((16, 10), 99.0), ((17, 10), 98.5), ((19, 10), 99.5)]:
        live.at(9, m, s)
        live.tick(price)
        live.flush()
    live.at(9, 20, 1)
    live.flush()

    # second candle (09:20-09:25) pushes through 100 and closes at 101
    for (m, s), price in [((20, 10), 99.6), ((21, 10), 100.4), ((22, 30), 101.0)]:
        live.at(9, m, s)
        live.tick(price)
        live.flush()

    live.at(9, 24, 59)
    live.flush()
    assert live.messages("signal") == [], "the breakout candle is still forming: nothing may fire yet"
    assert live.t.book.open_positions() == []

    live.at(9, 25, 1)
    live.flush()
    messages = live.messages()
    kinds = [m["type"] for m in messages]
    assert "signal" in kinds and "position" in kinds
    signal = next(m["signal"] for m in messages if m["type"] == "signal")
    assert (signal["side"], signal["status"], signal["timeframe"], signal["entry"]) == ("BUY", "open", "5m", 101.0)
    assert signal["stop"] == 99.6                                   # low of the signal candle
    assert signal["target"] == pytest.approx(101.0 + 2 * 1.4)       # 2R
    (position,) = live.t.book.open_positions()
    assert (position.quantity, position.entry_price, position.stop) == (10, 101.0, 99.6)


async def test_the_browser_is_told_when_a_candle_it_is_watching_closes(live):
    live.at(9, 15, 10)
    live.tick(99.0)
    live.at(9, 20, 1)
    live.flush()
    closed = live.messages("bar_closed")
    assert {m["timeframe"] for m in closed} >= {"1m", "5m"}
    five = next(m for m in closed if m["timeframe"] == "5m")
    assert five["bar"]["time"] == int(ist(2026, 10, 19, 9, 15).timestamp()) and five["bar"]["close"] == 99.0


async def _open_position(live):
    for (m, s), price in [((15, 5), 99.0), ((20, 10), 99.6), ((21, 10), 100.4), ((22, 30), 101.0)]:
        live.at(9, m, s)
        live.tick(price)
        live.flush()
    live.at(9, 25, 1)
    live.flush()
    live.messages()


async def test_a_tick_through_the_stop_closes_the_position_at_that_price(live):
    await _open_position(live)
    live.at(9, 26, 0)
    live.tick(99.1)                       # below the 99.6 stop
    (event,) = live.messages("position")
    p = event["position"]
    assert (p["status"], p["exit_reason"], p["exit_price"]) == ("closed", "stop", 99.1)
    assert p["realized_pnl"] == pytest.approx((99.1 - 101.0) * 10)
    assert live.t.book.open_positions() == []


async def test_an_open_position_keeps_getting_bar_closes_after_its_rule_is_switched_off(live):
    await _open_position(live)
    (rule,) = live.t.rules.list()
    live.t.rules.set_enabled(rule.id, False)          # a `once` rule does this to itself; so can the user
    closes = live.t.builder.flush(ist(2026, 10, 19, 9, 30, 1))
    assert ("5m", ist(2026, 10, 19, 9, 25)) in {(e.timeframe, e.bar_start) for e in closes if e.symbol == SYM}


async def test_a_tick_to_the_target_takes_profit(live):
    await _open_position(live)
    live.at(9, 26, 0)
    live.tick(104.0)                      # above the 103.8 target
    (event,) = live.messages("position")
    assert event["position"]["exit_reason"] == "target"


async def test_prices_between_stop_and_target_leave_the_position_alone(live):
    await _open_position(live)
    live.at(9, 26, 0)
    live.tick(100.5)
    assert live.messages("position") == []
    assert len(live.t.book.open_positions()) == 1


async def test_the_same_breakout_is_not_signalled_twice_when_the_bar_close_repeats(live):
    await _open_position(live)
    live.t.flush(ist(2026, 10, 19, 9, 25, 30))
    assert live.messages("signal") == []
    assert len(live.t.book.open_positions()) == 1


async def test_ticks_reach_a_browser_watching_the_symbol_with_change_vs_previous_close(live):
    live.at(9, 16, 0)
    live.t.on_tick(Tick(SYM, 101.0, live.now, prev_close=100.0))
    (tick,) = live.messages("tick")
    assert tick["quote"]["price"] == 101.0 and tick["quote"]["change_pct"] == pytest.approx(1.0)


class TestFeedSubscriptions:
    async def test_symbols_with_enabled_rules_stay_subscribed_with_no_browser_open(self, db):
        live = Live(db)
        await live.ready()
        await live.t.hub.disconnect(live.client)
        assert SYM in live.feed.subscribed, "rules need prices even when nobody is looking"

    async def test_a_symbol_nobody_needs_is_unsubscribed(self, db):
        live = Live(db)
        await live.ready(with_rule=False)
        assert SYM in live.feed.subscribed
        await live.t.hub.disconnect(live.client)
        assert SYM not in live.feed.subscribed

    async def test_disabling_the_last_rule_releases_the_symbol(self, db):
        live = Live(db)
        await live.ready()
        await live.t.hub.disconnect(live.client)
        (rule,) = live.t.rules.list()
        live.t.rules.set_enabled(rule.id, False)
        await live.t.refresh_background_subscriptions()
        assert SYM not in live.feed.subscribed

    async def test_open_positions_keep_their_symbol_subscribed_so_stops_work(self, db):
        live = Live(db)
        await live.ready()
        await _open_position(live)
        (rule,) = live.t.rules.list()
        live.t.rules.set_enabled(rule.id, False)
        await live.t.hub.disconnect(live.client)
        await live.t.refresh_background_subscriptions()
        assert SYM in live.feed.subscribed


async def test_off_session_ticks_do_not_make_candles(live):
    live.at(8, 0, 0)
    live.tick(100.0)
    assert live.t.builder.forming_minutes(SYM) == []


def test_demo_mode_starts_at_the_most_recent_session():
    monday_morning = ist(2026, 10, 19, 8, 0)        # before the open: use Friday
    assert last_session_start(monday_morning) == ist(2026, 10, 16, 9, 30)
    saturday = ist(2026, 10, 17, 12, 0)
    assert last_session_start(saturday) == ist(2026, 10, 16, 9, 30)
    assert last_session_start(ist(2026, 10, 19, 14, 0)) == ist(2026, 10, 19, 9, 30)


async def test_demo_mode_opens_with_a_watchlist_to_look_at_but_a_real_broker_starts_empty(db):
    from trading_service.terminal.runtime import DEMO_WATCHLIST

    demo = Terminal(db, RecordingFeed(), mode="demo")
    await demo.start()
    try:
        assert demo.watchlists.list()[0]["symbols"] == DEMO_WATCHLIST
    finally:
        await demo.stop()


async def test_a_real_broker_terminal_starts_with_an_empty_watchlist(db):
    real = Terminal(db, RecordingFeed(), mode="flattrade")
    await real.start()
    try:
        assert real.watchlists.list()[0]["symbols"] == []
    finally:
        await real.stop()
