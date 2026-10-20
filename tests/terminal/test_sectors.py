"""Sector / theme grouping and the in-sector comparison.

A *group* is a parent sector ("Banking & Finance"); its members are the symbols whose granular NSE industry
rolls up into it. `compare` puts every member on one table and ranks each column WITHIN the group, 1 = best:
higher is better for returns, ROE and growth; lower is better for P/E and P/B (a loss-maker's negative P/E is
not "cheap", so it is not ranked).
"""
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_service.terminal import api, sectors
from trading_service.terminal.sector_groups import SECTOR_GROUP_MAP, get_parent_sector

BANK, PSU, PHARMA = "Private Sector Bank", "Public Sector Bank", "Pharmaceuticals"


def add_symbol(db, symbol, sector, name=""):
    db.execute("INSERT INTO symbols (tradingsymbol, name, sector) VALUES (:s, :n, :c)", s=symbol, n=name or symbol, c=sector)


def add_ratios(db, symbol, *, cap=None, pe=None, pb=None, roe=None, roce=None, div=None, rev_g=None, profit_g=None, kind="quarterly", period="Jun 2026", end=date(2026, 6, 30)):
    db.execute(
        """INSERT INTO stock_financials (tradingsymbol, nse_symbol, result_type, period, period_end_date, market_cap_cr, pe_ratio, pb_ratio,
               roe_pct, roce_pct, dividend_yield, revenue_growth_pct, profit_growth_pct)
           VALUES (:s, :n, :k, :p, :e, :cap, :pe, :pb, :roe, :roce, :div, :rg, :pg)""",
        s=symbol, n=symbol.split(":")[1][:-3], k=kind, p=period, e=end, cap=cap, pe=pe, pb=pb, roe=roe, roce=roce, div=div, rg=rev_g, pg=profit_g,
    )


def add_momentum(db, symbol, **cols):
    names = ", ".join(cols)
    values = ", ".join(f":{c}" for c in cols)
    db.execute(f"INSERT INTO terminal_screener_metrics (run_date, symbol, asof{', ' + names if cols else ''}) VALUES (:d, :s, :d{', ' + values if cols else ''})",
               d=date(2026, 10, 16), s=symbol, **cols)


@pytest.fixture
def market(db):
    add_symbol(db, "NSE:HDFCBANK-EQ", BANK, "HDFC Bank")
    add_symbol(db, "NSE:ICICIBANK-EQ", BANK, "ICICI Bank")
    add_symbol(db, "NSE:SBIN-EQ", PSU, "State Bank of India")
    add_symbol(db, "NSE:SUNPHARMA-EQ", PHARMA, "Sun Pharma")
    add_symbol(db, "NSE:ODDBALL-EQ", "Some Unmapped Industry", "Oddball")
    add_symbol(db, "NSE:NOSECTOR-EQ", "", "No sector")
    add_symbol(db, "NSE:NIFTYBANK-INDEX", BANK, "An index, not a share")
    add_ratios(db, "NSE:HDFCBANK-EQ", cap=1200000, pe=18.0, pb=2.8, roe=16.0, roce=7.0, div=1.1, rev_g=12.0, profit_g=15.0)
    add_ratios(db, "NSE:ICICIBANK-EQ", cap=900000, pe=17.0, pb=3.1, roe=17.5, roce=7.5, div=0.8, rev_g=14.0, profit_g=9.0)
    add_ratios(db, "NSE:SBIN-EQ", cap=700000, pe=9.0, pb=1.5, roe=18.0, roce=6.5, div=1.9, rev_g=8.0, profit_g=-4.0)
    add_ratios(db, "NSE:SUNPHARMA-EQ", cap=400000, pe=34.0, roe=15.0)
    add_momentum(db, "NSE:HDFCBANK-EQ", change_pct=0.5, ret_20d=3.0, ret_60d=6.0, ret_120d=10.0, pct_from_52w_high=-4.0, rsi_14=58.0)
    add_momentum(db, "NSE:ICICIBANK-EQ", change_pct=-0.2, ret_20d=5.0, ret_60d=4.0, ret_120d=12.0, pct_from_52w_high=-1.0, rsi_14=64.0)
    add_momentum(db, "NSE:SBIN-EQ", change_pct=1.1, ret_20d=-2.0, ret_60d=8.0, ret_120d=15.0, pct_from_52w_high=-9.0, rsi_14=47.0)


