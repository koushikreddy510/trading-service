"""Entry conditions: validation, 'cross' semantics, combinations, stops and targets."""
import math

import numpy as np
import pytest

from trading_service.terminal import indicators as ind
from trading_service.terminal.conditions import (
    ConditionError, EvalContext, InvalidRisk, compute_stop, compute_target, evaluate, referenced_drawings,
    validate_condition, validate_stop, validate_target,
)
from trading_service.terminal.drawings import Drawing
from trading_service.terminal.timeframes import to_epoch
from tests.terminal.helpers import hourly_bars

SYMBOL = "NSE:SBIN-EQ"
CLOSE = {"kind": "close"}


def level(v):
    return {"kind": "value", "value": v}


def cross(direction, a, b):
    return validate_condition({"type": "cross", "direction": direction, "a": a, "b": b})


def ctx_for(closes, drawings=None):
    return EvalContext(hourly_bars(closes), "1H", drawings or {})


def fired_on_every_prefix(cond, closes):
    """Evaluate the condition as if each bar had just closed; return the indexes where it fires."""
    return [i for i in range(len(closes)) if evaluate(cond, ctx_for(closes[: i + 1])).fired]


# ------------------------------------------------------------------ cross
def test_cross_above_fires_only_on_the_bar_where_price_goes_from_at_or_below_to_above():
    cond = cross("above", CLOSE, level(100))
    assert fired_on_every_prefix(cond, [98, 99, 100, 101, 102, 103]) == [3]   # 100 -> 101 is the transition


def test_a_condition_that_stays_true_does_not_fire_again():
    cond = cross("above", CLOSE, level(100))
    assert evaluate(cond, ctx_for([98, 101, 102])).fired is False             # already above on the previous bar


def test_cross_below_is_the_mirror_image():
    cond = cross("below", CLOSE, level(100))
    assert fired_on_every_prefix(cond, [103, 102, 100, 99, 98]) == [3]


def test_touching_the_level_without_closing_beyond_it_is_not_a_cross():
    cond = cross("above", CLOSE, level(100))
    assert fired_on_every_prefix(cond, [98, 99, 100, 100, 100]) == []


def test_ema_crossover_fires_exactly_once_on_the_transition_bar():
    closes = [100.0] * 30 + [100 + 2 * i for i in range(1, 11)]               # flat, then a rally
    fast = {"kind": "indicator", "name": "ema", "params": {"period": 3}}
    slow = {"kind": "indicator", "name": "ema", "params": {"period": 8}}
    cond = cross("above", fast, slow)
    [i] = fired_on_every_prefix(cond, closes)
    e3, e8 = ind.ema(closes, 3), ind.ema(closes, 8)
    assert e3[i - 1] <= e8[i - 1] and e3[i] > e8[i]


def test_rsi_crossing_a_level():
    closes = [100 - i for i in range(30)] + [71 + 3 * i for i in range(12)]    # selloff then bounce
    rsi_op = {"kind": "indicator", "name": "rsi", "params": {"period": 5}}
    hits = fired_on_every_prefix(cross("above", rsi_op, level(30)), closes)
    assert len(hits) == 1 and hits[0] >= 30


def test_supertrend_flip_is_close_crossing_the_supertrend_line():
    # 40 rising bars (peak at bar 39) then a selloff. Supertrend flips down on bar 43, three bars into it,
    # and never flips up during this series.
    closes = [100 + i for i in range(40)] + [139 - 2 * i for i in range(30)]
    st = {"kind": "indicator", "name": "supertrend", "params": {"period": 10, "multiplier": 3}, "output": "line"}
    assert fired_on_every_prefix(cross("below", CLOSE, st), closes) == [43]
    assert fired_on_every_prefix(cross("above", CLOSE, st), closes) == []


def test_indicator_warmup_never_fires():
    cond = cross("above", CLOSE, {"kind": "indicator", "name": "ema", "params": {"period": 50}})
    assert fired_on_every_prefix(cond, [1, 2, 3, 4, 5, 6]) == []


def test_a_trend_line_breakout():
    # A falling resistance line through bar 2 (110) and bar 6 (106) drops one point per bar: line(i) = 112 - i.
    # Closes sit under it until bar 7 (close 106 > line 105), the single breakout; later bars are already above.
    bars = hourly_bars([100, 101, 105, 107, 108, 107, 105, 106, 107, 109, 110, 111])
    times = [to_epoch(b.time) for b in bars]
    line = Drawing(SYMBOL, "trend_line", [{"time": times[2], "price": 110.0}, {"time": times[6], "price": 106.0}], id="res")
    cond = cross("above", CLOSE, {"kind": "line", "drawing_id": "res"})
    fired = [i for i in range(len(bars)) if evaluate(cond, EvalContext(bars[: i + 1], "1H", {"res": line})).fired]
    assert fired == [7]


