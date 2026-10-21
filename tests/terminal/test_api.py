"""The HTTP + websocket API the UI talks to, exercised like the UI would.

Each test class is one screen/feature of the terminal: watchlists, chart data,
drawings, rules, positions, financials, screener and the live socket.
"""
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.terminal.helpers import ist
from trading_service.terminal import api
from trading_service.terminal.feed.mock import MockFeed, MockHistory, SimClock
from trading_service.terminal.runtime import Terminal
from trading_service.terminal.ticks import Tick

SYM = "NSE:SBIN-EQ"


@pytest.fixture
def terminal(db):
    clock = SimClock(ist(2026, 10, 19, 10, 0, 30), speed=0)
    t = Terminal(db, MockFeed(clock), MockHistory(), clock.now, mode="test", hub_interval=0)
    t.watchlists.ensure_default()
    return t


@pytest.fixture
def client(terminal):
    app = FastAPI()
    api.mount(app)
    app.state.terminal = terminal
    with TestClient(app) as c:
        c.terminal = terminal
        yield c


def test_when_the_terminal_is_not_running_endpoints_answer_503():
    app = FastAPI()
    api.mount(app)
    app.state.terminal = None
    assert TestClient(app).get("/terminal/status").status_code == 503


def test_status_and_indicator_catalogue(client):
    s = client.get("/terminal/status").json()
    assert s["mode"] == "test" and s["clients"] == 0
    specs = {i["name"]: i for i in client.get("/terminal/indicators").json()["indicators"]}
    assert specs["bollinger"]["outputs"] == ["middle", "upper", "lower"]
    assert specs["rsi"]["defaults"] == {"period": 14}


class TestCandles:
    def test_returns_candles_backfilled_from_the_broker(self, client):
        r = client.get("/terminal/candles", params={"symbol": SYM, "timeframe": "1H", "limit": 50})
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == SYM and body["timeframe"] == "1H"
        candles = body["candles"]
        assert 0 < len(candles) <= 50
        assert set(candles[0]) == {"time", "open", "high", "low", "close", "volume"}
        assert [c["time"] for c in candles] == sorted(c["time"] for c in candles)

    def test_the_newest_bar_is_flagged_as_forming_while_its_hour_is_running(self, client):
        # clock is 10:00:30 and 1H bars run 09:15-10:15, so the last bar is still forming
        assert client.get("/terminal/candles", params={"symbol": SYM, "timeframe": "1H"}).json()["forming"] is True

    def test_lower_timeframes_have_more_bars_than_higher_ones(self, client):
        n = {tf: len(client.get("/terminal/candles", params={"symbol": SYM, "timeframe": tf}).json()["candles"]) for tf in ("5m", "1H")}
        assert n["5m"] > n["1H"]

    def test_history_is_fetched_from_the_broker_only_once_per_cooldown(self, client):
        calls = []
        original = client.terminal.history.fetch_1m
        client.terminal.history.fetch_1m = lambda *a: calls.append(a) or original(*a)
        for _ in range(3):
            client.get("/terminal/candles", params={"symbol": SYM, "timeframe": "5m"})
        assert len(calls) == 1

    @pytest.mark.parametrize("params", [{"symbol": SYM, "timeframe": "7m"}, {"symbol": "sbin", "timeframe": "1H"}, {"timeframe": "1H"}])
    def test_bad_requests_are_rejected(self, client, params):
        assert client.get("/terminal/candles", params=params).status_code in (400, 422)

    def test_symbols_are_case_insensitive(self, client):
        assert client.get("/terminal/candles", params={"symbol": "nse:sbin-eq", "timeframe": "1H"}).json()["symbol"] == SYM