def row(result, symbol):
    return next(r for r in result["rows"] if r["symbol"] == symbol)


class TestTheMap:
    def test_granular_sectors_roll_up_into_parent_groups(self):
        assert get_parent_sector(BANK) == get_parent_sector(PSU) == "Banking & Finance"
        assert get_parent_sector(PHARMA) == "Pharma & Healthcare"

    def test_anything_unmapped_is_others(self):
        assert get_parent_sector("Some Unmapped Industry") == "Others"

    def test_every_mapped_group_is_one_the_ui_orders(self):
        from trading_service.terminal.sector_groups import PARENT_SECTORS
        assert set(SECTOR_GROUP_MAP.values()) <= set(PARENT_SECTORS)


class TestGroups:
    def test_lists_groups_with_the_sectors_inside_them(self, db, market):
        groups = {g["name"]: g for g in sectors.list_groups(db)}
        assert groups["Banking & Finance"]["count"] == 3
        assert groups["Banking & Finance"]["sectors"] == [{"name": BANK, "count": 2}, {"name": PSU, "count": 1}]   # biggest sector first
        assert groups["Pharma & Healthcare"]["count"] == 1

    def test_unmapped_industries_get_their_own_others_group_after_the_known_ones(self, db, market):
        names = [g["name"] for g in sectors.list_groups(db)]
        assert names[-1] == "Others"
        assert names.index("Banking & Finance") < names.index("Pharma & Healthcare")

    def test_symbols_without_a_sector_and_non_shares_are_left_out(self, db, market):
        assert sum(g["count"] for g in sectors.list_groups(db)) == 5          # 7 rows - no sector - the index

    def test_no_symbols_table_no_groups(self, db):
        db.execute("DROP TABLE symbols")
        assert sectors.list_groups(db) == []


