"""Symbol search and the financials panel's data, over the data-store's tables."""
from datetime import date, timedelta

import pytest

from trading_service.terminal import financials, symbols


@pytest.fixture
def universe(db):
    for sym, name, sector in [
        ("NSE:SBIN-EQ", "State Bank of India", "Banks"),
        ("NSE:SBILIFE-EQ", "SBI Life Insurance", "Insurance"),
        ("NSE:HDFCBANK-EQ", "HDFC Bank", "Banks"),
        ("NSE:TCS-EQ", "Tata Consultancy Services", "IT"),
        ("NSE:ICICIBANK-EQ", "ICICI Bank", "Banks"),
    ]:
        db.execute("INSERT INTO symbols (tradingsymbol, name, sector) VALUES (:s, :n, :c)", s=sym, n=name, c=sector)


class TestSearch:
    def test_finds_by_trading_symbol_and_company_name(self, db, universe):
        assert [r["symbol"] for r in symbols.search(db, "tcs")] == ["NSE:TCS-EQ"]
        assert [r["symbol"] for r in symbols.search(db, "tata consultancy")] == ["NSE:TCS-EQ"]

    def test_exact_and_prefix_matches_rank_before_substring_matches(self, db, universe):
        found = [r["symbol"] for r in symbols.search(db, "SBI")]
        assert found[:2] == ["NSE:SBIN-EQ", "NSE:SBILIFE-EQ"]

    def test_results_describe_the_instrument(self, db, universe):
        (hit,) = symbols.search(db, "hdfc")
        assert hit == {"symbol": "NSE:HDFCBANK-EQ", "exchange": "NSE", "name": "HDFC Bank", "sector": "Banks"}

    def test_wildcard_characters_in_the_query_are_literal(self, db, universe):
        assert symbols.search(db, "%") == []
        assert symbols.search(db, "_") == []

    def test_blank_query_and_limit(self, db, universe):
        assert symbols.search(db, "  ") == []
        assert len(symbols.search(db, "bank", limit=2)) == 2

    def test_without_the_data_store_table_search_is_empty_not_an_error(self, db):
        db.execute("DROP TABLE symbols")
        assert symbols.search(db, "sbi") == []


def add_result(db, symbol, result_type, period, end, **kw):
    cols = {"revenue": 100.0, "net_profit": 10.0, "opm_pct": 20.0, "eps": 1.5, "pe_ratio": 18.0, "roe_pct": 12.0,
            "revenue_growth_pct": 5.0, "profit_growth_pct": 8.0, "company_name": "State Bank of India", **kw}
    nse = financials.nse_name(symbol)
    db.execute(
        """INSERT INTO stock_financials (tradingsymbol, nse_symbol, company_name, result_type, period, period_end_date,
               revenue, net_profit, opm_pct, eps, pe_ratio, roe_pct, revenue_growth_pct, profit_growth_pct)
           VALUES (:s, :n, :company_name, :t, :p, :e, :revenue, :net_profit, :opm_pct, :eps, :pe_ratio, :roe_pct,
                   :revenue_growth_pct, :profit_growth_pct)""",
        s=symbol, n=nse, t=result_type, p=period, e=end, **cols,
    )


