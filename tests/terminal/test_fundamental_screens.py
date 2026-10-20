"""Screens on financial data (growth, ROCE, magic formula...) and the extra price metrics (golden cross).

Fundamentals are worked out from `stock_financials`; these tests build tiny companies by hand so every number can be checked.
"""
from datetime import date, datetime, timedelta

import pytest

from tests.terminal.test_screener import D0, daily
from trading_service.terminal import fundamentals as fd, screener
from trading_service.terminal.candles import Candle


def q(i, revenue, profit, opm=20.0):
    """The i-th quarter counted from Mar 2025 (quarters end 90 days apart, close enough for the year-ago lookup)."""
    return {"period_end_date": date(2025, 3, 31) + timedelta(days=91 * i), "revenue": revenue, "net_profit": profit, "opm_pct": opm}


def y(year, revenue, profit=None, eps=None, interest=None, op=None):
    return {"period_end_date": date(year, 3, 31), "revenue": revenue, "net_profit": profit, "eps": eps, "interest": interest, "operating_profit": op}


class TestQuarterly:
    def test_growth_against_a_year_ago_and_the_quarter_before(self):
        rows = [q(0, 100, 10), q(1, 110, 12), q(2, 120, 14), q(3, 130, 16), q(4, 150, 20, opm=25.0)]
        f = fd.compute_fundamentals(rows, [], {})
        assert f["q_revenue_yoy_pct"] == pytest.approx(50.0) and f["q_profit_yoy_pct"] == pytest.approx(100.0)
        assert f["q_revenue_qoq_pct"] == pytest.approx(150 / 130 * 100 - 100) and f["q_profit_qoq_pct"] == pytest.approx(25.0)
        assert f["q_opm_change"] == pytest.approx(5.0) and f["q_net_profit"] == 20

    def test_without_a_year_of_history_year_on_year_is_unknown_not_zero(self):
        f = fd.compute_fundamentals([q(0, 100, 10), q(1, 110, 12)], [], {})
        assert f["q_revenue_yoy_pct"] is None and f["q_revenue_qoq_pct"] == pytest.approx(10.0)

    def test_growth_off_a_loss_is_not_a_percentage(self):
        f = fd.compute_fundamentals([q(0, 100, -5), q(1, 100, -2), q(2, 100, 1), q(3, 100, 2), q(4, 100, 4)], [], {})
        assert f["q_profit_yoy_pct"] is None

    def test_streak_counts_quarters_of_growth_in_both_revenue_and_profit(self):
        rows = [q(0, 100, 10), q(1, 100, 10), q(2, 100, 10), q(3, 100, 10), q(4, 110, 11), q(5, 120, 12), q(6, 90, 13), q(7, 130, 14), q(8, 140, 15)]
        # latest two quarters beat their year-ago quarters; q(6) had lower revenue than q(2), which ends the streak there
        assert fd.compute_fundamentals(rows, [], {})["q_growth_streak"] == 2

    def test_a_quarter_that_turned_to_profit_is_flagged(self):
        assert fd.compute_fundamentals([q(0, 100, -5), q(1, 100, 3)], [], {})["q_turnaround"] == 1
        assert fd.compute_fundamentals([q(0, 100, 5), q(1, 100, 3)], [], {})["q_turnaround"] == 0

    def test_a_missing_quarter_does_not_pass_for_the_previous_one(self):
        gap = [q(0, 100, -5), q(2, 100, 3)]            # a quarter is missing between them
        assert fd.compute_fundamentals(gap, [], {})["q_turnaround"] is None