class TestWatchlists:
    def test_a_default_watchlist_exists(self, client):
        assert [w["name"] for w in client.get("/terminal/watchlists").json()["watchlists"]] == ["Watchlist"]

    def test_create_add_reorder_remove_rename_delete(self, client):
        wl = client.post("/terminal/watchlists", json={"name": "Banks", "symbols": ["NSE:SBIN-EQ"]})
        assert wl.status_code == 201
        wid = wl.json()["id"]

        added = client.post(f"/terminal/watchlists/{wid}/symbols", json={"symbols": ["nse:hdfcbank-eq", "NSE:SBIN-EQ"]}).json()
        assert added["added"] == ["NSE:HDFCBANK-EQ"]

        order = client.put(f"/terminal/watchlists/{wid}/order", json={"symbols": ["NSE:HDFCBANK-EQ"]}).json()
        assert order["symbols"] == ["NSE:HDFCBANK-EQ", "NSE:SBIN-EQ"]

        assert client.delete(f"/terminal/watchlists/{wid}/symbols/NSE:SBIN-EQ").json()["symbols"] == ["NSE:HDFCBANK-EQ"]
        assert client.patch(f"/terminal/watchlists/{wid}", json={"name": "PSU Banks"}).json()["name"] == "PSU Banks"
        assert client.delete(f"/terminal/watchlists/{wid}").status_code == 204
        assert client.delete(f"/terminal/watchlists/{wid}").status_code == 404

    def test_errors_are_readable(self, client):
        assert "already exists" in client.post("/terminal/watchlists", json={"name": "watchlist"}).json()["detail"]
        assert client.post("/terminal/watchlists", json={"name": ""}).status_code == 400
        wid = client.get("/terminal/watchlists").json()["watchlists"][0]["id"]
        assert client.post(f"/terminal/watchlists/{wid}/symbols", json={"symbols": ["SBIN"]}).status_code == 400
        assert client.post("/terminal/watchlists/nope/symbols", json={"symbols": [SYM]}).status_code == 404
        assert client.delete(f"/terminal/watchlists/{wid}/symbols/NSE:ZZZ-EQ").status_code == 404

    def test_symbol_search(self, client, db):
        db.execute("INSERT INTO symbols (tradingsymbol, name, sector) VALUES ('NSE:SBIN-EQ', 'State Bank of India', 'Banks')")
        assert client.get("/terminal/symbols/search", params={"q": "state bank"}).json()["results"][0]["symbol"] == SYM


LINE = {"symbol": SYM, "kind": "horizontal_line", "points": [{"time": 1_700_000_000, "price": 100}]}
TREND = {"symbol": SYM, "kind": "trend_line", "points": [{"time": 1_700_000_000, "price": 90}, {"time": 1_700_090_000, "price": 95}]}


class TestDrawings:
    def test_create_list_move_delete_a_trendline(self, client):
        made = client.post("/terminal/drawings", json={**TREND, "label": "support", "style": {"color": "#2962ff"}})
        assert made.status_code == 201
        d = made.json()
        assert client.get("/terminal/drawings", params={"symbol": SYM}).json()["drawings"] == [d]

        moved = client.put(f"/terminal/drawings/{d['id']}", json={"points": [{"time": 1_700_000_000, "price": 91}, {"time": 1_700_090_000, "price": 96}]}).json()
        assert moved["points"][0]["price"] == 91 and moved["label"] == "support" and moved["symbol"] == SYM

        assert client.delete(f"/terminal/drawings/{d['id']}").status_code == 204
        assert client.get("/terminal/drawings", params={"symbol": SYM}).json()["drawings"] == []

    def test_drawings_are_per_symbol(self, client):
        client.post("/terminal/drawings", json=LINE)
        assert client.get("/terminal/drawings", params={"symbol": "NSE:TCS-EQ"}).json()["drawings"] == []

    @pytest.mark.parametrize("bad", [
        {**LINE, "kind": "fibonacci"},
        {**TREND, "points": [{"time": 5, "price": 90}]},
        {**TREND, "points": [{"time": 5, "price": 90}, {"time": 5, "price": 91}]},
        {**LINE, "points": [{"time": 5, "price": -1}]},
        {**LINE, "symbol": "bad"},
    ])
    def test_invalid_drawings_are_rejected(self, client, bad):
        assert client.post("/terminal/drawings", json=bad).status_code == 400

    def test_a_line_used_by_a_rule_cannot_be_deleted(self, client):
        line = client.post("/terminal/drawings", json=LINE).json()
        rule = client.post("/terminal/rules", json=rule_body(line["id"], name="Breakout")).json()
        blocked = client.delete(f"/terminal/drawings/{line['id']}")
        assert blocked.status_code == 409 and "Breakout" in blocked.json()["detail"]
        client.delete(f"/terminal/rules/{rule['id']}")
        assert client.delete(f"/terminal/drawings/{line['id']}").status_code == 204


def rule_body(drawing_id, **over):
    body = {
        "symbol": SYM, "timeframe": "1H", "side": "BUY", "quantity": 10, "name": "R",
        "condition": {"type": "cross", "direction": "above", "a": {"kind": "close"}, "b": {"kind": "line", "drawing_id": drawing_id}},
        "stop": {"type": "candle"}, "target": {"type": "rr", "ratio": 2},
    }
    body.update(over)
    return body


