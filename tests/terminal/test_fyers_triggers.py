"""Triggers borrowed from Fyers Automate that our conditions did not have: range breakouts and time windows.

* `highest` / `lowest` - the extreme of the previous N bars, so "close crosses above the 20-bar high" is a
  plain `cross`. The current bar is never part of its own window (it could never be crossed otherwise).
* `time_between` - true when the candle being judged CLOSED inside a clock window (IST). Combine it with
  `all` to only enter during the session's good hours, or use it as an exit condition to square off at 15:15.

hourly_bars(): seven NSE bars a day, opening 09:15 ... 15:15, each spanning +-0.5 around open/close.
"""
import numpy as np
import pytest

from tests.terminal.helpers import bar, hourly_bars, ist
from tests.terminal.test_exits import Run
from trading_service.terminal.candles import Candle
from trading_service.terminal.conditions import ConditionError, EvalContext, evaluate, validate_condition, validate_operand

CLOSE = {"kind": "close"}


def cross(direction, a, b):
    return validate_condition({"type": "cross", "direction": direction, "a": a, "b": b})


def fires_at(cond, bars, tf="1H", exchange="NSE"):
    return [i for i in range(len(bars)) if evaluate(cond, EvalContext(bars[: i + 1], tf, {}, exchange)).fired]


# ------------------------------------------------------------ highest / lowest
class TestRangeOperands:
    def test_defaults_follow_the_name(self):
        assert validate_operand({"kind": "highest"}) == {"kind": "highest", "bars": 20, "source": "high"}
        assert validate_operand({"kind": "lowest", "bars": 5}) == {"kind": "lowest", "bars": 5, "source": "low"}
        assert validate_operand({"kind": "highest", "bars": 10, "source": "close"})["source"] == "close"

    @pytest.mark.parametrize("raw, fragment", [
        ({"kind": "highest", "bars": 0}, "'bars'"),
        ({"kind": "highest", "bars": 501}, "'bars'"),
        ({"kind": "highest", "bars": 2.5}, "'bars'"),
        ({"kind": "highest", "bars": True}, "'bars'"),
        ({"kind": "lowest", "source": "vwap"}, "'source'"),
    ])
    def test_bad_ranges_are_rejected(self, raw, fragment):
        with pytest.raises(ConditionError, match=fragment):
            validate_operand(raw)

    def test_each_bar_sees_the_extreme_of_the_bars_before_it(self):
        ctx = EvalContext(hourly_bars([10, 12, 11, 9, 13]), "1H")
        # highs are close/open + 0.5: bars -> 10.5, 12.5, 12.5(open 12, close 11), 11.5(open 11, close 9) ...
        highest = ctx.series({"kind": "highest", "bars": 2, "source": "high"})
        assert np.isnan(highest[:2]).all()                       # not enough history yet
        assert highest[2] == max(ctx.high[0:2]) and highest[3] == max(ctx.high[1:3]) and highest[4] == max(ctx.high[2:4])
        lowest = ctx.series({"kind": "lowest", "bars": 2, "source": "low"})
        assert lowest[4] == min(ctx.low[2:4])

    def test_a_window_longer_than_the_history_is_all_unknown(self):
        out = EvalContext(hourly_bars([1, 2, 3]), "1H").series({"kind": "highest", "bars": 3, "source": "high"})
        assert np.isnan(out).all()

    def test_the_current_bar_is_not_in_its_own_window(self):
        ctx = EvalContext(hourly_bars([10, 10, 10, 50]), "1H")
        assert ctx.series({"kind": "highest", "bars": 3, "source": "high"})[3] == 10.5       # the 50 is not counted

    def test_range_breakout_fires_once_on_the_bar_that_clears_the_n_bar_high(self):
        closes = [10, 11, 10.5, 11, 10.8, 12.5, 12.8, 12.9]
        cond = cross("above", CLOSE, {"kind": "highest", "bars": 4, "source": "high"})
        assert fires_at(cond, hourly_bars(closes)) == [5]       # 12.5 beats the previous four bars' highs (<=11.5); the next closes are continuations

    def test_a_breakdown_below_the_n_bar_low(self):
        closes = [20, 19, 19.5, 19, 19.2, 17.5, 17]
        cond = cross("below", CLOSE, {"kind": "lowest", "bars": 4, "source": "low"})
        assert fires_at(cond, hourly_bars(closes)) == [5]

    def test_it_can_be_compared_like_any_other_operand(self):
        cond = validate_condition({"type": "compare", "op": ">", "a": {"kind": "volume"}, "b": {"kind": "highest", "bars": 3, "source": "volume"}})
        bars = [bar(ist(2026, 10, 19, 9 + i, 15), 100, 101, 99, 100, v) for i, v in enumerate([100, 200, 150, 400])]
        assert fires_at(cond, bars) == [3]                      # volume above its previous 3-bar peak

    def test_the_note_names_the_range(self):
        cond = cross("above", CLOSE, {"kind": "highest", "bars": 3, "source": "high"})
        bars = hourly_bars([10, 10, 10, 10, 20])
        assert "highest(3 high)" in evaluate(cond, EvalContext(bars, "1H")).note


