"""The exit side of a rule: trailing stops and exit conditions on open positions.

Scenario: the same as test_engine - a resistance line at 100, BUY when the close crosses above it on SBIN 1H
(bars 0-3 close 98, 99, 100, 101; the entry is bar 3 at 101 with its low, 99.5, as the stop) - and then more
hourly bars arrive. Each bar opens at the previous close and spans 0.5 beyond open and close, so lows and
highs are easy to read off.

Three rules to keep in mind:

* the plan is read from the POSITION, not the rule: it is copied when the position opens, so editing or
  disabling the rule later changes nothing for a trade that is already running;
* a trail only ever tightens the stop, and only on completed candles of ITS OWN timeframe;
* the candle the position was opened on does not count: the plan starts with the next one.
"""
import numpy as np
import pytest

from tests.terminal.helpers import bar, hourly_bars, ist
from tests.terminal.test_engine import BAR3_END, BAR3_START, CLOSES, SYMBOL, Harness
from tests.terminal.test_rules import payload
from trading_service.terminal.candles import Candle
from trading_service.terminal.conditions import ConditionError, EvalContext, compute_trail, validate_trail
from trading_service.terminal.drawings import Drawing, DrawingRepo
from trading_service.terminal.paper import PaperBook
from trading_service.terminal.rules import RuleRepo, referenced_ids, rule_from_payload
from trading_service.terminal.timeframes import bucket_end

CANDLE_TRAIL = {"type": "candle"}
BELOW_102 = {"type": "cross", "direction": "below", "a": {"kind": "close"}, "b": {"kind": "value", "value": 102}}


class Run(Harness):
    """The engine harness, plus a way to let the next hourly bar arrive."""

    def __init__(self, db, rule_over=None):
        super().__init__(db, rule_over=rule_over)
        self.closes = list(CLOSES)

    def entered(self):
        events = self.close_bar()
        assert [e["type"] for e in events] == ["signal", "position"]
        return events

    def next_bar(self, close, timeframe="1H"):
        self.closes.append(close)
        self.bars = hourly_bars(self.closes)
        start = self.bars[-1].time
        self.now = bucket_end(start, "1H", "NSE")
        return self.engine.on_candle_close(SYMBOL, timeframe, start)

    @property
    def position(self):
        [p] = self.book.open_positions()
        return p


