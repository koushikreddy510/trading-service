"""Live orders: every check that must pass before anything is sent, the ledger, and tracking."""
import pytest

from trading_service.terminal.orders import OrderRefused, OrderService, round_to_tick

NSE = [{"prd": "F", "name": "MTF", "listed": False}, {"prd": "I", "name": "Intraday", "listed": True}]
MCX = [{"prd": "M", "name": "NRML", "listed": True}, {"prd": "I", "name": "Intraday", "listed": True}]
QUOTE = {"NSE:SBIN-EQ": {"ltp": 967.6, "bid": 967.5, "ask": 967.7, "tick_size": 0.05, "lot_size": 1, "point_value": 1.0,
                         "lower_circuit": 876.35, "upper_circuit": 1071.05},
         "MCX:SILVERM30NOV26": {"ltp": 211200.0, "bid": 211190.0, "ask": 211210.0, "tick_size": 1.0, "lot_size": 5, "point_value": 5.0}}


class Broker:
    def __init__(self, positions=None, fail=None):
        self.sent, self.cancelled = [], []
        self.positions = positions or {}
        self.fail = fail
        self.book = {}

    def products(self):
        return {"NSE": NSE, "MCX": MCX}

    def net_position(self, symbol, product):
        return self.positions.get((symbol, product), 0)

    def place(self, order):
        if self.fail:
            raise RuntimeError(self.fail)
        self.sent.append(order)
        oid = f"B{len(self.sent)}"
        self.book[oid] = {"broker_order_id": oid, "status": "open", "filled_units": 0, "avg_price": None, "message": "", "tag": order["tag"]}
        return oid

    def cancel(self, oid):
        self.cancelled.append(oid)
        self.book[oid]["status"] = "cancelled"

    def orders(self):
        return list(self.book.values())


@pytest.fixture
def setup(db):
    broker = Broker()
    sent = []
    svc = OrderService(db, accounts=lambda: {"flattrade": broker}, quote=lambda s: QUOTE[s], notify=sent.append)
    svc.settings.set("flattrade", live_enabled=True)
    return svc, broker, sent


def test_live_orders_are_off_until_switched_on(db):
    broker = Broker()
    svc = OrderService(db, accounts=lambda: {"flattrade": broker}, quote=lambda s: QUOTE[s])
    with pytest.raises(OrderRefused, match="live orders are off"):
        svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 1, "I", "LMT", 950)
    assert broker.sent == [] and svc.today() == []


def test_a_limit_order_is_written_to_the_ledger_and_sent_with_our_tag(setup):
    svc, broker, sent = setup
    row = svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 1, "I", "LMT", 950.03)
    (order,) = broker.sent
    assert (order["units"], order["price"], order["product"], order["side"]) == (1, 950.0, "I", "BUY")   # rounded to the tick
    assert order["tag"] == f"atlas:{row['id']}" and row["status"] == "open" and row["broker_order_id"] == "B1"
    assert "Order sent" in sent[0]


def test_a_market_order_is_a_limit_just_through_the_touch_within_the_circuit(setup):
    svc, broker, _ = setup
    svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 2, "I", "MKT")
    svc.place("flattrade", "NSE:SBIN-EQ", "SELL", 2, "I", "MKT")
    buy, sell = broker.sent
    assert buy["price"] == round_to_tick(967.7 * 1.003, 0.05, up=True) and sell["price"] == round_to_tick(967.5 * 0.997, 0.05, up=False)


def test_futures_are_sent_in_units_and_valued_by_point_value(setup):
    svc, broker, _ = setup
    svc.settings.set("flattrade", max_order_value=5_000_000)
    svc.place("flattrade", "MCX:SILVERM30NOV26", "SELL", 2, "M", "LMT", 211000)
    assert broker.sent[0]["units"] == 10


@pytest.mark.parametrize("product,why", [("C", "not used for NSE"), ("M", "not used for NSE"), ("F", "MTF is not activated")])
def test_products_outside_policy_or_not_activated_are_refused(setup, product, why):
    svc, broker, _ = setup
    with pytest.raises(OrderRefused, match=why):
        svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 1, product, "LMT", 950)
    assert broker.sent == []


