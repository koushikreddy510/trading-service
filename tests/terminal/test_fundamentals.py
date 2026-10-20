"""What the chart overlays get from /financials: result-day events and trailing-twelve-month EPS.

Two ideas to keep in mind while reading:

* a result is "known" to the market from the day AFTER it is announced (results usually land after the
  bell), so TTM EPS - and therefore the chart's P/E line - switches over on the next day, never earlier;
* when no announcement date is on file we assume the LATEST legal day (SEBI: 45 days after a quarter,
  60 after March), so the chart can lag the truth but never see the future.
"""
from datetime import date

import pytest

from tests.terminal.helpers import ist
from tests.terminal.test_symbols_financials import add_result
from trading_service.terminal import financials

SYM = "NSE:SBIN-EQ"

# four consecutive quarters, EPS 1, 2, 3, 4 (TTM 10), then a fifth with EPS 5 (TTM 14)
QUARTERS = [
    ("Sep 2025", date(2025, 9, 30), 1.0),
    ("Dec 2025", date(2025, 12, 31), 2.0),
    ("Mar 2026", date(2026, 3, 31), 3.0),
    ("Jun 2026", date(2026, 6, 30), 4.0),
    ("Sep 2026", date(2026, 9, 30), 5.0),
]


def midnight_ist(y, m, d) -> int:
    return int(ist(y, m, d).timestamp())


def announce(db, day: date, quarter="Q"):
    db.execute("INSERT INTO result_announcements (nse_symbol, result_date, quarter) VALUES ('SBIN', :d, :q)", d=day, q=quarter)


def close(db, day: date, price: float):
    db.execute("INSERT INTO ohlcv_1d (tradingsymbol, date, open, high, low, close, volume) VALUES (:s, :d, :p, :p, :p, :p, 1)", s=SYM, d=day, p=price)


@pytest.fixture
def quarters(db):
    for period, end, eps in QUARTERS:
        add_result(db, SYM, "quarterly", period, end, eps=eps)


class TestEvents:
    def test_each_announcement_becomes_an_event_for_the_quarter_it_reports(self, db, quarters):
        announce(db, date(2026, 8, 7))
        announce(db, date(2026, 11, 6))
        events = financials.fundamentals(db, SYM)["events"]
        assert [(e["date"], e["quarter"]) for e in events] == [("2026-08-07", "Jun 2026"), ("2026-11-06", "Sep 2026")]

    def test_an_event_carries_the_growth_of_its_quarter(self, db):
        add_result(db, SYM, "quarterly", "Jun 2026", date(2026, 6, 30), revenue_growth_pct=12.5, profit_growth_pct=-4.0, net_profit=77.0, eps=2.5)
        announce(db, date(2026, 8, 7))
        (event,) = financials.fundamentals(db, SYM)["events"]
        assert (event["revenue_growth_pct"], event["profit_growth_pct"], event["net_profit"], event["eps"]) == (12.5, -4.0, 77.0, 2.5)

    def test_price_reaction_is_the_next_sessions_close_against_the_close_on_result_day(self, db, quarters):
        announce(db, date(2026, 8, 7))                    # a Friday, announced after the close
        close(db, date(2026, 8, 6), 90.0)
        close(db, date(2026, 8, 7), 100.0)
        close(db, date(2026, 8, 10), 105.0)               # Monday
        (event,) = financials.fundamentals(db, SYM)["events"]
        assert event["reaction_pct"] == 5.0

    def test_reaction_is_unknown_until_the_next_session_has_closed(self, db, quarters):
        announce(db, date(2026, 8, 7))
        close(db, date(2026, 8, 7), 100.0)
        assert financials.fundamentals(db, SYM)["events"][0]["reaction_pct"] is None

    def test_reaction_is_unknown_without_daily_prices(self, db, quarters):
        announce(db, date(2026, 8, 7))
        db.execute("DROP TABLE ohlcv_1d")
        assert financials.fundamentals(db, SYM)["events"][0]["reaction_pct"] is None

    def test_the_earliest_announcement_of_a_quarter_wins(self, db, quarters):
        announce(db, date(2026, 8, 7), "Q1")
        announce(db, date(2026, 8, 20), "Q1 (revised)")
        events = financials.fundamentals(db, SYM)["events"]
        assert [e["date"] for e in events] == ["2026-08-07"]

    def test_an_announcement_with_no_matching_quarter_makes_no_event(self, db, quarters):
        announce(db, date(2026, 1, 15))   # 15 days after the Dec 2025 quarter ended: matches it
        announce(db, date(2024, 2, 1))    # no quarter ended in the 100 days before this one: ignored
        assert [e["date"] for e in financials.fundamentals(db, SYM)["events"]] == ["2026-01-15"]

    def test_quarters_without_an_announcement_get_no_marker_rather_than_a_guessed_date(self, db, quarters):
        assert financials.fundamentals(db, SYM)["events"] == []

    def test_another_companys_announcements_are_ignored(self, db, quarters):
        db.execute("INSERT INTO result_announcements (nse_symbol, result_date, quarter) VALUES ('TCS', :d, 'Q1')", d=date(2026, 8, 7))
        assert financials.fundamentals(db, SYM)["events"] == []