# ---------------------------------------------------------------- validation
class TestTrailValidation:
    @pytest.mark.parametrize("raw, expected", [
        ({"type": "candle"}, {"type": "candle", "bars": 1, "buffer_pct": 0.0}),
        ({"type": "candle", "bars": 3, "buffer_pct": 0.2, "timeframe": "30m"}, {"type": "candle", "timeframe": "30m", "bars": 3, "buffer_pct": 0.2}),
        ({"type": "atr"}, {"type": "atr", "period": 14, "mult": 2.0}),
        ({"type": "pct", "value": 1.5}, {"type": "pct", "value": 1.5}),
        ({"type": "line", "drawing_id": "d1"}, {"type": "line", "drawing_id": "d1"}),
        ({"type": "indicator", "name": "ema", "params": {"period": 20}}, {"type": "indicator", "name": "ema", "params": {"period": 20.0}, "output": "value"}),
    ])
    def test_valid_trails_are_normalised(self, raw, expected):
        got = validate_trail(raw)
        assert got["type"] == expected["type"]
        for key, value in expected.items():
            assert got[key] == value

    def test_no_trail_is_fine(self):
        assert validate_trail(None) is None

    @pytest.mark.parametrize("raw, message", [
        ("candle", "must be an object"),
        ({"type": "magic"}, "trail type must be one of"),
        ({"type": "pct"}, "'value' must be a number above 0"),
        ({"type": "pct", "value": -2}, "'value' must be a number above 0"),
        ({"type": "atr", "mult": 0}, "'mult'"),
        ({"type": "candle", "bars": 0}, "'bars'"),
        ({"type": "candle", "bars": 51}, "between 1 and 50"),
        ({"type": "candle", "buffer_pct": -1}, "buffer_pct"),
        ({"type": "line"}, "needs a drawing_id"),
        ({"type": "indicator", "name": "nope"}, "nope"),
        ({"type": "pct", "value": 1, "timeframe": "7m"}, "unknown timeframe"),
    ])
    def test_bad_trails_say_what_is_wrong(self, raw, message):
        with pytest.raises((ConditionError, ValueError), match=message):
            validate_trail(raw)

    def test_a_rule_carries_its_exit_plan(self):
        rule = rule_from_payload(payload(trail={"type": "candle", "timeframe": "15m"}, exit_condition=BELOW_102, exit_timeframe="1D"))
        assert rule.trail["timeframe"] == "15m" and rule.exit_condition == BELOW_102 and rule.exit_timeframe == "1D"
        assert rule.to_json()["exit_timeframe"] == "1D"

    def test_a_rule_without_an_exit_plan_has_none(self):
        rule = rule_from_payload(payload())
        assert (rule.trail, rule.exit_condition, rule.exit_timeframe) == (None, None, None)
        assert rule.exit_plan() == {"trail": None, "exit_condition": None, "exit_timeframe": "1H"}

    def test_the_trail_follows_the_rule_timeframe_unless_it_names_its_own(self):
        assert rule_from_payload(payload(trail=CANDLE_TRAIL)).exit_plan()["trail"]["timeframe"] == "1H"
        assert rule_from_payload(payload(trail={"type": "candle", "timeframe": "15m"})).exit_plan()["trail"]["timeframe"] == "15m"

    def test_a_bad_exit_condition_is_rejected(self):
        with pytest.raises(Exception, match="unknown condition type"):
            rule_from_payload(payload(exit_condition={"type": "nope"}))

    def test_lines_used_by_the_trail_or_the_exit_condition_are_referenced(self):
        rule = rule_from_payload(payload(
            trail={"type": "line", "drawing_id": "trail-line"},
            exit_condition={"type": "cross", "direction": "below", "a": {"kind": "close"}, "b": {"kind": "line", "drawing_id": "exit-line"}},
        ))
        assert referenced_ids(rule) == {"res", "trail-line", "exit-line"}

    def test_the_exit_plan_is_stored_with_the_rule(self, db):
        rule = RuleRepo(db).save(rule_from_payload(payload(trail=CANDLE_TRAIL, exit_condition=BELOW_102, exit_timeframe="15m")))
        got = RuleRepo(db).get(rule.id)
        assert (got.trail["type"], got.exit_condition, got.exit_timeframe) == ("candle", BELOW_102, "15m")


