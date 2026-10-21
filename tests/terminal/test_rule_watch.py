"""How close each rule is to triggering: condition parts with values and distance, next check, open position exits."""
from datetime import timedelta

import pytest

from tests.terminal.helpers import ist
from trading_service.terminal.candles import Candle
from trading_service.terminal.conditions import EvalContext, explain

T0 = ist(2026, 10, 21, 10, 0)


def ctx(closes, line=None):
    bars = [Candle(T0 + timedelta(minutes=15 * i), c, c + 1, c - 1, c, 100) for i, c in enumerate(closes)]
    drawings = {}
    if line is not None:
        from trading_service.terminal.drawings import Drawing
        drawings = {"L": Drawing("MCX:X", "horizontal_line", [{"time": 1, "price": line}], id="L")}
    return EvalContext(bars, "15m", drawings, "MCX")


CLOSE = {"kind": "close"}
LINE = {"kind": "line", "drawing_id": "L"}


def test_a_cross_below_a_line_reports_how_far_price_still_has_to_fall():
    e = explain({"type": "cross", "direction": "below", "a": CLOSE, "b": LINE}, ctx([14560, 14550, 14538], line=14536))
    assert e["met"] is False and e["label"] == "close crosses below line"
    assert e["a"] == 14538 and e["b"] == 14536 and e["distance"] == 2 and e["distance_pct"] == pytest.approx(2 / 14538 * 100, abs=1e-3)


def test_the_live_price_shows_the_move_in_progress():
    e = explain({"type": "cross", "direction": "below", "a": CLOSE, "b": LINE}, ctx([14560, 14550, 14538], line=14536), live=14530)
    assert e["a_live"] == 14530 and e["on_trigger_side"] is True and e["met"] is False   # only a candle close triggers


def test_a_cross_that_happened_on_the_last_candle_is_met():
    e = explain({"type": "cross", "direction": "below", "a": CLOSE, "b": LINE}, ctx([14560, 14540, 14530], line=14536))
    assert e["met"] is True and e["distance_pct"] == 0.0


def test_all_reports_the_farthest_unmet_part_and_any_the_nearest():
    a = {"type": "compare", "op": ">", "a": CLOSE, "b": {"kind": "value", "value": 110}}    # 10% away
    b = {"type": "compare", "op": ">", "a": CLOSE, "b": {"kind": "value", "value": 101}}    # 1% away
    c = ctx([100, 100, 100])
    assert explain({"type": "all", "conditions": [a, b]}, c)["distance_pct"] == pytest.approx(10.0)
    assert explain({"type": "any", "conditions": [a, b]}, c)["distance_pct"] == pytest.approx(1.0)


def test_the_watch_endpoint_lists_rules_closest_first(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from trading_service.terminal import api
    from trading_service.terminal.feed.mock import MockFeed, MockHistory, SimClock
    from trading_service.terminal.rules import rule_from_payload
    from trading_service.terminal.runtime import Terminal
    clock = SimClock(ist(2026, 10, 19, 11, 0, 30), speed=0)
    app = FastAPI()
    api.mount(app)
    t = Terminal(db, MockFeed(clock), MockHistory(), clock.now, mode="test", hub_interval=0)
    app.state.terminal = t
    for i in range(60):                                   # an hour of minute bars so the 15m candles exist
        m = ist(2026, 10, 19, 9, 45) + timedelta(minutes=i)
        t.candles.store_minute("NSE:SBIN-EQ", Candle(m, 100, 101, 99, 100, 10))
    for name, level in (("far", 1_000_000), ("near", 1)):
        t.rules.save(rule_from_payload({"symbol": "NSE:SBIN-EQ", "timeframe": "15m", "side": "BUY", "quantity": 1, "name": name,
                                        "condition": {"type": "compare", "op": ">", "a": CLOSE, "b": {"kind": "value", "value": level}},
                                        "stop": {"type": "pct", "value": 1}}))
    with TestClient(app) as cl:
        rows = cl.get("/terminal/rules/watch").json()["rules"]
    assert [r["rule"]["name"] for r in rows][:2] == ["near", "far"]
    assert rows[1]["entry"]["distance_pct"] == pytest.approx((1_000_000 - 100) / 100 * 100)
    assert rows[0]["entry"]["label"] == "close > 1" and "next_check" in rows[0]


def test_a_cross_that_happened_earlier_is_not_reported_as_close():
    e = explain({"type": "cross", "direction": "below", "a": CLOSE, "b": LINE}, ctx([14540, 14530, 14520], line=14536))
    assert e["state"] == "crossed_earlier" and e["distance_pct"] is None and e["met"] is False