class TestRules:
    def test_create_toggle_update_delete(self, client):
        line = client.post("/terminal/drawings", json=LINE).json()
        made = client.post("/terminal/rules", json=rule_body(line["id"]))
        assert made.status_code == 201
        rid = made.json()["id"]
        assert made.json()["enabled"] is True

        assert client.patch(f"/terminal/rules/{rid}/enabled", json={"enabled": False}).json()["enabled"] is False
        updated = client.put(f"/terminal/rules/{rid}", json=rule_body(line["id"], quantity=25)).json()
        assert updated["quantity"] == 25 and updated["id"] == rid
        assert [r["id"] for r in client.get("/terminal/rules", params={"symbol": SYM}).json()["rules"]] == [rid]
        assert client.delete(f"/terminal/rules/{rid}").status_code == 204
        assert client.get("/terminal/rules").json()["rules"] == []

    def test_a_rule_must_point_at_a_drawing_that_exists(self, client):
        r = client.post("/terminal/rules", json=rule_body("ghost"))
        assert r.status_code == 400 and "ghost" in r.json()["detail"]

    @pytest.mark.parametrize("over, fragment", [
        ({"quantity": 0}, "quantity"),
        ({"side": "HOLD"}, "side"),
        ({"timeframe": "7m"}, "timeframe"),
        ({"stop": {"type": "pct", "value": -1}}, "positive number"),
    ])
    def test_invalid_rules_explain_what_is_wrong(self, client, over, fragment):
        line = client.post("/terminal/drawings", json=LINE).json()
        r = client.post("/terminal/rules", json=rule_body(line["id"], **over))
        assert r.status_code == 400 and fragment in r.json()["detail"]

    def test_an_enabled_rule_subscribes_its_symbol_on_the_feed(self, client):
        line = client.post("/terminal/drawings", json=LINE).json()
        client.post("/terminal/rules", json=rule_body(line["id"]))
        assert SYM in client.terminal.feed._symbols

    def test_indicator_conditions_are_accepted(self, client):
        body = rule_body("x", condition={"type": "compare", "op": ">", "a": {"kind": "indicator", "name": "rsi", "params": {"period": 14}}, "b": {"kind": "value", "value": 60}}, stop={"type": "pct", "value": 2}, target=None)
        assert client.post("/terminal/rules", json=body).status_code == 201

    def test_the_exit_plan_round_trips(self, client):
        line = client.post("/terminal/drawings", json=LINE).json()
        floor = client.post("/terminal/drawings", json=LINE).json()
        plan = {
            "trail": {"type": "line", "drawing_id": floor["id"], "timeframe": "15m"},
            "exit_condition": {"type": "compare", "op": "<", "a": {"kind": "close"}, "b": {"kind": "value", "value": 90}},
            "exit_timeframe": "1D",
        }
        made = client.post("/terminal/rules", json=rule_body(line["id"], **plan)).json()
        assert (made["trail"]["type"], made["trail"]["timeframe"], made["exit_timeframe"]) == ("line", "15m", "1D")
        assert client.get("/terminal/rules").json()["rules"][0]["exit_condition"]["op"] == "<"

    def test_a_line_only_the_trail_uses_is_protected_and_must_exist(self, client):
        line = client.post("/terminal/drawings", json=LINE).json()
        floor = client.post("/terminal/drawings", json=LINE).json()
        ghost = client.post("/terminal/rules", json=rule_body(line["id"], trail={"type": "line", "drawing_id": "ghost"}))
        assert ghost.status_code == 400 and "ghost" in ghost.json()["detail"]
        client.post("/terminal/rules", json=rule_body(line["id"], name="Trailer", trail={"type": "line", "drawing_id": floor["id"]}))
        blocked = client.delete(f"/terminal/drawings/{floor['id']}")
        assert blocked.status_code == 409 and "Trailer" in blocked.json()["detail"]

    def test_a_zone_needs_an_edge_and_a_note_cannot_be_used(self, client):
        zone = client.post("/terminal/drawings", json={"symbol": SYM, "kind": "rectangle", "points": [{"time": 1_700_000_000, "price": 95}, {"time": 1_700_003_600, "price": 105}]}).json()
        note = client.post("/terminal/drawings", json={"symbol": SYM, "kind": "text", "label": "watch", "points": [{"time": 1_700_000_000, "price": 100}]}).json()
        no_edge = client.post("/terminal/rules", json=rule_body(zone["id"]))
        assert no_edge.status_code == 400 and "top or bottom" in no_edge.json()["detail"]
        edged = rule_body(zone["id"], condition={"type": "cross", "direction": "above", "a": {"kind": "close"}, "b": {"kind": "line", "drawing_id": zone["id"], "edge": "top"}})
        assert client.post("/terminal/rules", json=edged).status_code == 201
        assert client.post("/terminal/rules", json=rule_body(note["id"])).status_code == 400
        assert "no price" in client.post("/terminal/rules", json=rule_body(note["id"])).json()["detail"]

    @pytest.mark.parametrize("plan, fragment", [
        ({"trail": {"type": "pct", "value": -1}}, "trail pct"),
        ({"trail": {"type": "nope"}}, "trail type"),
        ({"exit_condition": {"type": "nope"}}, "unknown condition type"),
        ({"exit_condition": {"type": "compare", "op": "<", "a": {"kind": "close"}, "b": {"kind": "value", "value": 1}}, "exit_timeframe": "7m"}, "timeframe"),
    ])
    def test_a_bad_exit_plan_explains_itself(self, client, plan, fragment):
        line = client.post("/terminal/drawings", json=LINE).json()
        r = client.post("/terminal/rules", json=rule_body(line["id"], **plan))
        assert r.status_code == 400 and fragment in r.json()["detail"]

    def test_signals_list_starts_empty(self, client):
        assert client.get("/terminal/signals", params={"symbol": SYM}).json() == {"signals": []}


