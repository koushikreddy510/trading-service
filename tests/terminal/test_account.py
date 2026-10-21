"""The order ticket's read-only account views: funds, quote, margin preview. None of them may ever send an order."""
import pytest

from trading_service.terminal.account import BrokerAccount, parse_limits
from trading_service.terminal.feed.base import BrokerError

USER_DETAILS = {"stat": "Ok", "prarr": [
    {"prd": "M", "s_prdt_ali": "NRML", "exch": ["NFO", "MCX"]},
    {"prd": "I", "s_prdt_ali": "MIS", "exch": ["NSE", "NFO", "MCX"]},
    {"prd": "C", "s_prdt_ali": "CNC", "exch": ["NSE", "BSE"]},
]}
QUOTES = {
    "562059": {"stat": "Ok", "lp": "211200.00", "c": "213900.00", "o": "213000", "h": "213500", "l": "211000", "bp1": "211190", "bq1": "3",
               "sp1": "211210", "sq1": "5", "uc": "222000", "lc": "205000", "v": "23384", "oi": "1200", "ls": "5", "ti": "1.00",
               "exd": "30-NOV-2026", "instname": "FUTCOM"},
    "3045": {"stat": "Ok", "lp": "966.60", "c": "973.70", "ls": "1", "ti": "0.05"},
}


class FakeApi:
    def __init__(self, positions=None, cash="50000.00"):
        self.calls = []
        self.positions = positions
        self.cash = cash

    def call(self, endpoint, payload):
        self.calls.append((endpoint, payload))
        assert endpoint in {"Limits", "UserDetails", "GetQuotes", "GetOrderMargin", "PositionBook"}, f"{endpoint} must never be called"
        if endpoint == "Limits":
            return {"stat": "Ok", "cash": self.cash, "payin": "0.00", "brkcollamt": "0.00", "marginused": "5000.00"}
        if endpoint == "UserDetails":
            return USER_DETAILS
        if endpoint == "GetQuotes":
            return QUOTES[payload["token"]]
        if endpoint == "GetOrderMargin":
            return {"stat": "Ok", "ordermargin": "30259.80", "marginused": "35259.80", "remarks": ""}
        if self.positions is None:
            raise BrokerError("PositionBook: no data")
        return self.positions


class Resolver:
    def resolve(self, symbol):
        return {"MCX:SILVERM30NOV26": ("MCX", "562059"), "NSE:SBIN-EQ": ("NSE", "3045")}[symbol]


class Instruments:
    def point_value(self, symbol):
        return 5.0


def account(api):
    return BrokerAccount(api, Resolver(), Instruments(), "FT1")


def test_funds_available_is_cash_plus_payin_and_collateral_minus_margin_used():
    assert parse_limits({"cash": "15000.00", "payin": "2000", "brkcollamt": "1000", "marginused": "4000"})["available"] == 14000.0
    assert parse_limits({"cash": "15000.00"}) | {} == {"cash": 15000.0, "payin": 0.0, "collateral": 0.0, "margin_used": 0.0,
                                                     "available": 15000.0, "payout": 0.0, "blocked": 0.0}


def test_funds_are_cached_briefly():
    api = FakeApi()
    a = account(api)
    a.funds()
    a.funds()
    assert [c for c, _ in api.calls].count("Limits") == 1


def test_a_futures_quote_has_depth_lot_size_and_point_value():
    q = account(FakeApi()).quote("MCX:SILVERM30NOV26")
    assert (q["ltp"], q["bid"], q["ask"], q["lot_size"], q["point_value"], q["quantity_unit"]) == (211200.0, 211190.0, 211210.0, 5, 5.0, "lots")
    assert q["change"] == pytest.approx(-2700.0) and q["expiry"] == "30-NOV-2026"


def test_a_futures_preview_sends_lots_times_lot_size_and_values_the_contract():
    api = FakeApi()
    p = account(api).preview("MCX:SILVERM30NOV26", "SELL", 2)
    (margin_call,) = [pl for c, pl in api.calls if c == "GetOrderMargin"]
    assert margin_call == {"actid": "FT1", "exch": "MCX", "tsym": "SILVERM30NOV26", "qty": "10", "prc": "211200.0",
                           "prctyp": "LMT", "prd": "M", "trantype": "S"}
    assert (p["units"], p["contract_value"], p["margin_required"], p["product_name"]) == (10, 2112000.0, 30259.8, "NRML")
    assert p["available"] == 45000.0 and p["shortfall"] == 0.0 and p["preview_only"] is True


def test_a_stock_preview_is_in_shares_and_defaults_to_delivery():
    p = account(FakeApi(cash="1000.00")).preview("NSE:SBIN-EQ", "BUY", 10, price=960.0)
    assert (p["product"], p["units"], p["price"], p["contract_value"]) == ("C", 10, 960.0, 9600.0)
    assert p["shortfall"] == pytest.approx(30259.8 + 4000.0)          # available is 1000 - 5000 = -4000


def test_a_product_the_account_does_not_have_is_refused_before_asking_the_broker():
    api = FakeApi()
    with pytest.raises(ValueError, match="not enabled for NSE"):
        account(api).preview("NSE:SBIN-EQ", "BUY", 1, product="M")
    assert "GetOrderMargin" not in [c for c, _ in api.calls]


def test_an_existing_position_in_the_same_contract_and_product_is_flagged():
    api = FakeApi(positions=[{"exch": "MCX", "tsym": "SILVERM30NOV26", "prd": "M", "netqty": "-5", "netavgprc": "212000"},
                             {"exch": "MCX", "tsym": "SILVERM30NOV26", "prd": "I", "netqty": "5"}])
    assert account(api).preview("MCX:SILVERM30NOV26", "BUY", 1)["existing_position"] == {"net_quantity": -5, "product": "M", "avg_price": 212000.0}
    assert account(FakeApi()).preview("MCX:SILVERM30NOV26", "BUY", 1)["existing_position"] is None


def test_an_index_can_be_quoted_but_not_ordered():
    with pytest.raises(ValueError, match="index cannot be traded"):
        account(FakeApi()).preview("NSE:NIFTYIT-INDEX", "BUY", 1)


def test_bad_input_is_refused():
    a = account(FakeApi())
    for args in (("MCX:SILVERM30NOV26", "HOLD", 1), ("MCX:SILVERM30NOV26", "BUY", 0)):
        with pytest.raises(ValueError):
            a.preview(*args)


def test_the_endpoints_say_so_in_demo_mode(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tests.terminal.helpers import ist
    from trading_service.terminal import api
    from trading_service.terminal.feed.mock import MockFeed, MockHistory, SimClock
    from trading_service.terminal.runtime import Terminal
    clock = SimClock(ist(2026, 10, 19, 10, 0, 30), speed=0)
    app = FastAPI()
    api.mount(app)
    app.state.terminal = Terminal(db, MockFeed(clock), MockHistory(), clock.now, mode="demo", hub_interval=0)
    with TestClient(app) as c:
        assert c.get("/terminal/account/funds").status_code == 503
        assert c.post("/terminal/order-preview", json={"symbol": "MCX:SILVERM30NOV26", "side": "BUY", "quantity": 1}).status_code == 503
