"""Resampling finer bars into coarser ones, and dropping the forming bar."""
from datetime import timedelta

from trading_service.terminal.candles import Candle, completed, resample
from tests.terminal.helpers import bar, ist

OPEN = ist(2026, 10, 19, 9, 15)


def minutes(*ohlcv):
    return [bar(OPEN + timedelta(minutes=i), *x) for i, x in enumerate(ohlcv)]


def test_five_one_minute_bars_make_one_five_minute_bar():
    bars = minutes(
        (100, 102, 99, 101, 10),
        (101, 105, 100, 104, 20),
        (104, 106, 103, 105, 30),
        (105, 105, 98, 99, 40),
        (99, 101, 97, 100, 50),
    )
    [five] = resample(bars, "5m")
    assert five == Candle(OPEN, open=100, high=106, low=97, close=100, volume=150)


def test_buckets_split_on_the_session_clock():
    bars = [bar(OPEN + timedelta(minutes=m), 1, 1, 1, 1, 1) for m in (0, 4, 5, 9, 10)]
    starts = [c.time for c in resample(bars, "5m")]
    assert starts == [OPEN, OPEN + timedelta(minutes=5), OPEN + timedelta(minutes=10)]


def test_daily_bars_roll_up_into_a_weekly_bar():
    days = [bar(ist(2026, 10, 19 + i, 9, 15), 10 + i, 20 + i, 5 + i, 12 + i, 1000) for i in range(5)]
    [week] = resample(days, "1W")
    assert (week.open, week.high, week.low, week.close, week.volume) == (10, 24, 5, 16, 5000)
    assert week.time == ist(2026, 10, 19, 9, 15)


def test_completed_drops_the_bar_that_is_still_forming():
    hourly = [bar(ist(2026, 10, 19, 9, 15), 1, 1, 1, 1), bar(ist(2026, 10, 19, 10, 15), 1, 1, 1, 1)]
    # At 10:40 the 09:15 bar is finished but the 10:15 bar is not.
    assert completed(hourly, "1H", ist(2026, 10, 19, 10, 40), "NSE:SBIN-EQ") == hourly[:1]
    assert completed(hourly, "1H", ist(2026, 10, 19, 11, 15), "NSE:SBIN-EQ") == hourly


def test_resample_of_nothing_is_empty():
    assert resample([], "5m") == []


def test_candle_json_uses_epoch_seconds():
    c = bar(OPEN, 1, 2, 0.5, 1.5, 7)
    assert c.to_json() == {"time": int(OPEN.timestamp()), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 7}
