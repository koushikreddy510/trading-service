"""The demo market: deterministic, session-aware, and self-consistent between the
history the chart loads and the live ticks that extend it."""
from datetime import timedelta

from tests.terminal.helpers import ist
from trading_service.terminal.feed.mock import MockFeed, MockHistory, MockMarket, SimClock

SYM = "NSE:SBIN-EQ"
OPEN = ist(2026, 10, 19, 9, 15)


def test_the_same_minute_always_has_the_same_bar():
    a, b = MockMarket(), MockMarket()
    assert a.minute_bar(SYM, OPEN) == b.minute_bar(SYM, OPEN)


def test_different_symbols_move_differently():
    m = MockMarket()
    assert m.minute_bar(SYM, OPEN).close != m.minute_bar("NSE:TCS-EQ", OPEN).close


def test_bars_are_well_formed_and_chain_close_to_open():
    bars = MockHistory().fetch_1m(SYM, OPEN, OPEN + timedelta(hours=3))
    assert len(bars) == 180
    for prev, cur in zip(bars, bars[1:]):
        assert cur.time - prev.time == timedelta(minutes=1)
        assert abs(cur.open - prev.close) < 0.06, "each bar opens where the last one closed (to tick size)"
    for b in bars:
        assert b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close)


def test_history_has_no_bars_outside_the_session_or_at_weekends():
    h = MockHistory()
    assert h.fetch_1m(SYM, ist(2026, 10, 19, 7, 0), ist(2026, 10, 19, 9, 15)) == []
    assert h.fetch_1m(SYM, ist(2026, 10, 24, 9, 15), ist(2026, 10, 25, 15, 30)) == []
    assert len(h.fetch_1m(SYM, ist(2026, 10, 19, 15, 0), ist(2026, 10, 19, 17, 0))) == 30


def test_a_full_nse_day_has_375_minutes_and_a_full_mcx_day_870():
    h = MockHistory()
    assert len(h.fetch_1m(SYM, ist(2026, 10, 19), ist(2026, 10, 20))) == 375
    assert len(h.fetch_1m("MCX:SILVERM", ist(2026, 10, 19), ist(2026, 10, 20))) == 870


def test_live_ticks_stay_inside_the_minutes_range_and_end_near_its_close():
    m = MockMarket()
    bar = m.minute_bar(SYM, OPEN)
    for s in range(0, 60, 7):
        t = m.tick_at(SYM, OPEN + timedelta(seconds=s))
        assert bar.low - 0.05 <= t.price <= bar.high + 0.05
    late = m.tick_at(SYM, OPEN + timedelta(seconds=59.9))
    assert abs(late.price - bar.close) < (bar.high - bar.low) * 0.5 + 0.1


def test_cumulative_volume_only_grows_within_a_day():
    m = MockMarket()
    vols = [m.tick_at(SYM, OPEN + timedelta(seconds=s)).volume for s in range(0, 600, 30)]
    assert vols == sorted(vols)


class TestMockFeed:
    def test_emits_only_for_subscribed_symbols(self):
        feed = MockFeed(SimClock(ist(2026, 10, 19, 10, 0, 5), speed=0))
        got = []
        import asyncio

        asyncio.run(feed.subscribe([SYM]))
        assert feed.emit(got.append) == 1
        assert [t.symbol for t in got] == [SYM]
        asyncio.run(feed.unsubscribe([SYM]))
        assert feed.emit(got.append) == 0

    def test_is_silent_when_the_market_is_closed(self):
        import asyncio

        feed = MockFeed(SimClock(ist(2026, 10, 19, 20, 0, 0), speed=0))
        asyncio.run(feed.subscribe([SYM]))
        assert feed.emit(lambda t: None) == 0

    def test_the_sim_clock_advances_by_hand(self):
        clock = SimClock(ist(2026, 10, 19, 10, 0, 0), speed=0)
        clock.advance(90)
        assert clock.now() == ist(2026, 10, 19, 10, 1, 30)

    def test_the_sim_clock_can_run_faster_than_real_time(self):
        t = [100.0]
        clock = SimClock(ist(2026, 10, 19, 10, 0, 0), speed=60, monotonic=lambda: t[0])
        t[0] += 2
        assert clock.now() == ist(2026, 10, 19, 10, 2, 0)
