"""A strategy = rule template + watchlist: one rule per symbol, kept in step with the list."""
import pytest

from trading_service.terminal.rules import RuleRepo
from trading_service.terminal.strategies import StrategyError, StrategyRepo, rule_name
from trading_service.terminal.watchlists import WatchlistRepo

TEMPLATE = {
    "timeframe": "15m", "side": "SELL", "quantity": 10,
    "condition": {"type": "compare", "op": "<", "a": {"kind": "close"}, "b": {"kind": "value", "value": 5}},
    "stop": {"type": "candle", "buffer_pct": 0.5}, "target": {"type": "rr", "ratio": 3},
}


@pytest.fixture
def env(db):
    return StrategyRepo(db), RuleRepo(db), WatchlistRepo(db)


def make(repo, **kw):
    return repo.save({"name": "Short X", "template": TEMPLATE, "watchlist": "Cheap", **kw})


def test_apply_makes_one_rule_per_symbol_and_is_repeatable(env):
    strat, rules, wl = env
    wl.create("Cheap", ["NSE:IDEA-EQ", "NSE:JPPOWER-EQ"])
    make(strat)
    res = strat.apply(strat.list()[0], rules, wl)
    assert (res["added"], res["updated"]) == (2, 0)
    assert {r.symbol for r in rules.list() if r.name == rule_name("Short X")} == {"NSE:IDEA-EQ", "NSE:JPPOWER-EQ"}
    assert strat.apply(strat.list()[0], rules, wl)["updated"] == 2 and len(rules.list()) == 2


def test_symbol_leaving_the_list_is_switched_off_not_deleted(env):
    strat, rules, wl = env
    w = wl.create("Cheap", ["NSE:IDEA-EQ", "NSE:JPPOWER-EQ"])
    make(strat)
    strat.apply(strat.list()[0], rules, wl)
    wl.remove(w["id"], "NSE:IDEA-EQ")
    assert strat.apply(strat.list()[0], rules, wl)["switched_off"] == 1
    assert {r.symbol: r.enabled for r in rules.list()} == {"NSE:IDEA-EQ": False, "NSE:JPPOWER-EQ": True}


def test_disabled_strategy_switches_its_rules_off_and_template_edits_propagate(env):
    strat, rules, wl = env
    wl.create("Cheap", ["NSE:IDEA-EQ"])
    make(strat)
    strat.apply(strat.list()[0], rules, wl)
    make(strat, enabled=False, template={**TEMPLATE, "quantity": 99})
    strat.apply(strat.list()[0], rules, wl)
    (r,) = rules.list()
    assert not r.enabled and r.quantity == 99


def test_bad_template_and_missing_watchlist_are_refused(env):
    strat, rules, wl = env
    with pytest.raises(StrategyError):
        make(strat, template={**TEMPLATE, "side": "HOLD"})
    make(strat, watchlist="Nope")
    with pytest.raises(StrategyError, match="not found"):
        strat.apply(strat.list()[0], rules, wl)


def test_apply_all_runs_every_strategy_and_reports_the_one_that_cannot_be_applied(env):
    strat, rules, wl = env
    wl.create("Cheap", ["NSE:IDEA-EQ", "NSE:JPPOWER-EQ"])
    make(strat)
    make(strat, name="Lost", watchlist="Nope")
    out = {r["strategy"]: r for r in strat.apply_all(rules, wl)}
    assert out["Short X"]["added"] == 2 and "not found" in out["Lost"]["error"]
    assert len(rules.list()) == 2


def test_save_many_inserts_and_updates_in_one_go(env):
    _, rules, _ = env
    from trading_service.terminal.rules import rule_from_payload
    a = rule_from_payload({**TEMPLATE, "symbol": "NSE:A-EQ"})
    b = rule_from_payload({**TEMPLATE, "symbol": "NSE:B-EQ"})
    rules.save_many([a, b])
    a.quantity = 77
    rules.save_many([a])
    assert {r.symbol: r.quantity for r in rules.list()} == {"NSE:A-EQ": 77, "NSE:B-EQ": 10}
    rules.save_many([])      # nothing to do is fine