class TestCompare:
    def test_members_are_listed_biggest_first_with_their_numbers(self, db, market):
        result = sectors.compare(db, group="Banking & Finance")
        assert [r["symbol"] for r in result["rows"]] == ["NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ", "NSE:SBIN-EQ"]
        assert row(result, "NSE:ICICIBANK-EQ")["metrics"] == {
            "market_cap_cr": 900000, "pe_ratio": 17.0, "pb_ratio": 3.1, "roe_pct": 17.5, "roce_pct": 7.5, "dividend_yield": 0.8,
            "revenue_growth_pct": 14.0, "profit_growth_pct": 9.0, "change_pct": -0.2, "ret_20d": 5.0, "ret_60d": 4.0, "ret_120d": 12.0,
            "pct_from_52w_high": -1.0, "rsi_14": 64.0,
        }
        assert result["count"] == 3 and result["group"] == "Banking & Finance" and result["sector"] is None

    def test_cheaper_is_better_for_valuation_and_higher_is_better_for_the_rest(self, db, market):
        result = sectors.compare(db, group="Banking & Finance")
        pe = {r["symbol"]: r["ranks"]["pe_ratio"] for r in result["rows"]}
        assert pe == {"NSE:SBIN-EQ": 1, "NSE:ICICIBANK-EQ": 2, "NSE:HDFCBANK-EQ": 3}
        roe = {r["symbol"]: r["ranks"]["roe_pct"] for r in result["rows"]}
        assert roe == {"NSE:SBIN-EQ": 1, "NSE:ICICIBANK-EQ": 2, "NSE:HDFCBANK-EQ": 3}
        ret = {r["symbol"]: r["ranks"]["ret_20d"] for r in result["rows"]}
        assert ret == {"NSE:ICICIBANK-EQ": 1, "NSE:HDFCBANK-EQ": 2, "NSE:SBIN-EQ": 3}
        near_high = {r["symbol"]: r["ranks"]["pct_from_52w_high"] for r in result["rows"]}
        assert near_high["NSE:ICICIBANK-EQ"] == 1 and near_high["NSE:SBIN-EQ"] == 3          # -1% below the high beats -9%

    def test_the_group_median_is_there_to_compare_against(self, db, market):
        medians = sectors.compare(db, group="Banking & Finance")["medians"]
        assert medians["pe_ratio"] == 17.0 and medians["roe_pct"] == 17.5 and medians["market_cap_cr"] == 900000

    def test_indicators_like_rsi_are_shown_but_not_ranked(self, db, market):
        r = row(sectors.compare(db, group="Banking & Finance"), "NSE:HDFCBANK-EQ")
        assert r["metrics"]["rsi_14"] == 58.0 and "rsi_14" not in r["ranks"]

    def test_a_loss_makers_negative_pe_is_not_ranked_as_cheap(self, db):
        for sym, pe in [("NSE:A-EQ", -50.0), ("NSE:B-EQ", 12.0), ("NSE:C-EQ", 20.0)]:
            add_symbol(db, sym, BANK)
            add_ratios(db, sym, cap=100, pe=pe)
        result = sectors.compare(db, group="Banking & Finance")
        ranks = {r["symbol"]: r["ranks"].get("pe_ratio") for r in result["rows"]}
        assert ranks == {"NSE:A-EQ": None, "NSE:B-EQ": 1, "NSE:C-EQ": 2}
        assert result["medians"]["pe_ratio"] == 16.0                                   # of the two usable values

    def test_ties_share_a_rank(self, db):
        for sym in ["NSE:A-EQ", "NSE:B-EQ", "NSE:C-EQ"]:
            add_symbol(db, sym, BANK)
        add_ratios(db, "NSE:A-EQ", roe=15.0)
        add_ratios(db, "NSE:B-EQ", roe=15.0)
        add_ratios(db, "NSE:C-EQ", roe=10.0)
        ranks = {r["symbol"]: r["ranks"]["roe_pct"] for r in sectors.compare(db, group="Banking & Finance")["rows"]}
        assert ranks == {"NSE:A-EQ": 1, "NSE:B-EQ": 1, "NSE:C-EQ": 3}

    def test_missing_data_stays_blank_rather_than_zero(self, db, market):
        r = row(sectors.compare(db, group="Pharma & Healthcare"), "NSE:SUNPHARMA-EQ")
        assert r["metrics"]["pe_ratio"] == 34.0 and r["metrics"]["pb_ratio"] is None and r["metrics"]["ret_20d"] is None
        assert "pb_ratio" not in r["ranks"]

    def test_a_ratio_comes_from_the_newest_row_that_has_it(self, db, market):
        add_ratios(db, "NSE:SBIN-EQ", pe=None, roe=19.5, kind="annual", period="Mar 2026", end=date(2026, 3, 31))   # newer, but no P/E
        r = row(sectors.compare(db, group="Banking & Finance"), "NSE:SBIN-EQ")
        assert r["metrics"]["pe_ratio"] == 9.0 and r["metrics"]["roe_pct"] == 19.5

    def test_growth_is_the_latest_quarter_not_a_year(self, db, market):
        add_ratios(db, "NSE:SBIN-EQ", rev_g=99.0, profit_g=99.0, kind="annual", period="Mar 2026", end=date(2026, 3, 31))
        add_ratios(db, "NSE:SBIN-EQ", rev_g=3.0, profit_g=2.0, kind="quarterly", period="Sep 2026", end=date(2026, 9, 30))
        m = row(sectors.compare(db, group="Banking & Finance"), "NSE:SBIN-EQ")["metrics"]
        assert (m["revenue_growth_pct"], m["profit_growth_pct"]) == (3.0, 2.0)

    def test_momentum_comes_from_the_newest_screener_run_only(self, db, market):
        db.execute("INSERT INTO terminal_screener_metrics (run_date, symbol, asof, ret_20d) VALUES (:d, 'NSE:SBIN-EQ', :d, 77)", d=date(2026, 9, 1))
        assert row(sectors.compare(db, group="Banking & Finance"), "NSE:SBIN-EQ")["metrics"]["ret_20d"] == -2.0

    def test_works_before_any_scan_or_results_have_loaded(self, db, market):
        db.execute("DROP TABLE terminal_screener_metrics")
        db.execute("DROP TABLE stock_financials")
        result = sectors.compare(db, group="Banking & Finance")
        assert result["count"] == 3 and all(r["metrics"]["pe_ratio"] is None for r in result["rows"]) and result["medians"] == {}

    def test_one_granular_sector_can_be_compared_on_its_own(self, db, market):
        result = sectors.compare(db, sector=BANK)
        assert [r["symbol"] for r in result["rows"]] == ["NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ"]
        assert result["sector"] == BANK and result["group"] == "Banking & Finance"
        assert result["rows"][1]["ranks"]["pe_ratio"] == 1                          # ranked among these two, not the whole group

    def test_the_row_limit_trims_the_table_but_not_the_ranks_or_the_member_list(self, db, market):
        result = sectors.compare(db, group="Banking & Finance", limit=2)
        assert [r["symbol"] for r in result["rows"]] == ["NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ"]
        assert result["symbols"] == ["NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ", "NSE:SBIN-EQ"]      # all members, biggest first
        assert result["ranked"]["pe_ratio"] == 3 and result["count"] == 3
        assert result["rows"][1]["ranks"]["pe_ratio"] == 2

    def test_unknown_groups_are_an_error(self, db, market):
        with pytest.raises(sectors.UnknownSector):
            sectors.compare(db, group="Basket Weaving")

    def test_columns_describe_themselves_for_the_ui(self, db, market):
        cols = {c["key"]: c for c in sectors.compare(db, group="Banking & Finance")["columns"]}
        assert cols["pe_ratio"]["higher_is_better"] is False and cols["roe_pct"]["higher_is_better"] is True and cols["rsi_14"]["higher_is_better"] is None


