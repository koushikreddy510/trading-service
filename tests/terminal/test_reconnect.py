"""When the live feed drops and comes back, the minutes it missed are refilled from the broker's history.

Without this, a 5-minute outage left a hole in the stored 1-minute bars (backfill only continues AFTER the newest
stored bar) and the minute the feed dropped in stayed a partial bar built from only some of its ticks.
"""
import asyncio
from datetime import timedelta

from tests.terminal.helpers import bar, ist
from trading_service.terminal.runtime import Terminal

SYM = "NSE:SBIN-EQ"


class ScriptedHistory:
    """The broker's candle history: complete, correct 1-minute bars (every minute is 100 -> 101)."""

    def __init__(self):
        self.calls: list[tuple[str, object, object]] = []

    def fetch_1m(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        out, t = [], start
        while t < end:
            out.append(bar(t, 100, 101.5, 99.5, 101, 1000))
            t += timedelta(minutes=1)
        return out


class Feed:
    async def subscribe(self, symbols): ...
    async def unsubscribe(self, symbols): ...
    async def run(self, on_tick): await asyncio.sleep(3600)


def make(db):
    clock = {"now": ist(2026, 10, 19, 10, 0, 30)}
    history = ScriptedHistory()
    t = Terminal(db, Feed(), history=history, clock=lambda: clock["now"], mode="test", hub_interval=0)
    t.repair_recheck_seconds = 0
    t.subs.set_source("browser", [SYM])
    return t, history, clock


def stored(t):
    return {b.time: b for b in t.candles.minutes.load(SYM, ist(2026, 10, 19, 9, 0))}


async def settle():
    for _ in range(20):               # let the spawned repair task and its worker threads finish
        await asyncio.sleep(0.02)


async def test_minutes_missing_after_an_outage_are_refilled_and_the_partial_minute_is_corrected(db):
    t, history, clock = make(db)
    # what the live feed managed to store: 10:00 (built from only a few ticks, so too narrow), then nothing, then 10:04+ after it came back
    t.candles.minutes.save(SYM, [bar(ist(2026, 10, 19, 10, 0), 100, 100.2, 100, 100.1, 5), bar(ist(2026, 10, 19, 10, 4), 100, 101, 100, 101, 800)])

    t._on_feed_status("connected")
    clock["now"] = ist(2026, 10, 19, 10, 0, 40)
    t._on_feed_status("reconnecting")                 # the outage starts at 10:00:40
    clock["now"] = ist(2026, 10, 19, 10, 5, 10)
    t._on_feed_status("connected")                    # ...and ends at 10:05:10
    await settle()

    minutes = stored(t)
    for m in range(0, 5):                              # 10:00 .. 10:04 are all present now
        assert ist(2026, 10, 19, 10, m) in minutes, f"10:0{m} is still missing"
    assert minutes[ist(2026, 10, 19, 10, 0)].high == 101.5, "the partial 10:00 bar was replaced by the broker's complete one"
    assert minutes[ist(2026, 10, 19, 10, 2)].volume == 1000, "a minute with no ticks at all came from the broker"
    assert ist(2026, 10, 19, 10, 5) not in minutes, "the minute still forming is never stored by a repair"


async def test_the_repair_starts_a_little_before_the_outage_and_runs_a_second_time_for_the_minute_in_progress(db):
    t, history, clock = make(db)
    t._on_feed_status("connected")
    clock["now"] = ist(2026, 10, 19, 10, 0, 40)
    t._on_feed_status("reconnecting")
    clock["now"] = ist(2026, 10, 19, 10, 5, 10)
    t._on_feed_status("connected")
    await settle()
    assert len(history.calls) == 2                                          # immediately, and again after the recheck delay
    assert all(c[1] == ist(2026, 10, 19, 9, 58) for c in history.calls)     # outage began 10:00:40, minus the 2-minute margin


async def test_every_subscribed_symbol_is_repaired(db):
    t, history, clock = make(db)
    t.subs.set_source("rules", ["NSE:TCS-EQ"])
    t._on_feed_status("connected"); t._on_feed_status("reconnecting"); t._on_feed_status("connected")
    await settle()
    assert {c[0] for c in history.calls} == {SYM, "NSE:TCS-EQ"}


async def test_a_repeated_drop_while_down_keeps_the_original_start_time(db):
    t, history, clock = make(db)
    t._on_feed_status("connected")
    clock["now"] = ist(2026, 10, 19, 10, 0, 40)
    t._on_feed_status("reconnecting")
    clock["now"] = ist(2026, 10, 19, 10, 3, 0)
    t._on_feed_status("disconnected")                 # failed attempts keep flipping the status; the outage still began at 10:00:40
    t._on_feed_status("reconnecting")
    clock["now"] = ist(2026, 10, 19, 10, 6, 0)
    t._on_feed_status("connected")
    await settle()
    assert history.calls[0][1] == ist(2026, 10, 19, 9, 58)


async def test_the_first_connection_after_start_up_triggers_no_repair(db):
    t, history, clock = make(db)
    t._on_feed_status("connecting")
    t._on_feed_status("connected")
    await settle()
    assert history.calls == []                        # start-up history is the ordinary backfill's job


async def test_a_broker_error_during_repair_does_not_break_the_feed(db):
    t, history, clock = make(db)

    def boom(*a):
        raise RuntimeError("broker down")
    history.fetch_1m = boom
    t._on_feed_status("connected"); t._on_feed_status("reconnecting"); t._on_feed_status("connected")
    await settle()                                    # nothing raised out of the task
    assert t.feed_status == "connected"


def test_the_service_level_repair_overwrites_but_backfill_only_extends(db):
    """The difference in one place: backfill never touches a hole behind the newest bar; repair does."""
    t, history, clock = make(db)
    clock["now"] = ist(2026, 10, 19, 10, 6, 0)
    t.candles.minutes.save(SYM, [bar(ist(2026, 10, 19, 10, 0), 100, 100, 100, 100, 1), bar(ist(2026, 10, 19, 10, 4), 100, 100, 100, 100, 1)])
    t.candles.backfill(SYM, days=1)
    assert ist(2026, 10, 19, 10, 2) not in stored(t)              # the hole behind 10:04 stays
    t.candles.repair(SYM, ist(2026, 10, 19, 10, 0))
    assert ist(2026, 10, 19, 10, 2) in stored(t)
