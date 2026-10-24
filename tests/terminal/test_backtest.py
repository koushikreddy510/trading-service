"""Backtests replay history through the real rule engine, so these tests describe prices and read trades back.

Daily bars start on Mon 2026-01-05, one per weekday: index 0 = Jan 5 ... index 4 = Jan 9, index 5 = Jan 12.
The rule used unless stated: BUY when the close crosses above 110, stop 5% below entry, target 10% above.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests.terminal.helpers import ist
from trading_service.terminal import backtest
from trading_service.terminal.backtest import BacktestError, run_backtest
from trading_service.terminal.candles import Candle
from trading_service.terminal.market import MinuteStore

A, B = "NSE:AAA-EQ", "NSE:BBB-EQ"
START = date(2026, 1, 5)


def weekdays(first, n):
    d, out = first, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def add_bars(db, symbol, bars, first=START):
    """bars: (open, high, low, close) per weekday."""
    for d, (o, h, l, c) in zip(weekdays(first, len(bars)), bars):
        db.execute("INSERT INTO ohlcv_1d (tradingsymbol, date, open, high, low, close, volume) VALUES (:s, :d, :o, :h, :l, :c, 1000)",
                   s=symbol, d=d, o=o, h=h, l=l, c=c)


def flat(c, spread=0.5):
    return (c, c + spread, c - spread, c)


# bars 0-3 sit below 110; bar 4 closes at 111 and crosses -> entry 111, stop 105.45, target 122.1
LEAD = [flat(100)] * 3 + [flat(105)] + [(105, 111.5, 104.5, 111)]


def rule(**over):
    r = {"timeframe": "1D", "side": "BUY", "name": "breakout",
         "condition": {"type": "cross", "direction": "above", "a": {"kind": "close"}, "b": {"kind": "value", "value": 110}},
         "stop": {"type": "pct", "value": 5}, "target": {"type": "pct", "value": 10}}
    r.update(over)
    return r


def go(db, **over):
    body = {"start": "2026-01-05", "end": "2026-02-27", "capital": 100000, "allocation_pct": 10, "symbols": [A], "rules": [rule()]}
    body.update(over)
    return run_backtest(db, body)


class TestExits:
    def test_the_target_is_hit_inside_a_later_candle(self, db):
        add_bars(db, A, LEAD + [(111, 118, 110, 116), (116, 123, 115, 121)])
        r = go(db)
        (t,) = r["trades"]
        assert (t["entry_price"], t["quantity"], t["exit_reason"]) == (111, 90, "target")      # 10% of 100000 = 10000 -> 90 shares
        assert t["exit_price"] == pytest.approx(122.1)
        assert t["pnl"] == pytest.approx((122.1 - 111) * 90)
        assert r["summary"]["final_equity"] == pytest.approx(100000 + t["pnl"])

    def test_the_stop_is_hit_at_its_price(self, db):
        add_bars(db, A, LEAD + [(111, 112, 104, 106)])
        (t,) = go(db)["trades"]
        assert (t["exit_reason"], t["exit_price"]) == ("stop", pytest.approx(105.45))
        assert t["pnl"] < 0

    def test_a_candle_touching_both_takes_the_stop(self, db):
        add_bars(db, A, LEAD + [(111, 125, 100, 110)])
        (t,) = go(db)["trades"]
        assert t["exit_reason"] == "stop"

    def test_a_gap_through_the_stop_fills_at_the_open(self, db):
        add_bars(db, A, LEAD + [(100, 101, 99, 100)])
        (t,) = go(db)["trades"]
        assert (t["exit_reason"], t["exit_price"]) == ("stop", 100)

    def test_the_entry_candle_cannot_stop_itself_out(self, db):
        add_bars(db, A, LEAD + [(111, 112, 111, 111.5)])       # the entry candle (index 4) already ranged 104.5-111.5
        r = go(db)
        assert r["trades"] == [] and r["summary"]["open_positions"] == 1

    def test_an_exit_condition_closes_at_that_candles_close(self, db):
        add_bars(db, A, LEAD + [(111, 113, 109, 112), (112, 112.5, 106, 107.5)])
        exit_rule = {"type": "compare", "op": "<", "a": {"kind": "close"}, "b": {"kind": "value", "value": 108}}
        (t,) = go(db, rules=[rule(exit_condition=exit_rule)])["trades"]
        assert (t["exit_reason"], t["exit_price"]) == ("exit_rule", 107.5)

    def test_a_trailing_stop_follows_closes_then_stops_the_trade_out(self, db):
        # closes 116 then 120: a 2% trail from the close puts the stop at 117.6; the next candle's low 117 touches it
        add_bars(db, A, LEAD + [(111, 117, 111, 116), (116, 120, 115, 120), (120, 121, 117, 118)])
        (t,) = go(db, rules=[rule(target=None, trail={"type": "pct", "value": 2})])["trades"]
        assert (t["exit_reason"], t["exit_price"]) == ("stop", pytest.approx(117.6))
        assert t["pnl"] > 0


class TestPortfolio:
    def test_positions_still_open_stay_open_and_are_marked_to_the_last_close(self, db):
        add_bars(db, A, LEAD + [(111, 114, 110, 113)])
        r = go(db)
        (o,) = r["open"]
        assert (o["quantity"], o["last_price"], o["unrealized_pnl"]) == (90, 113, 180)
        assert r["trades"] == []
        assert r["summary"]["final_equity"] == pytest.approx(100180) and r["summary"]["unrealized_pnl"] == 180

    def test_cash_runs_out_and_the_second_entry_is_rejected(self, db):
        add_bars(db, A, LEAD + [flat(112)])
        add_bars(db, B, LEAD + [flat(112)])
        r = go(db, symbols=[A, B], allocation_pct=100)
        assert r["summary"]["open_positions"] == 1
        assert r["rejected"] == {"not enough cash for one share": 1}

    def test_the_position_limit_applies_across_stocks(self, db):
        add_bars(db, A, LEAD + [flat(112)])
        add_bars(db, B, LEAD + [flat(112)])
        r = go(db, symbols=[A, B], max_positions=1)
        assert r["summary"]["open_positions"] == 1 and r["rejected"] == {"position limit reached": 1}

    def test_fees_are_charged_on_both_sides(self, db):
        add_bars(db, A, LEAD + [(111, 123, 110, 121)])
        (t,) = go(db, cost_pct=0.1)["trades"]
        gross = (122.1 - 111) * 90
        assert t["pnl"] == pytest.approx(gross - 0.001 * 90 * (111 + 122.1), abs=0.01)

    def test_the_equity_curve_has_a_point_per_trading_day_and_a_drawdown(self, db):
        add_bars(db, A, LEAD + [(111, 112, 104, 106)])
        r = go(db)
        assert [p["date"] for p in r["equity"]] == [d.isoformat() for d in weekdays(START, 6)]
        assert r["equity"][4]["equity"] == 100000 and r["equity"][4]["open"] == 1       # Jan 9: just entered, marked at entry
        assert r["equity"][5]["equity"] < 100000 and r["equity"][5]["open"] == 0        # Jan 12: stopped out
        assert r["summary"]["max_drawdown_pct"] == pytest.approx((100000 - r["equity"][5]["equity"]) / 1000, abs=0.01) and r["summary"]["losses"] == 1


def with_history(db, symbol, bars, before_price):
    """`bars` start on the backtest's first day; 60+ earlier bars at `before_price` give the screener something to measure."""
    first = date(2025, 10, 6)
    prefix = [d for d in weekdays(first, 200) if d < START]
    add_bars(db, symbol, [flat(before_price)] * len(prefix), first)
    add_bars(db, symbol, bars)