# --------------------------------------------------------- what a trail computes
class TestComputeTrail:
    BARS = hourly_bars([100.0, 102.0, 104.0, 103.0, 106.0, 105.0, 107.0, 108.0])

    def ctx(self, bars=None, drawings=None):
        return EvalContext(bars or self.BARS, "1H", drawings)

    def test_candle_trails_the_low_of_the_last_bar_for_a_buy(self):
        # last bar: open 107 close 108 -> low 106.5
        assert compute_trail(validate_trail({"type": "candle"}), "BUY", self.ctx()) == pytest.approx(106.5)

    def test_candle_over_several_bars_uses_the_lowest_low(self):
        # last three bars open at 106/105/107 and close at 105/107/108 -> lows 104.5, 104.5, 106.5
        assert compute_trail(validate_trail({"type": "candle", "bars": 3}), "BUY", self.ctx()) == pytest.approx(104.5)

    def test_candle_buffer_moves_it_further_away(self):
        got = compute_trail(validate_trail({"type": "candle", "buffer_pct": 1}), "BUY", self.ctx())
        assert got == pytest.approx(106.5 * 0.99)

    def test_a_sell_trails_the_high_of_the_last_bar(self):
        assert compute_trail(validate_trail({"type": "candle"}), "SELL", self.ctx()) == pytest.approx(108.5)

    def test_percent_is_taken_from_the_last_close(self):
        assert compute_trail(validate_trail({"type": "pct", "value": 2}), "BUY", self.ctx()) == pytest.approx(108 * 0.98)
        assert compute_trail(validate_trail({"type": "pct", "value": 2}), "SELL", self.ctx()) == pytest.approx(108 * 1.02)

    def test_atr_is_a_multiple_of_range_below_the_close(self):
        from trading_service.terminal import indicators as ind
        highs = np.array([b.high for b in self.BARS]); lows = np.array([b.low for b in self.BARS]); closes = np.array([b.close for b in self.BARS])
        atr = ind.atr(highs, lows, closes, 3)[-1]
        assert 2 <= atr <= 3.5                                    # every bar spans 2-4 points, so a sane ATR sits in between
        assert compute_trail(validate_trail({"type": "atr", "period": 3, "mult": 2}), "BUY", self.ctx()) == pytest.approx(108 - 2 * atr)
        assert compute_trail(validate_trail({"type": "atr", "period": 3, "mult": 2}), "SELL", self.ctx()) == pytest.approx(108 + 2 * atr)

    def test_atr_without_enough_history_is_unknown(self):
        assert compute_trail(validate_trail({"type": "atr", "period": 50}), "BUY", self.ctx()) is None

    def test_an_indicator_trail_is_its_value(self):
        from trading_service.terminal import indicators as ind
        spec = validate_trail({"type": "indicator", "name": "ema", "params": {"period": 3}})
        closes = np.array([b.close for b in self.BARS])
        expected = ind.compute("ema", {"period": 3}, closes, closes, closes)["value"][-1]
        assert compute_trail(spec, "BUY", self.ctx()) == pytest.approx(expected)

    def test_a_line_trail_is_the_lines_price_at_the_last_bar(self):
        line = Drawing(SYMBOL, "horizontal_line", [{"time": 1, "price": 105.0}], id="floor")
        assert compute_trail(validate_trail({"type": "line", "drawing_id": "floor"}), "BUY", self.ctx(drawings={"floor": line})) == 105.0

    def test_a_trail_can_follow_a_zone_edge(self):
        zone = Drawing(SYMBOL, "rectangle", [{"time": 1, "price": 99.0}, {"time": 2, "price": 106.0}], id="zone")
        spec = validate_trail({"type": "line", "drawing_id": "zone", "edge": "top"})
        assert spec["edge"] == "top"
        assert compute_trail(spec, "BUY", self.ctx(drawings={"zone": zone})) == 106.0
        assert compute_trail(validate_trail({"type": "line", "drawing_id": "zone"}), "BUY", self.ctx(drawings={"zone": zone})) is None

    def test_a_deleted_line_is_unknown_not_an_error(self):
        assert compute_trail(validate_trail({"type": "line", "drawing_id": "gone"}), "BUY", self.ctx()) is None


