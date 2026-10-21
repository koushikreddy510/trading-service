"""Screener building blocks: field groups, one-click conditions, the dual-Supertrend metrics, saved screens."""
from datetime import date, timedelta

import pytest

from tests.terminal.helpers import ist
from trading_service.terminal import screener
from trading_service.terminal.candles import Candle


def bars(closes):
    return [Candle(ist(2025, 1, 1) + timedelta(days=i), c, c * 1.01, c * 0.99, c, 1000) for i, c in enumerate(closes)]


def test_both_supertrends_agree_up_in_a_steady_rise_and_down_in_a_steady_fall():
    up = screener.compute_metrics(bars([100 + i for i in range(80)]))
    down = screener.compute_metrics(bars([200 - i for i in range(80)]))
    assert (up["supertrend_dir"], up["supertrend_52_dir"], up["st_agree"]) == (1, 1, 1)
    assert (down["st_agree"], down["st_agree_fresh"]) == (-1, 0)          # agreeing for a long time: not fresh


def test_a_fresh_agreement_is_flagged():
    closes = [200 - i for i in range(60)] + [140 + 4 * i for i in range(12)]     # a fall, then a sharp turn up
    m = screener.compute_metrics(bars(closes))
    agree_seen = [screener.compute_metrics(bars(closes[:k]))["st_agree"] for k in range(62, 73)]
    first_up = next(k for k, a in zip(range(62, 73), agree_seen) if a == 1)
    assert screener.compute_metrics(bars(closes[:first_up]))["st_agree_fresh"] == 1
    assert m["st_agree"] == 1


def test_every_quick_condition_is_a_valid_screen_and_every_field_has_a_group(db):
    for q in screener.QUICK:
        screener.run_screen(db, q["filters"], "symbol", False, 1, run_date=date(1900, 1, 1))
    assert all(screener.group_of(f) != "Other" for f in screener.FILTERABLE)


def test_saved_screens_round_trip_replace_by_name_and_delete(db):
    a = screener.save_screen(db, "Dual ST + ROCE", [{"field": "st_agree", "op": "=", "value": 1}, {"field": "roce_pct", "op": ">", "value": 15}], "ret_60d", True)
    screener.save_screen(db, "dual st + roce", [{"field": "st_agree", "op": "=", "value": 1}], "ret_20d", False)
    (s,) = screener.list_saved(db)
    assert s["id"] == a["id"] and s["sort"] == "ret_20d" and len(s["filters"]) == 1
    with pytest.raises(screener.ScreenError):
        screener.save_screen(db, "bad", [{"field": "nope", "op": ">", "value": 1}], "symbol", True)
    assert screener.delete_saved(db, a["id"]) and screener.list_saved(db) == []


def test_market_cap_falls_back_to_screener_in_when_the_symbols_table_has_zero(db):
    db.execute("INSERT INTO terminal_screener_metrics (run_date, symbol, asof, close) VALUES ('2026-10-21', 'NSE:X-EQ', '2026-10-21', 100)")
    db.execute("INSERT INTO symbols (tradingsymbol, name, market_cap) VALUES ('NSE:X-EQ', 'X Ltd', 0)")
    if not db.table_exists("stock_financials"):
        db.execute("CREATE TABLE stock_financials (tradingsymbol TEXT, market_cap_cr DOUBLE PRECISION, scraped_at TIMESTAMPTZ)")
    cols = {r["column_name"] for r in db.rows("SELECT column_name FROM information_schema.columns WHERE table_name = 'stock_financials' "
                                                "AND is_nullable = 'NO' AND column_default IS NULL")}
    values = {"tradingsymbol": "NSE:X-EQ", "market_cap_cr": 2500, "nse_symbol": "X", "result_type": "quarterly", "period": "Sep 2026",
              "company_name": "X Ltd", "source": "screener.in"}
    keys = ["tradingsymbol", "market_cap_cr"] + sorted(c for c in cols if c not in ("tradingsymbol", "market_cap_cr"))
    db.execute(f"INSERT INTO stock_financials ({', '.join(keys)}, scraped_at) VALUES ({', '.join(':' + k for k in keys)}, now())",
               **{k: values.get(k, "x") for k in keys})
    out = screener.run_screen(db, [{"field": "market_cap", "op": ">", "value": 1000}], "symbol", False, 10)
    assert [r["symbol"] for r in out["rows"]] == ["NSE:X-EQ"] and out["rows"][0]["market_cap"] == 2500
