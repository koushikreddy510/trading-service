"""Rule payload validation, storage, and the once-per-bar signal guard."""
import pytest

from trading_service.terminal.rules import InvalidRule, Rule, RuleRepo, Signal, SignalRepo, referenced_ids, rule_from_payload
from tests.terminal.helpers import ist

SYMBOL = "NSE:SBIN-EQ"


def payload(**over):
    base = {
        "symbol": SYMBOL, "timeframe": "1H", "side": "buy", "quantity": 10, "name": "Breakout",
        "condition": {"type": "cross", "direction": "above", "a": {"kind": "close"}, "b": {"kind": "line", "drawing_id": "res"}},
        "stop": {"type": "candle"}, "target": {"type": "rr", "ratio": 2},
    }
    base.update(over)
    return base


def test_a_valid_payload_becomes_a_normalised_rule():
    rule = rule_from_payload(payload())
    assert rule.side == "BUY" and rule.enabled is True and rule.once is False
    assert rule.stop == {"type": "candle", "buffer_pct": 0.0}
    assert referenced_ids(rule) == {"res"}


def test_a_line_stop_counts_as_a_referenced_drawing():
    rule = rule_from_payload(payload(stop={"type": "line", "drawing_id": "sup"}))
    assert referenced_ids(rule) == {"res", "sup"}


@pytest.mark.parametrize("over, message", [
    ({"symbol": ""}, "symbol is required"),
    ({"timeframe": "7m"}, "unknown timeframe"),
    ({"side": "HOLD"}, "side must be"),
    ({"quantity": 0}, "quantity"),
    ({"quantity": 1.5}, "quantity"),
    ({"quantity": True}, "quantity"),
    ({"condition": {"type": "nope"}}, "unknown condition type"),
    ({"stop": {"type": "pct", "value": -1}}, "positive"),
    ({"target": {"type": "rr", "ratio": 0}}, "positive"),
])
def test_invalid_payloads_say_what_is_wrong(over, message):
    with pytest.raises(InvalidRule, match=message):
        rule_from_payload(payload(**over))


def test_rules_round_trip_through_the_database(db):
    repo = RuleRepo(db)
    saved = repo.save(rule_from_payload(payload()))
    loaded = repo.get(saved.id)
    assert loaded.to_json() == saved.to_json()
    assert loaded.condition["b"] == {"kind": "line", "drawing_id": "res"}


def test_listing_filters_by_symbol_timeframe_and_enabled(db):
    repo = RuleRepo(db)
    a = repo.save(rule_from_payload(payload()))
    b = repo.save(rule_from_payload(payload(timeframe="1D")))
    c = repo.save(rule_from_payload(payload(symbol="NSE:TCS-EQ", enabled=False)))
    assert {r.id for r in repo.list()} == {a.id, b.id, c.id}
    assert {r.id for r in repo.list(symbol=SYMBOL)} == {a.id, b.id}
    assert [r.id for r in repo.list(symbol=SYMBOL, timeframe="1H")] == [a.id]
    assert [r.id for r in repo.list(enabled_only=True) if r.symbol == "NSE:TCS-EQ"] == []


def test_enable_disable_and_delete(db):
    repo = RuleRepo(db)
    rule = repo.save(rule_from_payload(payload()))
    assert repo.set_enabled(rule.id, False) and repo.get(rule.id).enabled is False
    assert repo.delete(rule.id) and repo.get(rule.id) is None
    assert repo.set_enabled(rule.id, True) is False


def test_a_rule_can_fire_only_once_per_bar(db):
    rules = RuleRepo(db)
    rule = rules.save(rule_from_payload(payload()))
    signals = SignalRepo(db)
    bar = ist(2026, 10, 19, 10, 15)
    first = Signal(SYMBOL, "1H", "BUY", bar, 100.0, "open", rule_id=rule.id, stop=98.0)
    replay = Signal(SYMBOL, "1H", "BUY", bar, 100.0, "open", rule_id=rule.id, stop=98.0)   # same bar, e.g. a replayed event
    next_bar = Signal(SYMBOL, "1H", "BUY", ist(2026, 10, 19, 11, 15), 101.0, "open", rule_id=rule.id)
    assert signals.insert_if_new(first) is True
    assert signals.insert_if_new(replay) is False
    assert signals.insert_if_new(next_bar) is True
    assert [s.entry for s in signals.list(SYMBOL)] == [101.0, 100.0]    # newest first