class TestAnnual:
    def test_three_year_growth_rates_and_the_weakest_year(self):
        a = [y(2022, 100, 10), y(2023, 120, 12), y(2024, 138, 15), y(2025, 160, 20)]
        f = fd.compute_fundamentals([], a, {})
        assert f["sales_cagr_3y"] == pytest.approx(((160 / 100) ** (1 / 3) - 1) * 100)
        assert f["profit_cagr_3y"] == pytest.approx(((20 / 10) ** (1 / 3) - 1) * 100)
        assert f["rev_growth_min_3y"] == pytest.approx(15.0)       # 120 -> 138 was the slowest year

    def test_the_ttm_column_without_a_date_is_ignored(self):
        a = [y(2022, 100, 10), y(2023, 120, 12), y(2024, 138, 15), y(2025, 160, 20), {"period_end_date": None, "revenue": 999, "net_profit": 99}]
        assert fd.compute_fundamentals([], a, {})["sales_cagr_3y"] == pytest.approx(((160 / 100) ** (1 / 3) - 1) * 100)

    def test_interest_cover_and_its_change(self):
        f = fd.compute_fundamentals([], [y(2024, 100, interest=50, op=100), y(2025, 100, interest=40, op=200)], {})
        assert f["interest_cover"] == pytest.approx(5.0) and f["interest_change_pct"] == pytest.approx(-20.0)

    def test_no_interest_means_no_cover_figure(self):
        assert fd.compute_fundamentals([], [y(2025, 100, interest=0, op=50)], {})["interest_cover"] is None

    def test_price_over_average_earnings_needs_enough_years(self):
        few = [y(2023, 1, eps=10), y(2024, 1, eps=20)]
        many = [y(2021, 1, eps=10), y(2022, 1, eps=20), y(2023, 1, eps=30)]
        assert fd.compute_fundamentals([], few, {}, close=100)["pe_avg_eps"] is None
        f = fd.compute_fundamentals([], many, {}, close=100)
        assert f["pe_avg_eps"] == pytest.approx(5.0) and f["eps_years"] == 3


class TestRatios:
    def test_earnings_yield_and_peg(self):
        a = [y(2022, 100, 10), y(2023, 100, 12), y(2024, 100, 15), y(2025, 100, 20)]
        f = fd.compute_fundamentals([], a, {"pe_ratio": 25.0, "roce_pct": 18.0})
        assert f["earnings_yield_pct"] == pytest.approx(4.0)
        assert f["peg_ratio"] == pytest.approx(25.0 / (((20 / 10) ** (1 / 3) - 1) * 100))

    def test_a_negative_pe_gives_no_earnings_yield_or_peg(self):
        f = fd.compute_fundamentals([], [], {"pe_ratio": -5.0})
        assert f["earnings_yield_pct"] is None and f["peg_ratio"] is None


class TestMagicFormula:
    def test_best_combined_rank_wins_and_banks_and_losers_are_left_out(self):
        rows = {
            "A": {"roce_pct": 40.0, "earnings_yield_pct": 5.0},     # roce 1, yield 3 -> 4
            "B": {"roce_pct": 30.0, "earnings_yield_pct": 12.0},    # roce 2, yield 1 -> 3
            "C": {"roce_pct": 10.0, "earnings_yield_pct": 10.0},    # roce 3, yield 2 -> 5
            "BANK": {"roce_pct": 90.0, "earnings_yield_pct": 90.0},
            "LOSS": {"roce_pct": -5.0, "earnings_yield_pct": 20.0},
        }
        sectors = {"BANK": "Private Sector Bank"}
        assert fd.magic_ranks(rows, sectors) == {"B": 1, "A": 2, "C": 3}


