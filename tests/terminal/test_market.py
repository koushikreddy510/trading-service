"""CandleService: how stored 1-minute bars and daily rows become candles of any timeframe."""
from datetime import timedelta

import pytest

from tests.terminal.helpers import ist
from trading_service.terminal.builder import CandleBuilder
from trading_service.terminal.candles import Candle
from trading_service.terminal.feed.base import BrokerError
from trading_service.terminal.feed.mock import MockHistory
from trading_service.terminal.market import CandleService
from trading_service.terminal.ticks import Tick

SYM = "NSE:SBIN-EQ"
OPEN = ist(2026, 10, 19, 9, 15)


def minute(i, close, base=OPEN):
    t = base + timedelta(minutes=i)
    return Candle(t, close - 0.5, close + 1, close - 1, close, 10)


@pytest.fixture
def now():
    return [ist(2026, 10, 19, 11, 0, 30)]


@pytest.fixture
def service(db, now):
    return CandleService(db, clock=lambda: now[0])


def test_minutes_round_trip_through_the_database(service):
    service.minutes.save(SYM, [minute(0, 100), minute(1, 101)])
    assert [c.close for c in service.candles(SYM, "1m")] == [100, 101]


def test_saving_a_minute_twice_replaces_it(service):
    service.minutes.save(SYM, [minute(0, 100)])
    service.minutes.save(SYM, [minute(0, 105)])
    (bar,) = service.candles(SYM, "1m")
    assert bar.close == 105


def test_higher_timeframes_are_resampled_from_the_minutes(service):
    service.minutes.save(SYM, [minute(i, 100 + i) for i in range(10)])    # 09:15 .. 09:24
    five = service.candles(SYM, "5m")
    assert [c.time for c in five] == [OPEN, OPEN + timedelta(minutes=5)]
    first = five[0]
    assert (first.open, first.close, first.volume) == (99.5, 104, 50)
    assert first.high == 105 and first.low == 99


def test_one_hour_bars_start_at_the_session_open_not_on_the_hour(service):
    service.minutes.save(SYM, [minute(i, 100) for i in range(0, 120)])    # 09:15 .. 11:14
    assert [c.time for c in service.candles(SYM, "1H")] == [OPEN, OPEN + timedelta(hours=1)]


def test_the_forming_minute_is_included_from_the_builder_but_not_stored(db, now):
    builder = CandleBuilder(lambda s, c: None, lambda: [SYM])
    svc = CandleService(db, builder=builder, clock=lambda: now[0])
    svc.minutes.save(SYM, [minute(0, 100)])
    builder.on_tick(Tick(SYM, 107, ist(2026, 10, 19, 9, 16, 20)))

    bars = svc.candles(SYM, "1m")
    assert [c.close for c in bars] == [100, 107]
    assert len(svc.minutes.load(SYM, OPEN)) == 1


def test_limit_returns_the_newest_bars(service):
    service.minutes.save(SYM, [minute(i, 100 + i) for i in range(30)])
    assert [c.close for c in service.candles(SYM, "1m", limit=3)] == [127, 128, 129]


def test_unknown_timeframe_is_rejected(service):
    with pytest.raises(ValueError):
        service.candles(SYM, "7m")


class TestDaily:
    def add_day(self, db, symbol, day, o, h, l, c, v=1000):
        db.execute(
            "INSERT INTO ohlcv_1d (tradingsymbol, date, open, high, low, close, volume) VALUES (:s,:d,:o,:h,:l,:c,:v)",
            s=symbol, d=day, o=o, h=h, l=l, c=c, v=v,
        )

    def test_daily_bars_come_from_the_data_stores_table(self, db, service):
        from datetime import date

        self.add_day(db, SYM, date(2026, 10, 15), 100, 110, 95, 105)
        self.add_day(db, SYM, date(2026, 10, 16), 105, 112, 101, 111)
        bars = service.candles(SYM, "1D")
        assert [b.close for b in bars] == [105, 111]
        assert bars[0].time == ist(2026, 10, 15, 9, 15)

    def test_todays_bar_is_built_live_from_the_minutes(self, db, service):
        from datetime import date

        self.add_day(db, SYM, date(2026, 10, 16), 105, 112, 101, 111)
        service.minutes.save(SYM, [minute(0, 120), minute(1, 125)])
        bars = service.candles(SYM, "1D")
        assert [b.close for b in bars] == [111, 125]
        assert bars[-1].high == 126 and bars[-1].volume == 20

    def test_a_stale_row_for_today_is_overridden_by_live_minutes(self, db, service):
        from datetime import date

        self.add_day(db, SYM, date(2026, 10, 19), 1, 1, 1, 1)
        service.minutes.save(SYM, [minute(0, 120)])
        assert [b.close for b in service.candles(SYM, "1D")] == [120]

    def test_a_symbol_without_daily_rows_still_has_todays_bar(self, service):
        service.minutes.save("MCX:SILVERM", [minute(0, 70000, base=ist(2026, 10, 19, 9, 0))])
        assert len(service.candles("MCX:SILVERM", "1D")) == 1

    def test_weekly_bars_are_resampled_from_daily(self, db, service):
        from datetime import date

        # Mon 12 .. Fri 16 Oct, then Mon 19 (today, no minutes yet)
        for i, d in enumerate([12, 13, 14, 15, 16]):
            self.add_day(db, SYM, date(2026, 10, d), 100 + i, 110 + i, 90 + i, 105 + i)
        weeks = service.candles(SYM, "1W")
        assert len(weeks) == 1
        w = weeks[0]
        assert (w.open, w.high, w.low, w.close, w.volume) == (100, 114, 90, 109, 5000)
        assert w.time == ist(2026, 10, 12, 9, 15)


class TestBackfill:
    def test_without_a_history_provider_nothing_happens(self, service):
        assert service.backfill(SYM) == 0

    def test_fills_from_the_broker_up_to_but_excluding_the_forming_minute(self, db, now):
        svc = CandleService(db, history=MockHistory(), clock=lambda: now[0])    # now = 11:00:30
        stored = svc.backfill(SYM, days=1)
        assert stored > 0
        # the clock says 11:00:30, so the 11:00 minute is still forming and must not be stored
        assert svc.minutes.latest(SYM) == ist(2026, 10, 19, 10, 59)

    def test_only_the_missing_gap_is_requested_next_time(self, db, now):
        calls = []

        class Spy(MockHistory):
            def fetch_1m(self, symbol, start, end):
                calls.append((start, end))
                return super().fetch_1m(symbol, start, end)

        svc = CandleService(db, history=Spy(), clock=lambda: now[0])
        svc.backfill(SYM, days=1)
        now[0] = ist(2026, 10, 19, 11, 5, 30)
        svc.backfill(SYM, days=1)

        assert calls[1][0] == ist(2026, 10, 19, 11, 0)      # resumes right after the last stored minute (10:59)
        assert calls[1][1] == ist(2026, 10, 19, 11, 5)

    def test_broker_errors_do_not_propagate(self, db, now):
        class Broken:
            def fetch_1m(self, *a):
                raise BrokerError("token expired")

        assert CandleService(db, history=Broken(), clock=lambda: now[0]).backfill(SYM) == 0
