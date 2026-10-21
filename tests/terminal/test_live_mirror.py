"""A rule's paper position mirrored with live orders: entry, protective stop, trailing, 2-level exit, close, cut-off."""
from datetime import datetime, timezone

import pytest

from tests.terminal.helpers import ist
from tests.terminal.test_orders import QUOTE, Broker
from trading_service.terminal.live_mirror import LiveMirror, past_cutoff
from trading_service.terminal.orders import OrderService
from trading_service.terminal.rules import RuleRepo, Signal, SignalRepo, rule_from_payload

SYM = "NSE:SBIN-EQ"
NOW = ist(2026, 10, 22, 10, 0)


class Mirror:
    def __init__(self, db, execution={"account": "flattrade", "product": "I"}, live=True):
        self.db, self.broker, self.slack = db, Broker(), []
        self.orders = OrderService(db, accounts=lambda: {"flattrade": self.broker}, quote=lambda s: QUOTE[s], notify=self.slack.append)
        self.orders.settings.set("flattrade", live_enabled=live)
        self.broker.modify = self._modify
        self.modified = []
        self.mirror = LiveMirror(db, self.orders, notify=self.slack.append, clock=lambda: NOW, background=False)
        self.rule = RuleRepo(db).save(rule_from_payload({
            "symbol": SYM, "timeframe": "15m", "side": "BUY", "quantity": 4, "name": "test rule",
            "condition": {"type": "compare", "op": ">", "a": {"kind": "close"}, "b": {"kind": "value", "value": 1}},
            "stop": {"type": "pct", "value": 1}, "execution": execution}))
        sig = Signal(SYM, "15m", "BUY", NOW, 967.0, "open", rule_id=self.rule.id, stop=955.0)
        SignalRepo(db).insert_if_new(sig)
        self.p = {"id": "pos1", "signal_id": sig.id, "symbol": SYM, "side": "BUY", "quantity": 4, "entry_price": 967.0, "stop": 955.0,
                  "status": "open", "opened_at": int(NOW.timestamp()), "exit_level": 1, "exit_qty_1": None, "exit_reason": None}

    def _modify(self, oid, order):
        self.modified.append((oid, order["units"], order["trigger_price"]))

    def event(self, **changes):
        self.p.update(changes)
        self.mirror.on_event({"type": "position", "position": dict(self.p)})

    def fill(self, n, units=None, price=967.0):
        oid = f"B{n}"
        self.broker.book[oid].update(status="complete", filled_units=units if units is not None else self.broker.sent[n - 1]["units"], avg_price=price)
        self.orders.sync()

    def held_by_broker(self, units):
        self.broker.positions[(SYM, "I")] = units


def test_an_open_paper_position_becomes_a_live_entry_and_a_stop_after_the_fill(db):
    m = Mirror(db)
    m.event()
    (entry,) = m.broker.sent
    assert (entry["side"], entry["units"], entry["product"]) == ("BUY", 4, "I") and "atlas:" in entry["tag"]
    m.held_by_broker(4)
    m.fill(1)
    stop = m.broker.sent[1]
    assert (stop["side"], stop["units"], stop["trigger_price"]) == ("SELL", 4, 955.0)
    assert m.mirror.links.get("pos1")["stop_order_id"]


def test_a_trailing_step_modifies_the_same_stop(db):
    m = Mirror(db)
    m.event(); m.held_by_broker(4); m.fill(1)
    m.event(stop=960.0)
    m.event(stop=958.0)                    # a looser stop is never sent
    assert m.modified == [("B2", 4, 960.0)] and len(m.broker.sent) == 2


def test_exit_level_1_shrinks_the_stop_first_then_sells_the_first_part(db):
    m = Mirror(db)
    m.event(); m.held_by_broker(4); m.fill(1)
    m.event(exit_level=2, exit_qty_1=2, quantity=2)
    assert m.modified == [("B2", 2, 955.0)]
    exit_ = m.broker.sent[2]
    assert (exit_["side"], exit_["units"]) == ("SELL", 2) and m.mirror.links.get("pos1")["partial_done"]


def test_a_close_cancels_the_stop_and_sells_only_what_this_position_holds(db):
    m = Mirror(db)
    m.event(); m.held_by_broker(4); m.fill(1)
    m.event(status="closed", exit_reason="exit_rule")
    assert m.broker.cancelled == ["B2"]
    final = m.broker.sent[2]
    assert (final["side"], final["units"]) == ("SELL", 4) and m.mirror.links.get("pos1")["state"] == "closed"


def test_when_the_broker_stop_already_filled_the_close_sends_nothing_more(db):
    m = Mirror(db)
    m.event(); m.held_by_broker(4); m.fill(1)
    m.fill(2, price=955.0)                 # the stop triggered at the broker
    m.held_by_broker(0)
    m.event(status="closed", exit_reason="stop")
    assert len(m.broker.sent) == 2 and m.broker.cancelled == []


def test_a_paper_only_rule_and_an_old_position_never_trade(db):
    m = Mirror(db, execution=None)
    m.event()
    assert m.broker.sent == []
    m3 = Mirror(db)
    m3.p["id"], m3.p["opened_at"] = "old", int(ist(2026, 10, 22, 9, 30).timestamp())    # 30 minutes old
    m3.event()
    assert m3.broker.sent == []


def test_with_live_orders_off_the_entry_is_refused_and_reported_once(db):
    m = Mirror(db, live=False)
    m.event()
    m.event(stop=960.0)
    assert m.broker.sent == [] and m.mirror.links.get("pos1")["state"] == "refused"
    assert sum("Live entry refused" in t for t in m.slack) == 1


def test_intraday_positions_reach_the_cut_off(db):
    assert not past_cutoff("NSE", ist(2026, 10, 22, 15, 11)) and past_cutoff("NSE", ist(2026, 10, 22, 15, 12))
    assert past_cutoff("MCX", ist(2026, 10, 22, 23, 16)) and not past_cutoff("MCX", ist(2026, 11, 3, 23, 30))   # 23:40 in winter
    m = Mirror(db)
    m.event()
    assert m.mirror.due_for_cutoff() == []
    m.mirror.clock = lambda: ist(2026, 10, 22, 15, 13)
    assert [l["position_id"] for l in m.mirror.due_for_cutoff()] == ["pos1"]
