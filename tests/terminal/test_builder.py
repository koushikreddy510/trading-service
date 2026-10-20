"""CandleBuilder: how live ticks become stored 1-minute candles and 'bar closed' events.

The two ideas to keep in mind while reading these tests:

* ticks only touch an in-memory forming bar; nothing is written until the minute is over;
* bar-close events are driven by the CLOCK (`flush(now)`), not by the next tick, so a
  quiet symbol still closes its bars on time and the final bar of the session closes too.
"""
from datetime import timedelta

import pytest

from tests.terminal.helpers import ist
from trading_service.terminal.builder import CandleBuilder
from trading_service.terminal.ticks import Tick

SYM = "NSE:SBIN-EQ"


@pytest.fixture
def stored():
    return []


@pytest.fixture
def builder(stored):
    return CandleBuilder(persist=lambda sym, c: stored.append((sym, c)), tracked=lambda: [SYM])


def tick(h, m, s, price, volume=None, symbol=SYM):
    return Tick(symbol, price, ist(2026, 10, 19, h, m, s), volume=volume)


def test_ticks_inside_one_minute_form_a_single_ohlc_bar(builder, stored):
    for s, p in [(1, 100), (10, 103), (20, 99), (59, 101)]:
        builder.on_tick(tick(10, 0, s, p))

    (forming,) = builder.forming_minutes(SYM)
    assert (forming.open, forming.high, forming.low, forming.close) == (100, 103, 99, 101)
    assert stored == [], "nothing is persisted while the minute is still open"


def test_a_minute_is_persisted_once_its_time_has_passed(builder, stored):
    builder.on_tick(tick(10, 0, 5, 100))
    builder.on_tick(tick(10, 0, 40, 102))

    builder.flush(ist(2026, 10, 19, 10, 0, 59))
    assert stored == []

    builder.flush(ist(2026, 10, 19, 10, 1, 0))
    (sym, candle), = stored
    assert sym == SYM
    assert candle.time == ist(2026, 10, 19, 10, 0)
    assert (candle.open, candle.close) == (100, 102)
    assert builder.forming_minutes(SYM) == []


def test_a_new_minute_starts_a_new_bar_and_opens_at_its_first_tick(builder, stored):
    builder.on_tick(tick(10, 0, 30, 100))
    builder.on_tick(tick(10, 1, 2, 105))
    builder.flush(ist(2026, 10, 19, 10, 1, 3))

    assert [c.close for _, c in stored] == [100]
    (forming,) = builder.forming_minutes(SYM)
    assert forming.time == ist(2026, 10, 19, 10, 1)
    assert forming.open == 105


def test_ticks_outside_the_session_are_ignored(builder):
    builder.on_tick(tick(9, 0, 0, 100))     # pre-open
    builder.on_tick(tick(15, 30, 0, 100))   # at the close: the session is over
    assert builder.forming_minutes(SYM) == []


def test_off_session_ticks_are_kept_when_explicitly_allowed(stored):
    b = CandleBuilder(lambda s, c: stored.append(c), lambda: [SYM], allow_off_session=True)
    b.on_tick(tick(8, 0, 0, 100))
    assert len(b.forming_minutes(SYM)) == 1


class TestVolume:
    """Brokers send CUMULATIVE day volume; the bar needs what traded within the minute."""

    def test_bar_volume_is_the_change_in_cumulative_volume(self, builder):
        builder.on_tick(tick(10, 0, 1, 100, volume=1000))   # first sighting: baseline only
        builder.on_tick(tick(10, 0, 20, 100, volume=1300))
        builder.on_tick(tick(10, 0, 50, 100, volume=1500))
        assert builder.forming_minutes(SYM)[0].volume == 500

    def test_volume_carries_across_minutes_without_double_counting(self, builder, stored):
        builder.on_tick(tick(10, 0, 1, 100, volume=1000))
        builder.on_tick(tick(10, 0, 30, 100, volume=1200))
        builder.on_tick(tick(10, 1, 5, 100, volume=1500))
        builder.flush(ist(2026, 10, 19, 10, 1, 6))
        assert stored[0][1].volume == 200
        assert builder.forming_minutes(SYM)[0].volume == 300

    def test_a_counter_reset_is_not_a_negative_volume(self, builder):
        builder.on_tick(tick(10, 0, 1, 100, volume=9000))
        builder.on_tick(tick(10, 0, 30, 100, volume=40))
        assert builder.forming_minutes(SYM)[0].volume == 40

    def test_ticks_without_volume_give_zero_volume(self, builder):
        builder.on_tick(tick(10, 0, 1, 100))
        assert builder.forming_minutes(SYM)[0].volume == 0