def test_a_rule_can_break_out_of_the_top_edge_of_a_zone():
    bars = hourly_bars([100, 101, 103, 104, 106, 107])
    times = [to_epoch(b.time) for b in bars]
    zone = Drawing(SYMBOL, "rectangle", [{"time": times[0], "price": 99.0}, {"time": times[3], "price": 105.0}], id="zone")
    cond = validate_condition(cross("above", CLOSE, {"kind": "line", "drawing_id": "zone", "edge": "top"}))
    assert cond["b"] == {"kind": "line", "drawing_id": "zone", "edge": "top"}
    assert [i for i in range(len(bars)) if evaluate(cond, EvalContext(bars[: i + 1], "1H", {"zone": zone})).fired] == [4]  # 104 -> 106 clears 105


def test_the_bottom_edge_is_a_different_level_from_the_top():
    ctx = ctx_for([100.0] * 4, {"zone": Drawing(SYMBOL, "rectangle", [{"time": 1, "price": 99.0}, {"time": 2, "price": 105.0}], id="zone")})
    assert compute_stop({"type": "line", "drawing_id": "zone", "edge": "bottom"}, "BUY", 100, ctx) == 99.0
    with pytest.raises(InvalidRisk, match="degenerate"):
        compute_stop({"type": "line", "drawing_id": "zone"}, "BUY", 100, ctx)           # no edge chosen: no level


def test_an_unknown_rectangle_edge_is_rejected():
    with pytest.raises(ConditionError, match="edge must be"):
        validate_condition(cross("above", CLOSE, {"kind": "line", "drawing_id": "z", "edge": "middle"}))


def test_a_deleted_line_is_an_error_not_a_silent_no_fire():
    cond = cross("above", CLOSE, {"kind": "line", "drawing_id": "gone"})
    with pytest.raises(ConditionError, match="no longer exists"):
        evaluate(cond, ctx_for([1, 2, 3]))


# ---------------------------------------------------------------- compare / all / any
def test_compare_is_a_state_not_a_transition():
    cond = validate_condition({"type": "compare", "op": ">", "a": CLOSE, "b": level(100)})
    assert evaluate(cond, ctx_for([101, 102, 103])).fired is True
    assert evaluate(cond, ctx_for([101, 102, 99])).fired is False


def test_all_requires_every_condition_and_any_requires_one():
    trigger = cross("above", CLOSE, level(100))
    uptrend = {"type": "compare", "op": ">", "a": CLOSE, "b": level(90)}
    downtrend = {"type": "compare", "op": "<", "a": CLOSE, "b": level(90)}
    closes = [98, 99, 100, 101]
    assert evaluate(validate_condition({"type": "all", "conditions": [trigger, uptrend]}), ctx_for(closes)).fired
    assert not evaluate(validate_condition({"type": "all", "conditions": [trigger, downtrend]}), ctx_for(closes)).fired
    assert evaluate(validate_condition({"type": "any", "conditions": [downtrend, trigger]}), ctx_for(closes)).fired
    assert not evaluate(validate_condition({"type": "any", "conditions": [downtrend]}), ctx_for(closes)).fired


def test_a_fired_condition_explains_itself():
    note = evaluate(cross("above", CLOSE, level(100)), ctx_for([99, 101])).note
    assert note == "close crossed above 100 (101.00 vs 100.00)"


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("raw, message", [
    ("nope", "must be an object"),
    ({"type": "teleport"}, "unknown condition type"),
    ({"type": "cross", "direction": "sideways", "a": CLOSE, "b": CLOSE}, "direction"),
    ({"type": "cross", "direction": "above", "a": {"kind": "magic"}, "b": CLOSE}, "unknown operand"),
    ({"type": "cross", "direction": "above", "a": {"kind": "value", "value": "x"}, "b": CLOSE}, "finite number"),
    ({"type": "cross", "direction": "above", "a": {"kind": "indicator", "name": "ema", "params": {"period": -1}}, "b": CLOSE}, "positive"),
    ({"type": "cross", "direction": "above", "a": {"kind": "indicator", "name": "macd", "output": "nope"}, "b": CLOSE}, "no output"),
    ({"type": "compare", "op": "==", "a": CLOSE, "b": CLOSE}, "compare op"),
    ({"type": "all", "conditions": []}, "non-empty"),
])
def test_malformed_conditions_are_rejected_with_a_readable_reason(raw, message):
    with pytest.raises(ConditionError, match=message):
        validate_condition(raw)


