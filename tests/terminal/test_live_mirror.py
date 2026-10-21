"""A rule's paper position mirrored with live orders: entry, protective stop, trailing, 2-level exit, close, cut-off."""
from datetime import datetime, timedelta, timezone

import pytest

from tests.terminal.helpers import ist
from tests.terminal.test_orders import QUOTE, Broker
from trading_service.terminal.live_mirror import LiveMirror, past_cutoff
from trading_service.terminal.orders import OrderRefused, OrderService
from trading_service.terminal.rules import RuleRepo, Signal, SignalRepo, rule_from_payload

SYM = "NSE:SBIN-EQ"
NOW = ist(2026, 10, 22, 10, 0)


class Mirror:
    def __init__(self, db, execution={"account": "flattrade", "product": "I"}, live=True, approval=False):
        self.db, self.broker, self.slack = db, Broker(), []
        self.orders = OrderService(db, accounts=lambda: {"flattrade": self.broker}, quote=lambda s: QUOTE[s], notify=lambda t, channel="orders": self.slack.append(t))
        self.orders.settings.set("flattrade", live_enabled=live, require_approval=approval)
        self.broker.modify = self._modify
        self.modified = []
        self.events = []
        self.now = NOW
        self.mirror = LiveMirror(db, self.orders, notify=self.slack.append, clock=lambda: self.now, background=False,
                                 broadcast=self.events.append)
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


def test_messages_say_what_happened_and_go_to_the_right_channel(db):
    m = Mirror(db)
    routed = []
    m.orders.notify = lambda text, channel="orders": routed.append((channel, text))
    m.event(); m.held_by_broker(4); m.fill(1, price=967.0)
    m.event(stop=960.0)
    m.fill(2, price=959.5)                       # the stop fills at the broker
    by = {c: [t for ch, t in routed if ch == c] for c in ("orders", "updates")}
    assert any(t.startswith("📤 *Order sent*") and "BUY 4 NSE:SBIN-EQ · Market" in t and "rule “test rule” (entry)" in t for t in by["updates"])
    assert any("🛡 *Stop-loss placed*" in t and "trigger ₹955.00" in t for t in by["updates"])
    assert any("🔁 *Trailing stop moved*" in t and "₹955.00 → ₹960.00" in t for t in by["updates"])
    assert any(t.startswith("🟢 *BOUGHT* · 4 NSE:SBIN-EQ @ ₹967.00") for t in by["orders"])
    assert any(t.startswith("🛑 *Stop-loss hit*") and "P&L on this exit: −₹30.00" in t for t in by["orders"])



# ------------------------------------------------------------------ approvals
def _paper_position(db, m):
    """The paper position must exist (approval checks it is still open)."""
    db.execute("""INSERT INTO terminal_paper_positions (id, signal_id, symbol, side, quantity, entry_price, stop, target, status, opened_at)
                  VALUES ('pos1', :sig, :sym, 'BUY', 4, 967, 955, NULL, 'open', now())""", sig=m.p["signal_id"], sym=SYM)


def test_with_approval_required_nothing_is_sent_until_a_person_approves(db):
    m = Mirror(db, approval=True)
    _paper_position(db, m)
    m.event()
    assert m.broker.sent == [] and m.mirror.links.get("pos1")["state"] == "awaiting_approval"
    (a,) = m.mirror.approvals(pending_only=True)
    assert (a["side"], a["quantity"], a["symbol"], a["stop"], a["rule_name"]) == ("BUY", 4, SYM, 955.0, "test rule")
    assert any("Approval needed" in t for t in m.slack) and m.events[-1]["type"] == "approval"
    assert m.mirror.decide(a["id"], True)["status"] == "approved"
    assert m.broker.sent[0]["side"] == "BUY" and m.mirror.links.get("pos1")["state"] == "live"
    m.held_by_broker(4); m.fill(1)
    assert m.broker.sent[1]["trigger_price"] == 955.0                      # the stop follows by itself


def test_a_rejected_approval_sends_nothing(db):
    m = Mirror(db, approval=True)
    _paper_position(db, m)
    m.event()
    (a,) = m.mirror.approvals(pending_only=True)
    assert m.mirror.decide(a["id"], False)["status"] == "rejected" and m.broker.sent == []
    with pytest.raises(OrderRefused, match="already rejected"):
        m.mirror.decide(a["id"], True)


def test_an_approval_expires_after_its_window(db):
    m = Mirror(db, approval=True)
    _paper_position(db, m)
    m.event()
    m.now = NOW + timedelta(minutes=6)
    assert m.mirror.expire_due() == 1
    assert m.mirror.links.get("pos1")["state"] == "expired" and m.broker.sent == [] and any("Approval expired" in t for t in m.slack)


def test_a_position_that_closes_before_approval_cannot_be_entered(db):
    m = Mirror(db, approval=True)
    _paper_position(db, m)
    m.event()
    (a,) = m.mirror.approvals(pending_only=True)
    db.execute("UPDATE terminal_paper_positions SET status = 'closed' WHERE id = 'pos1'")
    with pytest.raises(OrderRefused, match="already closed"):
        m.mirror.decide(a["id"], True)
    assert m.broker.sent == []