class TestBarCloseEvents:
    def events(self, builder, now):
        return {(e.timeframe, e.bar_start) for e in builder.flush(now)}

    def test_the_first_flush_only_sets_the_clock(self, builder):
        assert builder.flush(ist(2026, 10, 19, 10, 15, 0)) == []

    def test_the_hour_bar_closes_exactly_on_its_boundary(self, builder):
        builder.flush(ist(2026, 10, 19, 10, 14, 30))
        assert ("1H", ist(2026, 10, 19, 9, 15)) not in self.events(builder, ist(2026, 10, 19, 10, 14, 59))

        got = self.events(builder, ist(2026, 10, 19, 10, 15, 1))
        assert ("1H", ist(2026, 10, 19, 9, 15)) in got
        assert ("1m", ist(2026, 10, 19, 10, 14)) in got

    def test_shorter_timeframes_close_on_their_own_boundaries(self, builder):
        builder.flush(ist(2026, 10, 19, 10, 4, 30))
        got = self.events(builder, ist(2026, 10, 19, 10, 5, 1))
        assert ("5m", ist(2026, 10, 19, 10, 0)) in got
        assert ("15m", ist(2026, 10, 19, 9, 45)) not in got   # 15m closes at 10:00, 10:15 ...
        assert not any(tf == "1H" for tf, _ in got)

    def test_each_close_is_reported_once(self, builder):
        builder.flush(ist(2026, 10, 19, 10, 14, 30))
        first = builder.flush(ist(2026, 10, 19, 10, 15, 1))
        second = builder.flush(ist(2026, 10, 19, 10, 15, 30))
        assert first and second == []

    def test_a_late_flush_still_reports_every_boundary_it_skipped(self, builder):
        builder.flush(ist(2026, 10, 19, 10, 0, 30))
        got = self.events(builder, ist(2026, 10, 19, 10, 6, 0))
        assert {("1m", ist(2026, 10, 19, 10, m)) for m in range(0, 6)} <= got
        assert ("5m", ist(2026, 10, 19, 10, 0)) in got

    def test_the_last_bar_of_the_session_closes_without_a_further_tick(self, builder):
        """The 4H bar 13:15-15:30 and the day bar both end at 15:30, when ticks stop."""
        builder.flush(ist(2026, 10, 19, 15, 29, 30))
        got = self.events(builder, ist(2026, 10, 19, 15, 30, 1))
        assert ("4H", ist(2026, 10, 19, 13, 15)) in got
        assert ("1D", ist(2026, 10, 19, 9, 15)) in got
        assert ("1m", ist(2026, 10, 19, 15, 29)) in got

    def test_friday_close_also_closes_the_week(self, builder):
        builder.flush(ist(2026, 10, 23, 15, 29, 30))     # Friday
        got = self.events(builder, ist(2026, 10, 23, 15, 30, 1))
        assert ("1W", ist(2026, 10, 19, 9, 15)) in got

    def test_nothing_closes_outside_the_session(self, builder):
        builder.flush(ist(2026, 10, 19, 16, 0, 0))
        assert self.events(builder, ist(2026, 10, 19, 17, 0, 0)) == set()
        builder.flush(ist(2026, 10, 24, 11, 0, 0))   # Saturday
        assert self.events(builder, ist(2026, 10, 24, 12, 0, 0)) == set()

    def test_only_tracked_symbols_produce_events(self, stored):
        b = CandleBuilder(lambda s, c: stored.append(c), tracked=lambda: [])
        b.flush(ist(2026, 10, 19, 10, 14, 30))
        assert b.flush(ist(2026, 10, 19, 10, 15, 1)) == []

    def test_a_huge_gap_does_not_replay_old_boundaries(self, builder):
        builder.flush(ist(2026, 10, 12, 10, 0, 0))
        assert builder.flush(ist(2026, 10, 19, 10, 0, 0)) == []

    def test_mcx_session_runs_until_23_30(self, stored):
        sym = "MCX:SILVERM"
        b = CandleBuilder(lambda s, c: stored.append(c), lambda: [sym])
        b.flush(ist(2026, 10, 19, 23, 29, 30))
        got = {(e.timeframe, e.bar_start) for e in b.flush(ist(2026, 10, 19, 23, 30, 1))}
        assert ("1D", ist(2026, 10, 19, 9, 0)) in got


def test_bars_are_persisted_before_close_events_are_returned(stored):
    """The rule engine reacts to a CloseEvent by reading stored candles, so the
    finished minute must already be in the store when the event is delivered."""
    seen_at_event_time = []

    def persist(sym, candle):
        seen_at_event_time.append(candle.time)

    b = CandleBuilder(persist, lambda: [SYM])
    b.flush(ist(2026, 10, 19, 10, 14, 30))
    b.on_tick(tick(10, 14, 40, 100))
    events = b.flush(ist(2026, 10, 19, 10, 15, 1))
    assert events and seen_at_event_time == [ist(2026, 10, 19, 10, 14)]