# ----------------------------------------------------------------------------- the scan, end to end
def add_company(db, symbol, *, sector="Software Products", cap=5000.0, quarters=(), years=(), **ratios):
    db.execute("INSERT INTO symbols (tradingsymbol, name, sector, market_cap) VALUES (:s, :s, :c, :m)", s=symbol, c=sector, m=cap * 1e7)  # symbols.market_cap is rupees
    nse = symbol.split(":")[1][:-3]
    cols = dict(pe_ratio=None, pb_ratio=None, roce_pct=None, roe_pct=None, dividend_yield=None)
    cols.update(ratios)
    for kind, rows in (("quarterly", quarters), ("annual", years)):
        for n, r in enumerate(rows):
            db.execute(
                """INSERT INTO stock_financials (tradingsymbol, nse_symbol, result_type, period, period_end_date, revenue, net_profit, opm_pct, eps, interest, operating_profit,
                       pe_ratio, pb_ratio, roce_pct, roe_pct, dividend_yield)
                   VALUES (:s, :n, :k, :p, :e, :rev, :np, :opm, :eps, :int, :op, :pe, :pb, :roce, :roe, :div)""",
                s=symbol, n=nse, k=kind, p=f"{kind[0]}{n}", e=r["period_end_date"], rev=r["revenue"], np=r["net_profit"], opm=r.get("opm_pct"), eps=r.get("eps"),
                int=r.get("interest"), op=r.get("operating_profit"), pe=cols["pe_ratio"], pb=cols["pb_ratio"], roce=cols["roce_pct"], roe=cols["roe_pct"], div=cols["dividend_yield"],
            )


def add_prices(db, symbol, closes):
    for c in daily(closes):
        db.execute("INSERT INTO ohlcv_1d (tradingsymbol, date, open, high, low, close, volume) VALUES (:s, :d, :o, :h, :l, :c, :v)",
                   s=symbol, d=c.time.date(), o=c.open, h=c.high, l=c.low, c=c.close, v=c.volume)


GROWER_Q = [q(0, 100, 10), q(1, 105, 11), q(2, 110, 12), q(3, 115, 13), q(4, 140, 18, opm=26.0), q(5, 150, 20, opm=27.0)]
GROWER_Y = [y(2022, 400, 40, eps=4, interest=10, op=80), y(2023, 450, 50, eps=5, interest=9, op=95), y(2024, 520, 62, eps=6, interest=8, op=110), y(2025, 600, 80, eps=8, interest=6, op=140)]
FLAT_Y = [y(2022, 400, 40, eps=4), y(2023, 400, 40, eps=4), y(2024, 400, 40, eps=4), y(2025, 400, 40, eps=4)]
FLAT_Q = [q(0, 100, 10), q(1, 100, 10), q(2, 100, 10), q(3, 100, 10), q(4, 100, 10), q(5, 100, 10)]


@pytest.fixture
def scanned(db):
    for sym in ("NSE:GROW-EQ", "NSE:FLAT-EQ", "NSE:BANK-EQ", "NSE:NOFIN-EQ"):
        add_prices(db, sym, [100.0 + i * 0.1 for i in range(260)])
    add_company(db, "NSE:GROW-EQ", quarters=GROWER_Q, years=GROWER_Y, pe_ratio=15.0, roce_pct=30.0, pb_ratio=0.9, roe_pct=25.0, dividend_yield=3.5)
    add_company(db, "NSE:FLAT-EQ", quarters=FLAT_Q, years=FLAT_Y,
                pe_ratio=40.0, roce_pct=8.0, pb_ratio=5.0, roe_pct=6.0, dividend_yield=0.2)
    add_company(db, "NSE:BANK-EQ", sector="Private Sector Bank", quarters=FLAT_Q, years=FLAT_Y, pe_ratio=10.0, roce_pct=50.0, pb_ratio=1.5, roe_pct=15.0)
    add_company(db, "NSE:NOFIN-EQ")
    screener.run_scan(db, run_date=date(2026, 2, 15))
    return db


def names(result):
    return [r["symbol"].split(":")[1][:-3] for r in result["rows"]]