class TestTtmEps:
    def test_ttm_is_the_sum_of_the_last_four_quarters(self, db, quarters):
        points = financials.fundamentals(db, SYM)["eps_ttm"]
        assert [(p["period"], p["value"]) for p in points] == [("Jun 2026", 10.0), ("Sep 2026", 14.0)]
        assert [p["time"] for p in points] == sorted(p["time"] for p in points)

    def test_it_takes_effect_the_day_after_the_announcement(self, db, quarters):
        announce(db, date(2026, 8, 7))
        first = financials.fundamentals(db, SYM)["eps_ttm"][0]
        assert first["time"] == midnight_ist(2026, 8, 8)
        assert first["estimated"] is False

    def test_without_an_announcement_it_waits_for_the_legal_deadline(self, db, quarters):
        first, second = financials.fundamentals(db, SYM)["eps_ttm"]
        assert (first["time"], first["estimated"]) == (midnight_ist(2026, 8, 15), True)    # Jun 30 + 45 days = Aug 14, effective Aug 15
        assert second["time"] == midnight_ist(2026, 11, 15)                                 # Sep 30 + 45 = Nov 14

    def test_the_march_quarter_gets_sixty_days(self, db):
        for period, end, eps in [("Jun 2025", date(2025, 6, 30), 1.0), ("Sep 2025", date(2025, 9, 30), 1.0),
                                 ("Dec 2025", date(2025, 12, 31), 1.0), ("Mar 2026", date(2026, 3, 31), 1.0)]:
            add_result(db, SYM, "quarterly", period, end, eps=eps)
        (point,) = financials.fundamentals(db, SYM)["eps_ttm"]
        assert point["time"] == midnight_ist(2026, 5, 31)                                   # Mar 31 + 60 = May 30, effective May 31

    def test_a_missing_quarter_breaks_the_run(self, db):
        add_result(db, SYM, "quarterly", "Sep 2025", date(2025, 9, 30), eps=1.0)
        add_result(db, SYM, "quarterly", "Dec 2025", date(2025, 12, 31), eps=1.0)
        add_result(db, SYM, "quarterly", "Jun 2026", date(2026, 6, 30), eps=1.0)       # Mar 2026 is missing
        add_result(db, SYM, "quarterly", "Sep 2026", date(2026, 9, 30), eps=1.0)
        assert financials.fundamentals(db, SYM)["eps_ttm"] == []

    def test_a_quarter_without_eps_gives_no_ttm_for_the_windows_it_touches(self, db):
        for i, (period, end, _) in enumerate(QUARTERS):
            add_result(db, SYM, "quarterly", period, end, eps=None if i == 1 else 1.0)
        assert financials.fundamentals(db, SYM)["eps_ttm"] == []                           # windows 0-3 and 1-4 both include index 1

    def test_fewer_than_four_quarters_gives_nothing(self, db):
        add_result(db, SYM, "quarterly", "Sep 2025", date(2025, 9, 30), eps=1.0)
        assert financials.fundamentals(db, SYM)["eps_ttm"] == []


class TestDegrading:
    def test_no_tables_no_overlays(self, db):
        db.execute("DROP TABLE stock_financials")
        assert financials.fundamentals(db, SYM) == {"events": [], "eps_ttm": []}

    def test_no_announcements_table_still_gives_estimated_eps(self, db, quarters):
        db.execute("DROP TABLE result_announcements")
        out = financials.fundamentals(db, SYM)
        assert out["events"] == [] and all(p["estimated"] for p in out["eps_ttm"]) and len(out["eps_ttm"]) == 2

    def test_get_financials_includes_the_overlays(self, db, quarters):
        announce(db, date(2026, 8, 7))
        out = financials.get_financials(db, SYM)
        assert len(out["events"]) == 1 and len(out["eps_ttm"]) == 2

    def test_an_unavailable_symbol_still_has_the_keys(self, db):
        out = financials.get_financials(db, SYM)
        assert out["events"] == [] and out["eps_ttm"] == []