# ------------------------------------------------------------ trailing in action
class TestTrailing:
    def test_the_stop_climbs_with_each_completed_candle(self, db):
        h = Run(db, {"trail": CANDLE_TRAIL})
        h.entered()
        assert h.position.stop == 99.5

        [event] = h.next_bar(104.0)               # low 100.5
        assert event["type"] == "position" and event["position"]["stop"] == 100.5
        assert h.position.stop == 100.5
        h.next_bar(103.0)                          # low 102.5
        assert h.position.stop == 102.5

    def test_the_stop_never_loosens(self, db):
        h = Run(db, {"trail": CANDLE_TRAIL})
        h.entered()
        h.next_bar(104.0)
        h.next_bar(103.0)
        assert h.next_bar(102.9) == []             # low 102.4 would loosen 102.5: ignored, nothing to broadcast
        assert h.position.stop == 102.5

    def test_the_candle_the_trade_opened_on_does_not_trail(self, db):
        h = Run(db, {"trail": CANDLE_TRAIL})
        events = h.entered()
        assert events[1]["position"]["stop"] == 99.5          # no third event: nothing moved on the entry bar
        assert h.close_bar() == []                             # even replayed

    def test_the_initial_stop_is_remembered_for_the_chart(self, db):
        h = Run(db, {"trail": CANDLE_TRAIL})
        h.entered()
        h.next_bar(104.0)
        position = h.position.to_json()
        assert (position["stop"], position["initial_stop"]) == (100.5, 99.5)
        assert position["trail"]["type"] == "candle" and position["trail"]["timeframe"] == "1H"

    def test_the_trailed_stop_is_what_a_tick_is_checked_against(self, db):
        h = Run(db, {"trail": CANDLE_TRAIL})
        h.entered()
        h.next_bar(104.0)
        h.next_bar(103.0)                          # stop 102.5
        assert h.book.on_tick(SYMBOL, 103.0) == []
        [closed] = h.book.on_tick(SYMBOL, 102.4)
        assert (closed.exit_reason, closed.exit_price) == ("stop", 102.4)
        assert closed.realized_pnl == pytest.approx((102.4 - 101.0) * 10)   # still a profit, locked in by the trail

    def test_a_trail_that_would_stop_the_trade_out_at_once_is_not_applied(self, db):
        # EMA above the close: as a BUY stop it would sit above price.
        h = Run(db, {"trail": {"type": "indicator", "name": "ema", "params": {"period": 3}}})
        h.entered()
        assert h.next_bar(90.0) == []
        assert h.position.stop == 99.5

    def test_the_moved_stop_survives_a_restart(self, db):
        h = Run(db, {"trail": CANDLE_TRAIL})
        h.entered()
        h.next_bar(104.0)
        reborn = PaperBook(db)
        [p] = reborn.open_positions()
        assert (p.stop, p.initial_stop, p.trail["type"]) == (100.5, 99.5, "candle")

    def test_a_sell_trail_only_comes_down(self, db):
        h = Run(db, {
            "side": "SELL", "trail": CANDLE_TRAIL,
            "condition": {"type": "cross", "direction": "below", "a": {"kind": "close"}, "b": {"kind": "line", "drawing_id": "res"}},
        })
        h.closes = [102.0, 101.0, 100.0, 99.0]
        h.bars = hourly_bars(h.closes)
        h.entered()
        assert h.position.stop == 100.5                         # the entry bar's high
        h.next_bar(96.0)                                         # open 99 -> high 99.5
        assert h.position.stop == 99.5
        assert h.next_bar(100.0) == []                           # open 96 close 100 -> high 100.5 would loosen it
        assert h.position.stop == 99.5

    def test_the_trail_works_on_its_own_timeframe(self, db):
        h = Run(db, {"trail": {"type": "candle", "timeframe": "15m"}})
        h.entered()
        assert h.next_bar(104.0) == [] and h.position.stop == 99.5     # a 1H close is not a 15m close

        m15 = [bar(ist(2026, 10, 19, 13, 15), 101, 103, 100.8, 102.5), bar(ist(2026, 10, 19, 13, 30), 102.5, 104, 102.0, 103.5)]
        h.now = ist(2026, 10, 19, 13, 45)
        h.engine.candles = lambda s, tf, n: m15 if tf == "15m" else h.bars
        [event] = h.engine.on_candle_close(SYMBOL, "15m", ist(2026, 10, 19, 13, 30))
        assert event["position"]["stop"] == 102.0                       # low of the last COMPLETED 15m bar

    def test_the_plan_is_copied_so_editing_or_disabling_the_rule_changes_nothing(self, db):
        h = Run(db, {"trail": CANDLE_TRAIL})
        h.entered()
        RuleRepo(db).save(rule_from_payload(payload(trail=None), rule_id=h.rule.id))
        RuleRepo(db).set_enabled(h.rule.id, False)
        h.next_bar(104.0)
        assert h.position.stop == 100.5

    def test_a_trail_on_a_deleted_line_is_skipped_quietly(self, db):
        DrawingRepo(db).save(Drawing(SYMBOL, "horizontal_line", [{"time": 1, "price": 99.0}], id="floor"))
        h = Run(db, {"trail": {"type": "line", "drawing_id": "floor"}})
        h.entered()
        DrawingRepo(db).delete("floor")
        assert h.next_bar(104.0) == []
        assert h.position.stop == 99.5

    def test_no_trail_means_the_stop_stays_put(self, db):
        h = Run(db)
        h.entered()
        assert h.next_bar(110.0) == []
        assert h.position.stop == 99.5