class TestFinancials:
    SYM = "NSE:SBIN-EQ"

    def test_nse_name_matches_how_the_data_store_derives_it(self):
        assert financials.nse_name("NSE:SBIN-EQ") == "SBIN"
        assert financials.nse_name("NSE:M&M-EQ") == "M&M"

    def test_a_symbol_without_any_data_is_reported_unavailable(self, db):
        out = financials.get_financials(db, self.SYM)
        assert out["available"] is False and out["quarterly"] == [] and out["annual"] == []

    def test_missing_tables_degrade_to_unavailable(self, db):
        db.execute("DROP TABLE stock_financials")
        assert financials.get_financials(db, self.SYM)["available"] is False

    def test_quarters_come_oldest_first_limited_to_the_latest_eight(self, db):
        for i in range(10):
            end = date(2024, 3, 31) + timedelta(days=91 * i)
            add_result(db, self.SYM, "quarterly", f"Q{i}", end, revenue=100.0 + i)
        add_result(db, self.SYM, "annual", "FY24", date(2024, 3, 31), revenue=999.0)

        out = financials.get_financials(db, self.SYM)
        assert [q["period"] for q in out["quarterly"]] == [f"Q{i}" for i in range(2, 10)]
        assert out["quarterly"][-1]["revenue"] == 109.0
        assert [a["period"] for a in out["annual"]] == ["FY24"]
        assert out["available"] is True

    def test_ratios_and_company_come_from_the_latest_scrape(self, db):
        add_result(db, self.SYM, "quarterly", "Q1", date(2026, 6, 30))
        out = financials.get_financials(db, self.SYM)
        assert out["company"] == "State Bank of India"
        assert out["ratios"]["pe_ratio"] == 18.0 and out["ratios"]["roe_pct"] == 12.0

    def test_a_newer_row_without_ratios_does_not_hide_the_ratios_of_an_older_one(self, db):
        add_result(db, self.SYM, "quarterly", "Q1", date(2026, 6, 30))                       # has P/E 18, ROE 12
        add_result(db, self.SYM, "annual", "FY26", date(2026, 3, 31), pe_ratio=None, roe_pct=None)  # scraped later, no ratios
        out = financials.get_financials(db, self.SYM)
        assert out["ratios"]["pe_ratio"] == 18.0 and out["ratios"]["roe_pct"] == 12.0
        assert out["ratios"]["pb_ratio"] is None                                               # genuinely unknown stays unknown

    def test_dates_are_json_friendly(self, db):
        add_result(db, self.SYM, "quarterly", "Q1", date(2026, 6, 30))
        assert financials.get_financials(db, self.SYM)["quarterly"][0]["period_end_date"] == "2026-06-30"

    def test_announcements_are_matched_on_the_nse_name(self, db):
        add_result(db, self.SYM, "quarterly", "Q1", date(2026, 6, 30))
        db.execute("INSERT INTO result_announcements (nse_symbol, result_date, quarter) VALUES ('SBIN', :d, 'Q1 FY27')", d=date(2026, 8, 8))
        db.execute("INSERT INTO result_announcements (nse_symbol, result_date, quarter) VALUES ('TCS', :d, 'Q1 FY27')", d=date(2026, 7, 9))
        assert financials.get_financials(db, self.SYM)["announcements"] == [{"result_date": "2026-08-08", "quarter": "Q1 FY27"}]

    def test_recent_results_joins_announcements_with_the_latest_quarter(self, db):
        add_result(db, self.SYM, "quarterly", "Jun 2026", date(2026, 6, 30), net_profit=50.0)
        add_result(db, self.SYM, "quarterly", "Mar 2026", date(2026, 3, 31), net_profit=40.0)
        db.execute("INSERT INTO result_announcements (nse_symbol, company_name, result_date, quarter) VALUES ('SBIN', 'SBI', current_date - 2, 'Q1')")
        db.execute("INSERT INTO result_announcements (nse_symbol, company_name, result_date, quarter) VALUES ('OLD', 'Old Co', current_date - 90, 'Q1')")

        (row,) = financials.recent_results(db, days=7)
        assert row["symbol"] == self.SYM and row["net_profit"] == 50.0 and row["quarter"] == "Q1"

    def test_a_result_with_no_scraped_financials_yet_still_lists(self, db):
        db.execute("INSERT INTO result_announcements (nse_symbol, result_date, quarter) VALUES ('NEWCO', current_date, 'Q2')")
        (row,) = financials.recent_results(db, days=1)
        assert row["symbol"] == "NSE:NEWCO-EQ" and row["net_profit"] is None