def test_conditions_cannot_be_arbitrarily_large_or_deep():
    leaf = {"type": "compare", "op": ">", "a": CLOSE, "b": level(1)}
    with pytest.raises(ConditionError, match="too large"):
        validate_condition({"type": "all", "conditions": [leaf] * 25})
    deep = leaf
    for _ in range(5):
        deep = {"type": "all", "conditions": [deep]}
    with pytest.raises(ConditionError, match="nested too deeply"):
        validate_condition(deep)


def test_validation_fills_indicator_defaults_and_lists_referenced_drawings():
    cond = validate_condition({"type": "all", "conditions": [
        {"type": "cross", "direction": "above", "a": CLOSE, "b": {"kind": "line", "drawing_id": "d1"}},
        {"type": "compare", "op": ">", "a": {"kind": "indicator", "name": "rsi"}, "b": level(50)},
    ]})
    rsi_operand = cond["conditions"][1]["a"]
    assert rsi_operand["params"] == {"period": 14} and rsi_operand["output"] == "value"
    assert referenced_drawings(cond) == {"d1"}


# ------------------------------------------------------------ stops and targets
def ctx_flat():
    return ctx_for([100.0] * 30)  # every bar: open 100, high 100.5, low 99.5, close 100


def test_percentage_stops_sit_on_the_losing_side_of_the_entry():
    assert compute_stop({"type": "pct", "value": 2}, "BUY", 100, ctx_flat()) == pytest.approx(98.0)
    assert compute_stop({"type": "pct", "value": 2}, "SELL", 100, ctx_flat()) == pytest.approx(102.0)


def test_candle_stop_uses_the_signal_bars_extreme_with_an_optional_buffer():
    assert compute_stop({"type": "candle", "buffer_pct": 0}, "BUY", 100, ctx_flat()) == 99.5
    assert compute_stop({"type": "candle", "buffer_pct": 1}, "BUY", 100, ctx_flat()) == pytest.approx(99.5 * 0.99)
    assert compute_stop({"type": "candle", "buffer_pct": 0}, "SELL", 100, ctx_flat()) == 100.5


def test_atr_stop_is_entry_minus_a_multiple_of_atr():
    ctx = ctx_flat()
    atr = ind.atr(ctx.high, ctx.low, ctx.close, 14)[-1]
    assert compute_stop({"type": "atr", "period": 14, "mult": 2}, "BUY", 100, ctx) == pytest.approx(100 - 2 * atr)


def test_atr_stop_without_enough_history_is_rejected():
    with pytest.raises(InvalidRisk, match="not enough history"):
        compute_stop({"type": "atr", "period": 14, "mult": 2}, "BUY", 100, ctx_for([100.0] * 5))


def test_line_stop_reads_the_drawn_line_on_the_signal_bar():
    ctx = ctx_for([100.0] * 10, {"sup": Drawing(SYMBOL, "horizontal_line", [{"time": 1, "price": 97.0}], id="sup")})
    assert compute_stop({"type": "line", "drawing_id": "sup"}, "BUY", 100, ctx) == 97.0


@pytest.mark.parametrize("side, stop", [("BUY", 101), ("BUY", 100), ("SELL", 99), ("SELL", 100)])
def test_a_stop_on_the_wrong_side_of_the_entry_is_rejected(side, stop):
    with pytest.raises(InvalidRisk, match="must be (below|above)"):
        compute_stop({"type": "fixed", "price": stop}, side, 100, ctx_flat())


def test_risk_reward_target_is_a_multiple_of_the_distance_to_the_stop():
    assert compute_target({"type": "rr", "ratio": 2}, "BUY", 100, 98) == 104
    assert compute_target({"type": "rr", "ratio": 3}, "SELL", 100, 102) == 94
    assert compute_target(None, "BUY", 100, 98) is None


def test_a_target_on_the_wrong_side_is_rejected():
    with pytest.raises(InvalidRisk, match="must be above"):
        compute_target({"type": "fixed", "price": 99}, "BUY", 100, 98)


def test_stop_and_target_specs_are_validated():
    assert validate_stop({"type": "atr"}) == {"type": "atr", "period": 14, "mult": 2.0}
    assert validate_target({"type": "rr", "ratio": 2}) == {"type": "rr", "ratio": 2.0}
    assert validate_target(None) is None
    for bad in ({"type": "pct", "value": 0}, {"type": "zigzag"}, {"type": "line"}, "x"):
        with pytest.raises(ConditionError):
            validate_stop(bad)
    with pytest.raises(ConditionError):
        validate_target({"type": "rr", "ratio": -1})