def test_a_position_that_is_not_ours_blocks_the_order(db):
    broker = Broker(positions={("MCX:SILVERM30NOV26", "M"): -5})          # the user's own NRML short
    svc = OrderService(db, accounts=lambda: {"flattrade": broker}, quote=lambda s: QUOTE[s])
    svc.settings.set("flattrade", live_enabled=True, max_order_value=5_000_000)
    with pytest.raises(OrderRefused, match="not ours"):
        svc.place("flattrade", "MCX:SILVERM30NOV26", "BUY", 1, "M", "LMT", 211000)
    assert broker.sent == []
    svc.place("flattrade", "MCX:SILVERM30NOV26", "BUY", 1, "I", "LMT", 211000)   # intraday is a separate position line
    assert len(broker.sent) == 1


def test_our_own_filled_quantity_does_not_count_as_someone_elses(setup):
    svc, broker, _ = setup
    row = svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 3, "I", "LMT", 950)
    broker.book["B1"].update(status="complete", filled_units=3, avg_price=950.0)
    svc.sync()
    broker.positions[("NSE:SBIN-EQ", "I")] = 3                               # the broker now shows exactly our 3
    svc.place("flattrade", "NSE:SBIN-EQ", "SELL", 3, "I", "LMT", 960)
    assert len(broker.sent) == 2 and svc.repo.get(row["id"])["filled_units"] == 3


def test_value_and_daily_count_limits(setup):
    svc, broker, _ = setup
    with pytest.raises(OrderRefused, match="above the ₹20,000 limit"):
        svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 30, "I", "LMT", 950)
    svc.settings.set("flattrade", max_orders_per_day=1)
    svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 1, "I", "LMT", 950)
    with pytest.raises(OrderRefused, match="daily limit"):
        svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 1, "I", "LMT", 950)


def test_the_kill_switch_stops_everything(setup):
    svc, broker, _ = setup
    svc.settings.set("*", live_enabled=False)
    with pytest.raises(OrderRefused, match="kill switch"):
        svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 1, "I", "LMT", 950)
    assert svc.settings.killed() and broker.sent == []


def test_a_broker_refusal_is_recorded_and_reported(db):
    broker = Broker(fail="RMS: insufficient margin")
    sent = []
    svc = OrderService(db, accounts=lambda: {"flattrade": broker}, quote=lambda s: QUOTE[s], notify=sent.append)
    svc.settings.set("flattrade", live_enabled=True)
    with pytest.raises(RuntimeError):
        svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 1, "I", "LMT", 950)
    (row,) = svc.today()
    assert row["status"] == "failed" and "insufficient margin" in row["message"] and "Order failed" in sent[0]


def test_tracking_follows_fills_and_cancels(setup):
    svc, broker, sent = setup
    a = svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 1, "I", "LMT", 950)
    b = svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 1, "I", "LMT", 940)
    broker.book["B1"].update(status="complete", filled_units=1, avg_price=950.0)
    (changed,) = svc.sync()
    assert changed["id"] == a["id"] and changed["status"] == "complete" and any("Order complete" in t for t in sent)
    assert svc.cancel(b["id"])["status"] == "cancelled" and broker.cancelled == ["B2"]
    with pytest.raises(OrderRefused, match="only an open order"):
        svc.cancel(b["id"])


def test_an_index_is_never_ordered(setup):
    svc, _, _ = setup
    with pytest.raises(OrderRefused, match="index"):
        svc.place("flattrade", "NSE:NIFTY50-INDEX", "BUY", 1, "I", "LMT", 100)


def _hold(svc, broker, units, price=950.0):
    svc.place("flattrade", "NSE:SBIN-EQ", "BUY", units, "I", "LMT", price)
    oid = f"B{len(broker.sent)}"
    broker.book[oid].update(status="complete", filled_units=units, avg_price=price)
    svc.sync()
    broker.positions[("NSE:SBIN-EQ", "I")] = broker.positions.get(("NSE:SBIN-EQ", "I"), 0) + units