class TestEndpoints:
    @pytest.fixture
    def client(self, db, market):
        from tests.terminal.test_api import SimClock, MockFeed, MockHistory, Terminal, ist
        clock = SimClock(ist(2026, 10, 19, 10, 0, 30), speed=0)
        app = FastAPI()
        api.mount(app)
        app.state.terminal = Terminal(db, MockFeed(clock), MockHistory(), clock.now, mode="test", hub_interval=0)
        with TestClient(app) as c:
            yield c

    def test_lists_the_groups(self, client):
        names = [g["name"] for g in client.get("/terminal/sectors").json()["groups"]]
        assert names[:2] == ["Banking & Finance", "Pharma & Healthcare"]

    def test_compares_a_group_or_a_sector(self, client):
        assert client.get("/terminal/sectors/compare", params={"group": "Banking & Finance"}).json()["count"] == 3
        assert client.get("/terminal/sectors/compare", params={"sector": BANK}).json()["count"] == 2

    def test_needs_a_group_or_a_sector(self, client):
        assert client.get("/terminal/sectors/compare").status_code == 400

    def test_unknown_names_are_a_404(self, client):
        assert client.get("/terminal/sectors/compare", params={"group": "Basket Weaving"}).status_code == 404

    def test_limit_is_bounded(self, client):
        assert client.get("/terminal/sectors/compare", params={"group": "Banking & Finance", "limit": 0}).status_code == 422
        assert len(client.get("/terminal/sectors/compare", params={"group": "Banking & Finance", "limit": 1}).json()["rows"]) == 1