# -------------------------------------------------------------- exit conditions
class TestExitCondition:
    def test_the_position_closes_at_that_candles_close_when_the_exit_fires(self, db):
        h = Run(db, {"exit_condition": BELOW_102})
        h.entered()
        assert h.next_bar(104.0) == []
        [event] = h.next_bar(101.5)                # crossed below 102
        p = event["position"]
        assert (p["status"], p["exit_reason"], p["exit_price"]) == ("closed", "exit_rule", 101.5)
        assert p["realized_pnl"] == pytest.approx(5.0)
        assert h.book.open_positions() == []
        assert p["closed_at"] == int(bucket_end(h.bars[-1].time, "1H", "NSE").timestamp())

    def test_it_waits_for_its_condition(self, db):
        h = Run(db, {"exit_condition": BELOW_102})
        h.entered()
        assert h.next_bar(104.0) == [] and h.next_bar(103.0) == []
        assert len(h.book.open_positions()) == 1

    def test_the_entry_candle_cannot_trigger_the_exit(self, db):
        h = Run(db, {"exit_condition": {"type": "compare", "op": ">", "a": {"kind": "close"}, "b": {"kind": "value", "value": 50}}})
        h.entered()
        assert len(h.book.open_positions()) == 1               # true from the start, but the plan begins next candle
        [event] = h.next_bar(102.0)
        assert event["position"]["exit_reason"] == "exit_rule"

    def test_it_is_checked_on_its_own_timeframe(self, db):
        h = Run(db, {"exit_condition": BELOW_102, "exit_timeframe": "15m"})
        h.entered()
        assert h.next_bar(101.0) == []                          # 1H closes do not count; it is below 102 but not a 15m close
        m15 = [bar(ist(2026, 10, 19, 13, 15), 101, 104, 101, 103), bar(ist(2026, 10, 19, 13, 30), 103, 103, 101, 101.2)]
        h.now = ist(2026, 10, 19, 13, 45)
        h.engine.candles = lambda s, tf, n: m15 if tf == "15m" else h.bars
        [event] = h.engine.on_candle_close(SYMBOL, "15m", ist(2026, 10, 19, 13, 30))
        assert event["position"]["exit_price"] == 101.2

    def test_a_line_based_exit(self, db):
        DrawingRepo(db).save(Drawing(SYMBOL, "horizontal_line", [{"time": 1, "price": 102.0}], id="support"))
        cond = {"type": "cross", "direction": "below", "a": {"kind": "close"}, "b": {"kind": "line", "drawing_id": "support"}}
        h = Run(db, {"exit_condition": cond})
        h.entered()
        h.next_bar(104.0)
        [event] = h.next_bar(101.0)
        assert event["position"]["exit_reason"] == "exit_rule"

    def test_exit_wins_over_the_trail_on_the_same_candle(self, db):
        h = Run(db, {"exit_condition": BELOW_102, "trail": CANDLE_TRAIL})
        h.entered()
        h.next_bar(104.0)
        [event] = h.next_bar(101.5)
        assert event["position"]["status"] == "closed"

    def test_a_deleted_line_in_the_exit_leaves_the_position_alone(self, db):
        DrawingRepo(db).save(Drawing(SYMBOL, "horizontal_line", [{"time": 1, "price": 102.0}], id="support"))
        cond = {"type": "cross", "direction": "below", "a": {"kind": "close"}, "b": {"kind": "line", "drawing_id": "support"}}
        h = Run(db, {"exit_condition": cond})
        h.entered()
        DrawingRepo(db).delete("support")
        h.next_bar(104.0)
        assert h.next_bar(101.0) == [] and len(h.book.open_positions()) == 1

    def test_the_stop_and_target_still_work_alongside(self, db):
        h = Run(db, {"exit_condition": BELOW_102})
        h.entered()
        [closed] = h.book.on_tick(SYMBOL, 99.0)
        assert closed.exit_reason == "stop"