class TestUniverse:
    SCREEN = {"filters": [{"field": "close", "op": ">=", "value": 100}], "limit": 10}

    def test_a_screen_picks_the_stocks_as_of_each_date_without_looking_ahead(self, db):
        with_history(db, A, [flat(90)] * 5 + [flat(105)] * 20, 90)      # A first closes above 100 on Jan 12
        with_history(db, B, [flat(120)] * 25, 120)
        r = go(db, symbols=None, screen=self.SCREEN, rescreen_days=7, end="2026-02-06")
        first, joined = r["universe"]
        assert (first["date"], first["added"]) == ("2026-01-05", [B])
        assert (joined["date"], joined["added"]) == ("2026-01-19", [A])    # the Jan 12 screen only sees closes up to Jan 9

    def test_a_screen_can_use_financial_data_which_is_todays_not_historic(self, db):
        with_history(db, A, [flat(105)] * 25, 105)
        with_history(db, B, [flat(120)] * 25, 120)
        for sym, roce in ((A, 25.0), (B, 5.0)):
            db.execute("INSERT INTO terminal_screener_metrics (run_date, symbol, asof, roce_pct) VALUES (:d, :s, :d, :r)", d=date(2026, 10, 16), s=sym, r=roce)
        r = go(db, symbols=None, screen={"filters": [{"field": "roce_pct", "op": ">=", "value": 15}]}, end="2026-01-30")
        assert [u["added"] for u in r["universe"]] == [[A]]
        assert any("financial-data" in n for n in r["notes"])

    def test_a_screen_can_be_limited_to_a_base_list(self, db):
        with_history(db, A, [flat(105)] * 25, 105)
        with_history(db, B, [flat(120)] * 25, 120)
        r = go(db, symbols=None, screen={**self.SCREEN, "base": [A]}, end="2026-01-30")
        assert [u["added"] for u in r["universe"]] == [[A]]

    def test_a_stock_that_leaves_the_screen_keeps_its_open_position(self, db):
        with_history(db, A, LEAD + [flat(112)] * 3 + [flat(90)] * 10, 105)
        r = go(db, symbols=None, screen=self.SCREEN, rules=[rule(stop={"type": "pct", "value": 50}, target=None)], end="2026-01-30")
        assert [u["removed"] for u in r["universe"]] == [[], [A]] and r["universe"][1]["date"] == "2026-01-19"
        assert r["trades"] == [] and r["summary"]["open_positions"] == 1

    def test_a_stock_outside_the_screen_gets_no_entries(self, db):
        with_history(db, A, LEAD + [flat(112)], 105)
        r = go(db, symbols=None, screen={"filters": [{"field": "close", "op": ">=", "value": 500}]})
        assert r["trades"] == [] and r["open"] == [] and r["universe"][0]["count"] == 0