class TestPositions:
    def open_one(self, client):
        from trading_service.terminal.rules import Signal

        t = client.terminal
        signal = Signal(SYM, "1H", "BUY", ist(2026, 10, 19, 9, 15), 100.0, "open", stop=95.0, target=110.0)
        t.signals.insert_if_new(signal)        # a position always belongs to a recorded signal
        t.book.open_from_signal(signal, 10, t.clock())
        return t

    def test_open_positions_carry_live_pnl_from_the_latest_price(self, client):
        t = self.open_one(client)
        t.hub.on_tick(Tick(SYM, 103.0, t.clock()))
        (p,) = client.get("/terminal/positions").json()["open"]
        assert (p["symbol"], p["entry_price"], p["stop"], p["target"]) == (SYM, 100.0, 95.0, 110.0)
        assert p["last_price"] == 103.0 and p["unrealized_pnl"] == pytest.approx(30.0)

    def test_without_a_live_price_pnl_is_omitted_not_zero(self, client):
        self.open_one(client)
        (p,) = client.get("/terminal/positions").json()["open"]
        assert "unrealized_pnl" not in p

    def test_closing_at_market_uses_the_latest_price_and_moves_it_to_history(self, client):
        t = self.open_one(client)
        pid = client.get("/terminal/positions").json()["open"][0]["id"]
        assert client.post(f"/terminal/positions/{pid}/close").status_code == 409     # no price yet
        t.hub.on_tick(Tick(SYM, 104.0, t.clock()))
        closed = client.post(f"/terminal/positions/{pid}/close").json()
        assert (closed["status"], closed["exit_reason"], closed["exit_price"], closed["realized_pnl"]) == ("closed", "manual", 104.0, 40.0)
        body = client.get("/terminal/positions").json()
        assert body["open"] == [] and [x["id"] for x in body["closed"]] == [pid]

    def test_unknown_position(self, client):
        assert client.post("/terminal/positions/nope/close").status_code == 404


