"""How bars are cut and when a bar counts as completed."""
import pytest

from trading_service.terminal import timeframes as tfm
from tests.terminal.helpers import ist

MON = (2026, 10, 19)  # a Monday


def test_nse_hourly_bars_follow_the_session_open_not_the_clock_hour():
    # 09:20 belongs to the 09:15-10:15 bar, not to a 09:00-10:00 bar.
    start = tfm.bucket_start(ist(*MON, 9, 20), "1H")
    assert start == ist(*MON, 9, 15)
    assert tfm.bucket_end(start, "1H") == ist(*MON, 10, 15)


def test_the_last_bar_of_the_session_is_shorter_than_its_nominal_length():
    start = tfm.bucket_start(ist(*MON, 14, 0), "4H")
    assert start == ist(*MON, 13, 15)
    assert tfm.bucket_end(start, "4H") == ist(*MON, 15, 30)  # clamped at the close, not 17:15


def test_mcx_runs_a_longer_session_so_its_buckets_differ_from_nse():
    assert tfm.bucket_start(ist(*MON, 22, 0), "4H", "MCX") == ist(*MON, 21, 0)
    assert tfm.bucket_end(ist(*MON, 21, 0), "4H", "MCX") == ist(*MON, 23, 30)


def test_daily_bar_spans_the_whole_session():
    start = tfm.bucket_start(ist(*MON, 11, 0), "1D")
    assert start == ist(*MON, 9, 15)
    assert tfm.bucket_end(start, "1D") == ist(*MON, 15, 30)


def test_weekly_bar_starts_monday_and_ends_friday_close():
    wednesday = ist(2026, 10, 21, 12, 0)
    start = tfm.bucket_start(wednesday, "1W")
    assert start == ist(2026, 10, 19, 9, 15)
    assert tfm.bucket_end(start, "1W") == ist(2026, 10, 23, 15, 30)


def test_a_time_before_the_open_is_clamped_into_the_first_bar():
    assert tfm.bucket_start(ist(*MON, 9, 0), "15m") == ist(*MON, 9, 15)


@pytest.mark.parametrize(
    "when, expected",
    [
        (ist(*MON, 9, 14, 59), False),   # before the open
        (ist(*MON, 9, 15), True),        # open is inclusive
        (ist(*MON, 15, 29, 59), True),
        (ist(*MON, 15, 30), False),      # close is exclusive
        (ist(2026, 10, 24, 11, 0), False),  # Saturday
    ],
)
def test_in_session(when, expected):
    assert tfm.in_session(when, "NSE") is expected


def test_a_bar_is_complete_only_once_its_end_has_passed():
    start = ist(*MON, 9, 15)
    assert not tfm.is_complete(start, "1H", ist(*MON, 10, 14, 59))
    assert tfm.is_complete(start, "1H", ist(*MON, 10, 15))


def test_exchange_is_read_from_the_symbol_prefix():
    assert tfm.exchange_of("MCX:SILVERM") == "MCX"
    assert tfm.exchange_of("NSE:SBIN-EQ") == "NSE"
    assert tfm.exchange_of("SBIN") == "NSE"  # bare symbols default to NSE


def test_unknown_timeframes_are_rejected():
    with pytest.raises(ValueError, match="unknown timeframe"):
        tfm.validate_timeframe("7m")
    assert tfm.TIMEFRAMES == ["1m", "5m", "15m", "30m", "1H", "2H", "4H", "1D", "1W"]