class TestIsolation:
    def test_the_real_tables_are_untouched_and_the_scratch_schema_is_gone(self, db):
        add_bars(db, A, LEAD + [(111, 123, 110, 121)])
        go(db)
        for table in ("terminal_rules", "terminal_signals", "terminal_paper_positions"):
            assert db.scalar(f"SELECT count(*) FROM {table}") == 0
        assert db.scalar("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'bt\\_%'") == 0

    def test_a_failed_run_still_cleans_up(self, db, monkeypatch):
        add_bars(db, A, LEAD)
        monkeypatch.setattr(backtest.Backtest, "_run", lambda self: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            go(db)
        assert db.scalar("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'bt\\_%'") == 0

    def test_saved_rules_can_be_used_as_templates(self, db):
        from trading_service.terminal.rules import RuleRepo, rule_from_payload
        add_bars(db, A, LEAD + [(111, 123, 110, 121)])
        saved = RuleRepo(db).save(rule_from_payload({**rule(), "symbol": "NSE:OTHER-EQ", "quantity": 7}))
        r = go(db, rules=None, rule_ids=[saved.id])
        assert r["trades"][0]["symbol"] == A and r["trades"][0]["quantity"] == 90      # sized by capital, not the saved quantity


class TestIntraday:
    def test_an_hourly_rule_enters_on_the_hour_and_a_minute_bar_triggers_the_stop(self, db):
        # 100 until 11:30, +0.1 a minute to 106 by 12:30 (the 11:15-12:15 candle closes at 104.5), a drop to 103 at 12:40
        minutes, price, t0 = [], 100.0, ist(2026, 3, 2, 9, 15)
        for i in range(375):
            nxt = 100.0 + 0.1 * min(max(i - 134, 0), 60) if i < 205 else 103.0
            minutes.append(Candle(t0 + timedelta(minutes=i), price, max(price, nxt) + 0.01, min(price, nxt) - 0.01, nxt, 10))
            price = nxt
        MinuteStore(db).save(A, minutes)
        r = go(db, start="2026-03-02", end="2026-03-02", rules=[{
            "timeframe": "1H", "side": "BUY", "name": "hourly",
            "condition": {"type": "cross", "direction": "above", "a": {"kind": "close"}, "b": {"kind": "value", "value": 104}},
            "stop": {"type": "pct", "value": 1}}])
        (t,) = r["trades"]
        # compared as instants: the same moment prints as +05:30 or +00:00 depending on the database's time zone
        assert datetime.fromisoformat(t["entry_time"]) == ist(2026, 3, 2, 12, 15) and t["entry_price"] == pytest.approx(104.5)
        assert datetime.fromisoformat(t["exit_time"]) == ist(2026, 3, 2, 12, 41)          # the minute that dropped through the stop
        assert (t["exit_reason"], t["exit_price"]) == ("stop", pytest.approx(104.5 * 0.99))


class TestParameters:
    @pytest.mark.parametrize("over, message", [
        ({"end": "2025-12-01"}, "end is before start"),
        ({"end": "2999-01-01"}, "future"),
        ({"rules": None}, "at least one rule"),
        ({"symbols": None}, "exactly one of"),
        ({"watchlist_id": "x"}, "exactly one of"),
        ({"allocation_pct": 0}, "allocation_pct"),
        ({"allocation_pct": 150}, "allocation_pct"),
        ({"capital": -5}, "capital"),
        ({"max_positions": 0}, "max_positions"),
        ({"rescreen_days": 0}, "rescreen_days"),
        ({"start": "soon"}, "start"),
        ({"rules": [{"timeframe": "1D"}]}, "rule:"),
        ({"rule_ids": ["nope"], "rules": None}, "does not exist"),
        ({"symbols": None, "screen": {"filters": [{"field": "nonsense", "op": ">", "value": 1}]}}, "unknown field"),
        ({"symbols": None, "screen": {"preset": "nope"}}, "unknown preset"),
        ({"symbols": None, "screen": {"sort": "close"}}, "preset or at least one filter"),
    ])
    def test_bad_requests_are_explained(self, db, over, message):
        with pytest.raises(BacktestError, match=message):
            go(db, **over)

    def test_a_watchlist_is_a_universe(self, db):
        from trading_service.terminal.watchlists import WatchlistRepo
        add_bars(db, A, LEAD + [(111, 123, 110, 121)])
        wl = WatchlistRepo(db).create("Mine", [A])
        r = go(db, symbols=None, watchlist_id=wl["id"])
        assert [t["symbol"] for t in r["trades"]] == [A]


class TestApi:
    @pytest.fixture
    def client(self, db):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from trading_service.terminal import api
        from trading_service.terminal.feed.mock import MockFeed, MockHistory, SimClock
        from trading_service.terminal.runtime import Terminal
        clock = SimClock(ist(2026, 10, 19, 10, 0, 30), speed=0)
        app = FastAPI()
        api.mount(app)
        app.state.terminal = Terminal(db, MockFeed(clock), MockHistory(), clock.now, mode="test", hub_interval=0)
        with TestClient(app) as c:
            yield c

    @staticmethod
    def wait(client, backtest_id):
        import time
        for _ in range(200):
            got = client.get(f"/terminal/backtests/{backtest_id}").json()
            if got["status"] != "running":
                return got
            time.sleep(0.05)
        raise AssertionError("backtest did not finish")

    def test_a_backtest_runs_in_the_background_and_can_be_read_back_and_deleted(self, db, client):
        add_bars(db, A, LEAD + [(111, 123, 110, 121)])
        posted = client.post("/terminal/backtests", json={"start": "2026-01-05", "end": "2026-02-27", "symbols": [A], "rules": [rule()], "capital": 100000})
        assert posted.status_code == 202
        done = self.wait(client, posted.json()["id"])
        assert done["status"] == "done" and done["result"]["summary"]["trades"] == 1 and done["params"]["capital"] == 100000
        (row,) = client.get("/terminal/backtests").json()["backtests"]
        assert row["summary"]["trades"] == 1 and "result" not in row                      # the list carries the summary only
        assert client.delete(f"/terminal/backtests/{done['id']}").status_code == 204
        assert client.get(f"/terminal/backtests/{done['id']}").status_code == 404

    def test_a_bad_request_is_a_400_and_nothing_is_stored(self, db, client):
        r = client.post("/terminal/backtests", json={"start": "2026-01-05", "end": "2026-02-27", "rules": [rule()]})
        assert r.status_code == 400 and "exactly one of" in r.json()["detail"]
        assert client.get("/terminal/backtests").json() == {"backtests": []}

    def test_a_run_that_fails_says_why(self, db, client, monkeypatch):
        monkeypatch.setattr(backtest.Backtest, "_run", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        add_bars(db, A, LEAD)
        done = self.wait(client, client.post("/terminal/backtests", json={"start": "2026-01-05", "end": "2026-02-27", "symbols": [A], "rules": [rule()]}).json()["id"])
        assert (done["status"], done["error"]) == ("failed", "boom")

    def test_only_two_run_at_once_and_stale_runs_are_failed_on_start(self, db, client):
        from trading_service.terminal.backtest_jobs import BacktestStore
        for i in "ab":
            db.execute("INSERT INTO terminal_backtests (id, params) VALUES (:i, '{}')", i=i)
        r = client.post("/terminal/backtests", json={"start": "2026-01-05", "end": "2026-02-27", "symbols": [A], "rules": [rule()]})
        assert r.status_code == 400 and "already running" in r.json()["detail"]
        assert BacktestStore(db).fail_stale() == 2
        assert {x["status"] for x in client.get("/terminal/backtests").json()["backtests"]} == {"failed"}

    def test_unknown_backtest_is_404(self, client):
        assert client.get("/terminal/backtests/nope").status_code == 404
        assert client.delete("/terminal/backtests/nope").status_code == 404


class TestTwoLevelExitCash:
    """A 2-level exit must give back all its capital and both halves' profit (the first half used to vanish)."""

    def test_capital_and_both_halves_come_back(self):
        from datetime import datetime, timezone
        from trading_service.terminal.backtest import Portfolio
        from trading_service.terminal.paper import PaperPosition

        class Book:
            def __init__(self):
                self.positions = []

            def open_positions(self):
                return [p for p in self.positions if p.status == "open"]

        book, pf = Book(), Portfolio(10_000, 50, None, 0)
        p = PaperPosition(id="p1", symbol="NSE:X-EQ", side="BUY", quantity=100, entry_price=10.0, stop=9.0, target=None,
                          status="open", opened_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        book.positions.append(p)
        pf.settle(book)
        assert pf.cash == 9_000
        p.quantity, p.exit_level, p.exit_qty_1, p.exit_price_1, p.exit_pnl_1 = 50, 2, 50, 11.0, 50.0   # level 1: half sold at 11
        pf.settle(book)
        assert pf.cash == pytest.approx(9_000 + 500 + 50)
        assert pf.equity({"NSE:X-EQ": 12.0}) == pytest.approx(9_550 + 500 + 100)
        p.status, p.exit_price, p.realized_pnl = "closed", 12.0, 100.0                                 # the rest sold at 12
        pf.settle(book)
        assert pf.cash == pytest.approx(10_150) and pf.open == {}
        (closed, fee), = pf.closed
        assert closed is p and fee == 0