class TestScanStoresFundamentals:
    def test_each_stock_carries_its_financial_columns(self, scanned):
        r = {x["symbol"]: x for x in screener.run_screen(scanned, [], sort="symbol", descending=False, limit=10)["rows"]}
        g = r["NSE:GROW-EQ"]
        assert g["q_revenue_yoy_pct"] == pytest.approx(150 / 105 * 100 - 100) and g["roce_pct"] == 30.0 and g["q_growth_streak"] == 2
        assert g["profit_cagr_3y"] == pytest.approx(((80 / 40) ** (1 / 3) - 1) * 100)

    def test_a_stock_without_financials_is_still_screened_on_price_with_blank_financials(self, scanned):
        r = {x["symbol"]: x for x in screener.run_screen(scanned, [], sort="symbol", descending=False)["rows"]}
        assert r["NSE:NOFIN-EQ"]["roce_pct"] is None and r["NSE:NOFIN-EQ"]["close"] > 0

    def test_magic_rank_skips_banks(self, scanned):
        r = {x["symbol"]: x["magic_rank"] for x in screener.run_screen(scanned, [], sort="symbol", descending=False)["rows"]}
        assert r["NSE:GROW-EQ"] == 1 and r["NSE:FLAT-EQ"] == 2 and r["NSE:BANK-EQ"] is None


class TestPresets:
    @pytest.mark.parametrize("preset,expected", [
        ("strong_quarter", ["GROW"]),
        ("quarterly_growers", []),                       # only 2 quarters in a row (needs 4)
        ("magic_formula", ["GROW", "FLAT"]),             # the bank has the best raw numbers but the formula leaves banks out
        ("growth_reasonable", ["GROW"]),
        ("high_dividend", ["GROW"]),
        ("below_book", ["GROW"]),
        ("interest_falling", ["GROW"]),
        ("quality_in_uptrend", ["GROW"]),
        ("coffee_can", ["GROW"]),
    ])
    def test_each_screen_finds_the_right_stock(self, scanned, preset, expected):
        assert names(screener.run_preset(scanned, preset)) == expected

    def test_every_preset_runs_and_every_column_it_uses_exists(self, scanned):
        for name, p in screener.PRESETS.items():
            screener.run_preset(scanned, name)
            assert set(p["show"]) <= set(screener.FILTERABLE), name
            assert p["group"] and p["label"] and p["description"]
            assert all(c in screener.SHORT for c in p["show"]), name

    def test_financial_and_price_filters_mix_in_one_screen(self, scanned):
        r = screener.run_screen(scanned, [{"field": "roce_pct", "op": ">=", "value": 15}, {"field": "pe_ratio", "op": "<", "value": 12}], sort="roce_pct")
        assert names(r) == ["BANK"]

    def test_unknown_financial_field_is_rejected(self, scanned):
        with pytest.raises(screener.ScreenError):
            screener.run_screen(scanned, [{"field": "debt_to_equity", "op": "<", "value": 1}])


class TestGoldenCross:
    def test_a_fresh_golden_cross_and_a_fresh_death_cross(self):
        down_then_up = [100.0 - i * 0.2 for i in range(250)] + [50.0 + i * 3 for i in range(40)]
        m = screener.compute_metrics(daily(down_then_up))
        assert m["golden_cross"] in (0, 1)                # may have crossed earlier than the last 5 bars
        crossing = None
        for n in range(255, 290):
            if screener.compute_metrics(daily(down_then_up[:n]))["golden_cross"] == 1:
                crossing = n
                break
        assert crossing is not None
        up_then_down = [50.0 + i * 0.2 for i in range(250)] + [100.0 - i * 3 for i in range(40)]
        assert any(screener.compute_metrics(daily(up_then_down[:n]))["golden_cross"] == -1 for n in range(255, 290))

    def test_flat_prices_have_no_cross(self):
        assert screener.compute_metrics(daily([100.0] * 260))["golden_cross"] == 0


class TestWeekVolume:
    def test_last_five_days_against_the_twenty_before(self):
        m = screener.compute_metrics(daily([100.0] * 40, [1000] * 35 + [4000] * 5))
        assert m["volume_ratio_5d"] == pytest.approx(4.0)