def test_a_two_step_stop_loss_covers_exactly_what_we_hold(setup):
    svc, broker, _ = setup
    _hold(svc, broker, 2)
    a = svc.place("flattrade", "NSE:SBIN-EQ", "SELL", 1, "I", "SL", trigger_price=955.0)
    b = svc.place("flattrade", "NSE:SBIN-EQ", "SELL", 1, "I", "SL", trigger_price=945.0)
    assert (broker.sent[1]["trigger_price"], broker.sent[1]["price"]) == (955.0, round_to_tick(955 * 0.995, 0.05, up=False))
    assert a["trigger_price"] == 955.0 and b["trigger_price"] == 945.0
    with pytest.raises(OrderRefused, match="only protects what we hold"):
        svc.place("flattrade", "NSE:SBIN-EQ", "SELL", 1, "I", "SL", trigger_price=940.0)


def test_a_stop_must_trigger_on_the_right_side_of_the_price(setup):
    svc, broker, _ = setup
    _hold(svc, broker, 1)
    with pytest.raises(OrderRefused, match="below the last price"):
        svc.place("flattrade", "NSE:SBIN-EQ", "SELL", 1, "I", "SL", trigger_price=970.0)


def test_a_stop_without_a_position_of_ours_is_refused(setup):
    svc, broker, _ = setup
    with pytest.raises(OrderRefused, match="only protects what we hold"):
        svc.place("flattrade", "NSE:SBIN-EQ", "SELL", 1, "I", "SL", trigger_price=950.0)


def test_a_trailing_step_moves_the_same_broker_order(setup):
    svc, broker, sent = setup
    _hold(svc, broker, 1)
    stop = svc.place("flattrade", "NSE:SBIN-EQ", "SELL", 1, "I", "SL", trigger_price=945.0)
    calls = []
    broker.modify = lambda oid, order: calls.append((oid, order["trigger_price"]))
    moved = svc.modify_stop(stop["id"], 955.0)
    assert calls == [(stop["broker_order_id"], 955.0)] and moved["trigger_price"] == 955.0 and len(broker.sent) == 2
    assert any("Stop moved" in t for t in sent)


def test_live_positions_come_from_our_fills_with_average_cost_and_pnl(setup):
    svc, broker, _ = setup
    _hold(svc, broker, 2, price=950.0)
    _hold(svc, broker, 2, price=960.0)                       # average 955 for 4
    svc.place("flattrade", "NSE:SBIN-EQ", "SELL", 1, "I", "LMT", 970.0)
    broker.book["B3"].update(status="complete", filled_units=1, avg_price=970.0)
    svc.sync()
    broker.positions[("NSE:SBIN-EQ", "I")] = 3                # the broker agrees: 3 left, all ours
    svc.place("flattrade", "NSE:SBIN-EQ", "SELL", 1, "I", "SL", trigger_price=940.0)
    (p,) = svc.positions(lambda s: 965.0)
    assert (p["side"], p["units"], p["avg_price"], p["realized"]) == ("BUY", 3, 955.0, 15.0)
    assert p["unrealized"] == pytest.approx(30.0) and [s["trigger_price"] for s in p["stops"]] == [940.0]


def test_an_entry_without_funds_in_its_segment_is_refused_but_an_exit_never_is(setup):
    svc, broker, _ = setup
    _hold(svc, broker, 2)
    broker.margin_check = lambda symbol, side, units, product, price: (False, "Insufficient Balance", 1500.0)
    with pytest.raises(OrderRefused, match="not enough funds in the equity segment"):
        svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 1, "I", "LMT", 950)
    svc.place("flattrade", "NSE:SBIN-EQ", "SELL", 1, "I", "SL", trigger_price=945.0)     # a stop still goes
    svc.place("flattrade", "NSE:SBIN-EQ", "SELL", 1, "I", "LMT", 960.0)                  # and so does an exit
    assert len(broker.sent) == 3


def test_the_order_type_asked_for_is_recorded(setup):
    svc, broker, _ = setup
    assert svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 1, "I", "MKT")["order_type"] == "MKT"
    assert svc.place("flattrade", "NSE:SBIN-EQ", "BUY", 1, "I", "LMT", 950)["order_type"] == "LMT"