class TestFinancialsAndScreener:
    def test_financials_for_a_symbol_without_data_say_so(self, client):
        assert client.get("/terminal/financials", params={"symbol": SYM}).json()["available"] is False

    def test_financials_return_quarterly_results(self, client, db):
        db.execute(
            "INSERT INTO stock_financials (tradingsymbol, nse_symbol, result_type, period, period_end_date, revenue, net_profit) "
            "VALUES (:s, 'SBIN', 'quarterly', 'Jun 2026', :d, 1000, 120)", s=SYM, d=date(2026, 6, 30),
        )
        body = client.get("/terminal/financials", params={"symbol": SYM}).json()
        assert body["available"] and body["quarterly"][0]["net_profit"] == 120

    def test_financials_carry_the_chart_overlays(self, client, db):
        db.execute("INSERT INTO result_announcements (nse_symbol, result_date, quarter) VALUES ('SBIN', :d, 'Q1')", d=date(2026, 8, 7))
        db.execute(
            "INSERT INTO stock_financials (tradingsymbol, nse_symbol, result_type, period, period_end_date, profit_growth_pct) "
            "VALUES (:s, 'SBIN', 'quarterly', 'Jun 2026', :d, 30)", s=SYM, d=date(2026, 6, 30),
        )
        body = client.get("/terminal/financials", params={"symbol": SYM}).json()
        assert [(e["date"], e["profit_growth_pct"]) for e in body["events"]] == [("2026-08-07", 30)]
        assert body["eps_ttm"] == []   # one quarter is not enough for a trailing year

    def test_scan_then_run_a_preset_and_a_custom_screen(self, client, db):
        for i in range(60):
            db.execute(
                "INSERT INTO ohlcv_1d (tradingsymbol, date, open, high, low, close, volume) VALUES (:s,:d,:c,:c,:c,:c,1000)",
                s=SYM, d=date(2026, 8, 1) + timedelta(days=i), c=100 + i,
            )
        assert client.get("/terminal/screener/preset/uptrend").json() == {"run_date": None, "count": 0, "rows": [], "preset": "uptrend"}

        scan = client.post("/terminal/screener/scan").json()
        assert scan["stored"] == 1

        custom = client.post("/terminal/screener/run", json={"filters": [{"field": "rsi_14", "op": ">", "value": 70}], "sort": "rsi_14"}).json()
        assert [r["symbol"] for r in custom["rows"]] == [SYM]
        assert client.get("/terminal/screener/preset/oversold").json()["count"] == 0

    def test_catalogue_endpoints(self, client):
        names = {p["name"] for p in client.get("/terminal/screener/presets").json()["presets"]}
        assert {"uptrend", "oversold", "volume_breakout"} <= names
        cols = client.get("/terminal/screener/columns").json()
        rsi = next(c for c in cols["columns"] if c["name"] == "rsi_14")
        assert {k: rsi[k] for k in ("label", "unit", "short", "group", "technical")} == {"label": "RSI 14", "unit": "int", "short": "RSI", "group": "Momentum", "technical": True}
        assert ">=" in cols["operators"] and cols["quick"] and "Valuation" in cols["groups"]

    def test_bad_screens_are_400_and_unknown_presets_404(self, client):
        assert client.post("/terminal/screener/run", json={"filters": [{"field": "close; x", "op": ">", "value": 1}]}).status_code == 400
        assert client.get("/terminal/screener/preset/nope").status_code == 404

    def test_recent_results(self, client):
        assert client.get("/terminal/results/recent").json() == {"results": []}


class TestWebsocket:
    def test_hello_then_ticks_for_subscribed_symbols_only(self, client):
        t = client.terminal
        with client.websocket_connect("/terminal/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["mode"] == "test"
            ws.send_json({"type": "subscribe", "symbols": [SYM]})
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}

            client.portal.call(t.hub.on_tick, Tick("NSE:TCS-EQ", 1.0, t.clock()))
            client.portal.call(t.hub.on_tick, Tick(SYM, 101.5, t.clock(), prev_close=100.0))
            msg = ws.receive_json()
            assert msg["type"] == "tick" and msg["quote"]["symbol"] == SYM and msg["quote"]["price"] == 101.5

    def test_subscribing_makes_the_server_watch_the_symbol_and_closing_releases_it(self, client):
        t = client.terminal
        with client.websocket_connect("/terminal/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "subscribe", "symbols": [SYM]})
            ws.send_json({"type": "ping"})
            ws.receive_json()
            assert SYM in t.feed._symbols
        for _ in range(50):
            if SYM not in t.feed._symbols:
                break
            import time

            time.sleep(0.02)
        assert SYM not in t.feed._symbols

    def test_bad_messages_get_an_error_and_do_not_drop_the_connection(self, client):
        with client.websocket_connect("/terminal/ws") as ws:
            ws.receive_json()
            ws.send_text("not json")
            assert ws.receive_json()["type"] == "error"
            ws.send_json({"type": "teleport"})
            assert "unknown message type" in ws.receive_json()["message"]
            ws.send_json({"type": "subscribe", "symbols": ["../etc/passwd"]})
            assert "invalid symbols" in ws.receive_json()["message"]
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}

    def test_the_socket_is_refused_when_the_terminal_is_down(self):
        app = FastAPI()
        api.mount(app)
        app.state.terminal = None
        with pytest.raises(Exception):
            with TestClient(app).websocket_connect("/terminal/ws"):
                pass