# ---------------------------------------------------------------- time_between
def window(start, end):
    return validate_condition({"type": "time_between", "start": start, "end": end})


class TestTimeWindow:
    @pytest.mark.parametrize("raw, fragment", [
        ({"type": "time_between", "start": "9:20", "end": "15:00"}, "look like"),
        ({"type": "time_between", "start": "09:20"}, "'end'"),
        ({"type": "time_between", "start": "25:00", "end": "26:00"}, "look like"),
        ({"type": "time_between", "start": "09:60", "end": "15:00"}, "look like"),
        ({"type": "time_between", "start": "15:00", "end": "09:20"}, "past midnight"),
    ])
    def test_malformed_windows_are_rejected(self, raw, fragment):
        with pytest.raises(ConditionError, match=fragment):
            validate_condition(raw)

    def test_it_is_judged_when_the_candle_closes_not_when_it_opens(self):
        bars = hourly_bars([10] * 7)                    # open 09:15 ... 15:15, so they close 10:15 ... 15:30
        assert fires_at(window("10:15", "11:15"), bars) == [0, 1]
        assert fires_at(window("09:15", "10:00"), bars) == []            # the first candle only closes at 10:15

    def test_the_last_candle_of_the_session_closes_at_the_session_end_not_an_hour_later(self):
        bars = hourly_bars([10] * 7)
        assert fires_at(window("15:25", "15:30"), bars) == [6]           # 15:15 + 60m would be 16:15; the market shuts at 15:30

    def test_both_ends_are_inclusive(self):
        assert fires_at(window("11:15", "11:15"), hourly_bars([10] * 7)) == [1]

    def test_mcx_runs_later(self):
        bars = [Candle(ist(2026, 10, 19, 22, 0), 100, 101, 99, 100, 1), Candle(ist(2026, 10, 19, 23, 0), 100, 101, 99, 100, 1)]
        assert fires_at(window("23:15", "23:30"), bars, exchange="MCX") == [1]   # the 23:00 candle closes with the 23:30 session end; the 22:00 one at 23:00

    def test_a_daily_candle_closes_at_the_session_end(self):
        bars = [Candle(ist(2026, 10, 19, 9, 15), 100, 101, 99, 100, 1)]
        assert fires_at(window("15:30", "15:30"), bars, tf="1D") == [0]

    def test_no_candles_no_fire(self):
        assert not evaluate(window("09:00", "15:30"), EvalContext([], "1H")).fired

    def test_the_note_says_when_it_closed(self):
        note = evaluate(window("10:00", "11:00"), EvalContext(hourly_bars([10]), "1H")).note
        assert note == "candle closed at 10:15 IST, inside 10:00-11:00"

    def test_it_combines_with_a_price_condition(self):
        closes = [10, 11, 10.5, 11, 10.8, 12.5, 13]       # 12.5 breaks out on bar 5, which closes at 14:15
        breakout = cross("above", CLOSE, {"kind": "highest", "bars": 4, "source": "high"})
        bars = hourly_bars(closes)
        both = lambda start, end: validate_condition({"type": "all", "conditions": [breakout, window(start, end)]})  # noqa: E731
        assert fires_at(both("09:00", "15:30"), bars) == [5]
        assert fires_at(both("09:00", "13:30"), bars) == []              # the breakout came too late in the day


# ---------------------------------------------------- inside the engine
class TestSquareOff:
    """The Fyers "square off at 15:15" idea: an exit condition that is only about the clock."""

    def test_a_time_window_exit_closes_the_position_at_that_candles_close(self, db):
        h = Run(db, {"exit_condition": {"type": "time_between", "start": "15:00", "end": "15:30"}})
        h.entered()                                     # entered on the 12:15 bar
        assert h.next_bar(102.0) == []                  # the 13:15 bar closes 14:15: before the window
        [event] = h.next_bar(103.5)                     # the 14:15 bar closes 15:15: inside
        p = event["position"]
        assert (p["status"], p["exit_reason"], p["exit_price"]) == ("closed", "exit_rule", 103.5)

    def test_a_breakout_rule_enters_through_the_engine(self, db):
        # CLOSES are 98, 99, 100, 101: bar 3 closes at 101, above the highs of the two bars before it (99.5, 100.5)
        h = Run(db, {"condition": {"type": "compare", "op": ">", "a": CLOSE, "b": {"kind": "highest", "bars": 2, "source": "high"}}})
        events = h.close_bar()
        assert [e["type"] for e in events] == ["signal", "position"]
