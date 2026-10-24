"""Strategy-screener -> watchlist: "everything that qualifies, plus what is nearly there".

The strategy-screener is a separate service. These tests replace it with `FakeScreener`, which serves the same
three endpoints the sync uses (`/strategies/`, `/strategies/{id}`, `/run/{id}`) with the same paging rules, so
every rule below can be read as an example of the real data flowing through.

Reading order: how a single condition is measured -> how a stock is classified -> how a plan is built ->
how the managed watchlist is written -> the HTTP API and the command line.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.terminal.helpers import ist
from trading_service.terminal import __main__ as cli
from trading_service.terminal import api
from trading_service.terminal import screener_sync as sync
from trading_service.terminal.feed.mock import MockFeed, MockHistory, SimClock
from trading_service.terminal.runtime import Terminal
from trading_service.terminal.watchlists import WatchlistRepo

# "RSI(14) above 60  AND  EMA(20) below the close"  - a tiny version of the screener's trend strategies
RSI_ABOVE_60 = {"indicator_type": "rsi", "params": {"period": 14}, "lookback_days": 1, "operator": ">", "threshold": {"value": 60}}
EMA_BELOW_CLOSE = {"indicator_type": "ema", "params": {"length": 20}, "lookback_days": 1, "operator": "<", "threshold": {"field": "close"}}
STRATEGY = {"id": 7, "name": "Momentum", "description": "", "is_active": True, "market_type": "stocks", "timeframe": "1D",
            "conditions": [RSI_ABOVE_60, EMA_BELOW_CLOSE]}


def stock(symbol, rsi, ema, close=100.0, matched=False, market_cap=1000, **extra):
    """One entry of the screener's /run answer: latest close plus the indicator values it computed."""
    return {"symbol": symbol, "close": close, "change_pct": 0.5, "date": "2026-10-16 00:00:00", "matched": matched,
            "indicators": {"RSI(14)": rsi, "EMA(20)": ema}, "market_cap": market_cap, "company_name": symbol, "sector": "Banks", **extra}


class FakeScreener:
    """Stands in for the strategy-screener HTTP API (same paths, same paging, `page_size` capped at 50)."""

    def __init__(self, entries, strategy=STRATEGY):
        self.entries, self.strategy, self.calls, self.down = entries, strategy, [], False

    def get(self, path, **params):
        self.calls.append((path, params))
        if self.down:
            raise sync.ScreenerUnavailable("strategy-screener is not reachable at http://fake")
        if path == "/strategies/":
            return [self.strategy]
        if path == f"/strategies/{self.strategy['id']}":
            return self.strategy
        if path == f"/run/{self.strategy['id']}":
            size = min(int(params.get("page_size", 15)), 50)
            page = int(params.get("page", 1))
            rows = self.entries if params.get("matched_only") == "false" else [e for e in self.entries if e["matched"]]
            return {"matches": rows[(page - 1) * size: page * size], "total": len(rows), "page": page, "page_size": size,
                    "total_scanned": len(self.entries), "total_matched": sum(1 for e in self.entries if e["matched"])}
        raise sync.SyncError(f"strategy-screener has no {path} (404) - is the strategy id right?")


# ------------------------------------------------------------------ measuring one condition
class TestMeasure:
    def test_a_condition_that_holds_has_no_gap(self):
        g = sync.measure(RSI_ABOVE_60, stock("NSE:A-EQ", rsi=65, ema=90))
        assert g.passed and g.gap_pct == 0

    def test_the_gap_is_the_distance_to_the_threshold_in_percent_of_the_threshold(self):
        g = sync.measure(RSI_ABOVE_60, stock("NSE:A-EQ", rsi=58.8, ema=90))   # needs >60, is 1.2 short = 2% of 60
        assert not g.passed and g.gap_pct == 2.0
        assert "2.0% below 60" in g.note

    def test_below_conditions_measure_in_the_other_direction(self):
        rsi_below_30 = {**RSI_ABOVE_60, "operator": "<", "threshold": {"value": 30}}
        g = sync.measure(rsi_below_30, stock("NSE:A-EQ", rsi=30.9, ema=90))   # needs <30, is 0.9 over = 3% of 30
        assert not g.passed and g.gap_pct == 3.0 and "above" in g.note

    def test_a_threshold_can_be_another_field_of_the_same_stock(self):
        close_above_ema = sync.measure(EMA_BELOW_CLOSE, stock("NSE:A-EQ", rsi=70, ema=99, close=100))
        assert close_above_ema.passed
        too_high = sync.measure(EMA_BELOW_CLOSE, stock("NSE:A-EQ", rsi=70, ema=101, close=100))   # EMA must be < close(100)
        assert not too_high.passed and too_high.gap_pct == 1.0

    def test_a_zero_threshold_is_measured_against_the_price_so_macd_above_zero_still_has_a_scale(self):
        macd_above_zero = {"indicator_type": "macd", "params": {"output": "macd_line"}, "lookback_days": 1, "operator": ">", "threshold": {"value": 0}}
        entry = {"symbol": "NSE:A-EQ", "close": 200.0, "matched": False, "indicators": {"MACD(macd_line)": -1.0}}
        g = sync.measure(macd_above_zero, entry)
        assert g.gap_pct == 0.5      # 1 rupee on a 200 rupee stock

    def test_a_zero_threshold_on_a_bounded_indicator_uses_that_indicators_own_scale_not_the_price(self):
        """Bollinger %B < 0 ('below the lower band'): %B is a fraction of the band, so 0.03 over is 3 - not 0.003% of a 100 rupee price,
        which would put nearly every stock in the list."""
        below_band = {"indicator_type": "bollinger", "params": {"length": 20, "std": 2.0}, "lookback_days": 1, "operator": "<", "threshold": {"value": 0}}
        entry = {"symbol": "NSE:A-EQ", "close": 100.0, "matched": False, "indicators": {"BOLLINGER(20, 2.0)": 0.03}}
        assert sync.measure(below_band, entry).gap_pct == 3.0
        assert sync.classify(entry, [below_band], tolerance_pct=2, max_failing=1).status == "out"
        rsi_below_zero = {**RSI_ABOVE_60, "operator": "<", "threshold": {"value": 0}}
        assert sync.measure(rsi_below_zero, stock("NSE:A-EQ", rsi=2.5, ema=90)).gap_pct == 2.5      # 2.5 RSI points

    def test_the_indicator_is_found_even_when_the_parameters_are_listed_in_another_order(self):
        """The database may hand the parameters back in a different order than the screener used to name the value."""
        st = {"indicator_type": "supertrend", "params": {"multiplier": 3.0, "period": 10}, "lookback_days": 1, "operator": ">", "threshold": {"value": 100}}
        entry = {"symbol": "NSE:A-EQ", "close": 100.0, "indicators": {"SUPERTREND(10, 3.0)": 98.0, "SUPERTREND(10, 3.0)_dir": "bullish"}}
        assert sync.measure(st, entry).gap_pct == 2.0
        assert sync.measure({**st, "operator": "==", "threshold": {"value": "bullish"}}, entry).passed

    def test_the_label_is_built_the_way_the_screener_builds_it(self):
        assert sync.indicator_label(RSI_ABOVE_60) == "RSI(14)"
        assert sync.indicator_label({"indicator_type": "vwap", "params": {}}) == "VWAP"
        assert sync.indicator_label({"indicator_type": "supertrend", "params": {"period": 10, "multiplier": 3}}) == "SUPERTREND(10, 3)"

    def test_a_cross_below_the_line_counts_as_about_to_cross_but_already_past_it_does_not(self):
        cross_up = {**RSI_ABOVE_60, "operator": "cross_above"}
        assert sync.measure(cross_up, stock("NSE:A-EQ", rsi=59, ema=90)).gap_pct == pytest.approx(1.67, abs=0.01)   # about to cross
        past = sync.measure(cross_up, stock("NSE:A-EQ", rsi=70, ema=90))     # crossed earlier: one snapshot cannot tell it is fresh
        assert not past.passed and past.gap_pct is None

    def test_supertrend_direction_has_no_distance_it_either_holds_or_it_does_not(self):
        trend_up = {"indicator_type": "supertrend", "params": {"period": 10, "multiplier": 3}, "lookback_days": 1, "operator": "==", "threshold": {"value": "up"}}
        up = {"symbol": "NSE:A-EQ", "indicators": {"SUPERTREND(10, 3)": 95.0, "SUPERTREND(10, 3)_dir": "up"}}
        down = {"symbol": "NSE:A-EQ", "indicators": {"SUPERTREND(10, 3)": 105.0, "SUPERTREND(10, 3)_dir": "down"}}
        assert sync.measure(trend_up, up).passed
        g = sync.measure(trend_up, down)
        assert not g.passed and g.gap_pct is None

    def test_a_missing_indicator_value_cannot_be_near(self):
        g = sync.measure(RSI_ABOVE_60, {"symbol": "NSE:A-EQ", "close": 100, "indicators": {"RSI(14)": None}})
        assert not g.passed and g.gap_pct is None


# ------------------------------------------------------------------- classifying one stock
class TestClassify:
    conds = STRATEGY["conditions"]

    def test_what_the_screener_matched_is_matched_whatever_the_numbers_say(self):
        fit = sync.classify(stock("NSE:A-EQ", rsi=70, ema=90, matched=True), self.conds, tolerance_pct=0, max_failing=0)
        assert fit.status == "matched"

    def test_one_condition_missed_by_less_than_the_tolerance_is_a_near_miss(self):
        fit = sync.classify(stock("NSE:A-EQ", rsi=59, ema=90), self.conds, tolerance_pct=3, max_failing=1)
        assert fit.status == "near" and fit.gap_pct == pytest.approx(1.67, abs=0.01)
        assert "RSI(14)" in fit.reason

    def test_missing_by_more_than_the_tolerance_is_out(self):
        assert sync.classify(stock("NSE:A-EQ", rsi=50, ema=90), self.conds, tolerance_pct=3, max_failing=1).status == "out"

    def test_the_tolerance_is_inclusive(self):
        assert sync.classify(stock("NSE:A-EQ", rsi=58.2, ema=90), self.conds, tolerance_pct=3, max_failing=1).status == "near"   # exactly 3%

    def test_missing_two_conditions_is_out_unless_you_allow_two(self):
        both = stock("NSE:A-EQ", rsi=59, ema=101)     # RSI 1.7% short AND EMA 1% too high
        assert sync.classify(both, self.conds, tolerance_pct=3, max_failing=1).status == "out"
        assert sync.classify(both, self.conds, tolerance_pct=3, max_failing=2).status == "near"

    def test_the_gap_reported_is_the_worst_failing_condition(self):
        both = stock("NSE:A-EQ", rsi=59, ema=101)
        assert sync.classify(both, self.conds, tolerance_pct=3, max_failing=2).gap_pct == pytest.approx(1.67, abs=0.01)

    def test_zero_tolerance_means_matched_stocks_only(self):
        assert sync.classify(stock("NSE:A-EQ", rsi=59.9, ema=90), self.conds, tolerance_pct=0, max_failing=1).status == "out"

    def test_a_failing_condition_that_cannot_be_measured_is_never_near(self):
        unmeasurable = stock("NSE:A-EQ", rsi=None, ema=90)
        assert sync.classify(unmeasurable, self.conds, tolerance_pct=50, max_failing=5).status == "out"


# ----------------------------------------------------------------------- reading the screener
class TestFetch:
    def test_every_page_of_the_run_is_read_and_unmatched_stocks_are_requested(self):
        entries = [stock(f"NSE:S{i:03d}-EQ", rsi=50, ema=90) for i in range(120)]
        client = FakeScreener(entries)
        got, meta = sync.fetch_entries(client, 7)
        assert len(got) == 120 and meta["total_scanned"] == 120
        runs = [p for path, p in client.calls if path == "/run/7"]
        assert [r["page"] for r in runs] == [1, 2, 3]                 # 50 + 50 + 20
        assert all(r["matched_only"] == "false" and r["page_size"] == 50 for r in runs)

    def test_an_answer_without_matches_is_reported_not_swallowed(self):
        class Odd:
            def get(self, path, **p):
                return {"oops": 1}
        with pytest.raises(sync.SyncError, match="unexpected shape"):
            sync.fetch_entries(Odd(), 7)

    def test_strategies_are_listed_with_their_conditions_in_words(self):
        (s,) = sync.list_strategies(FakeScreener([]))
        assert s["name"] == "Momentum"
        assert s["conditions"] == ["RSI(14) > 60", "EMA(20) < close"]


# ----------------------------------------------------------------------------- the plan
def universe():
    return [
        stock("NSE:BIGMATCH-EQ", rsi=70, ema=90, matched=True, market_cap=9000),
        stock("NSE:SMALLMATCH-EQ", rsi=65, ema=95, matched=True, market_cap=100),
        stock("NSE:CLOSE-EQ", rsi=59.4, ema=90),            # 1% short
        stock("NSE:CLOSER-EQ", rsi=59.9, ema=90),           # 0.17% short
        stock("NSE:EDGE-EQ", rsi=58.2, ema=90),             # exactly 3% short
        stock("NSE:FAR-EQ", rsi=45, ema=90),                # 25% short
        stock("NSE:TWO-EQ", rsi=59.5, ema=101),             # misses both
    ]


class TestPlan:
    def test_matches_come_first_biggest_company_first_then_near_misses_closest_first(self):
        plan = sync.build_plan(FakeScreener(universe()), 7, tolerance_pct=3)
        assert plan.symbols == ["NSE:BIGMATCH-EQ", "NSE:SMALLMATCH-EQ", "NSE:CLOSER-EQ", "NSE:CLOSE-EQ", "NSE:EDGE-EQ"]
        assert (plan.matched_total, plan.near_total, plan.scanned) == (2, 3, 7)
        assert [c.status for c in plan.candidates] == ["matched"] * 2 + ["near"] * 3

    def test_a_wider_tolerance_lets_more_stocks_in(self):
        assert "NSE:FAR-EQ" not in sync.build_plan(FakeScreener(universe()), 7, tolerance_pct=3).symbols
        assert "NSE:FAR-EQ" in sync.build_plan(FakeScreener(universe()), 7, tolerance_pct=30).symbols

    def test_allowing_two_misses_brings_in_the_stock_that_missed_both(self):
        plan = sync.build_plan(FakeScreener(universe()), 7, tolerance_pct=3, max_failing=2)
        assert "NSE:TWO-EQ" in plan.symbols

    def test_the_list_is_capped_and_matched_stocks_win_the_places(self):
        plan = sync.build_plan(FakeScreener(universe()), 7, tolerance_pct=3, max_symbols=3)
        assert plan.symbols == ["NSE:BIGMATCH-EQ", "NSE:SMALLMATCH-EQ", "NSE:CLOSER-EQ"] and plan.truncated
        assert plan.near_total == 3        # the totals still describe everything that qualified

    def test_the_plan_says_which_day_of_data_it_is_based_on(self):
        assert sync.build_plan(FakeScreener(universe()), 7).asof == "2026-10-16"

    def test_a_strategy_with_no_conditions_is_refused_because_it_would_select_everything(self):
        with pytest.raises(sync.SyncError, match="no conditions"):
            sync.build_plan(FakeScreener(universe(), strategy={**STRATEGY, "conditions": []}), 7)

    def test_commodity_strategies_are_refused(self):
        with pytest.raises(sync.SyncError, match="commodit"):
            sync.build_plan(FakeScreener(universe(), strategy={**STRATEGY, "market_type": "commodities"}), 7)

    @pytest.mark.parametrize("settings", [{"tolerance_pct": -1}, {"tolerance_pct": 99}, {"tolerance_pct": "x"}, {"max_failing": -1}, {"max_symbols": 0}, {"max_symbols": 10_000}])
    def test_nonsense_settings_are_rejected_before_the_screener_is_called(self, settings):
        client = FakeScreener(universe())
        with pytest.raises(sync.SyncError):
            sync.build_plan(client, 7, **settings)
        assert client.calls == []

    def test_a_screener_that_is_down_is_reported_as_unavailable(self):
        client = FakeScreener(universe())
        client.down = True
        with pytest.raises(sync.ScreenerUnavailable):
            sync.build_plan(client, 7)


# ------------------------------------------------------------- financial gates (quarterly / yearly)
class TestFinancialFilters:
    """Optional: keep only stocks whose latest results / ratios pass. Financials come from the data-store tables."""

    @staticmethod
    def fin(**kw):
        return {"ratios": kw.get("ratios", {}), "quarterly": kw.get("quarterly", []), "annual": kw.get("annual", [])}

    def test_a_ratio_filter_uses_the_newest_ratio(self):
        flt = sync.validate_filters([{"source": "ratio", "metric": "roce_pct", "op": ">=", "value": 15}])
        assert sync.apply_filters(self.fin(ratios={"roce_pct": 18}), flt)[0] is True
        assert sync.apply_filters(self.fin(ratios={"roce_pct": 12}), flt)[0] is False

    def test_quarter_and_year_filters_look_at_the_latest_row(self):
        flt = sync.validate_filters([
            {"source": "quarter", "metric": "profit_growth_pct", "op": ">", "value": 0},
            {"source": "year", "metric": "revenue_growth_pct", "op": ">=", "value": 10},
        ])
        good = self.fin(quarterly=[{"profit_growth_pct": -5}, {"profit_growth_pct": 8}], annual=[{"revenue_growth_pct": 3}, {"revenue_growth_pct": 12}])
        assert sync.apply_filters(good, flt)[0] is True                       # rows are oldest first: the last one counts
        bad_year = self.fin(quarterly=[{"profit_growth_pct": 8}], annual=[{"revenue_growth_pct": 4}])
        assert sync.apply_filters(bad_year, flt)[0] is False

    def test_no_data_keeps_the_stock_by_default_so_an_unscraped_company_is_not_lost(self):
        flt = sync.validate_filters([{"source": "ratio", "metric": "roce_pct", "op": ">=", "value": 15}])
        keep, notes = sync.apply_filters(self.fin(), flt)
        assert keep and "no data, kept" in notes[0]

    def test_no_data_can_instead_drop_the_stock(self):
        flt = sync.validate_filters([{"source": "ratio", "metric": "roce_pct", "op": ">=", "value": 15, "on_missing": "drop"}])
        assert sync.apply_filters(self.fin(), flt)[0] is False

    @pytest.mark.parametrize("bad", [
        {"source": "nope", "metric": "roce_pct", "op": ">", "value": 1},
        {"source": "ratio", "metric": "revenue", "op": ">", "value": 1},        # revenue is a quarter/year metric, not a ratio
        {"source": "quarter", "metric": "period", "op": ">", "value": 1},       # not a number
        {"source": "ratio", "metric": "roce_pct", "op": "=", "value": 1},
        {"source": "ratio", "metric": "roce_pct", "op": ">", "value": "x"},
        {"source": "ratio", "metric": "roce_pct", "op": ">", "value": 1, "on_missing": "maybe"},
    ])
    def test_bad_filters_are_rejected(self, bad):
        with pytest.raises(sync.SyncError):
            sync.validate_filters([bad])

    def test_the_plan_drops_stocks_that_fail_a_filter_and_counts_them(self):
        books = {"NSE:BIGMATCH-EQ": self.fin(ratios={"roce_pct": 30}), "NSE:SMALLMATCH-EQ": self.fin(ratios={"roce_pct": 5}),
                 "NSE:CLOSE-EQ": self.fin(ratios={"roce_pct": 20})}
        plan = sync.build_plan(
            FakeScreener(universe()), 7, tolerance_pct=1.5,
            filters=[{"source": "ratio", "metric": "roce_pct", "op": ">=", "value": 15}],
            financials=lambda s: books.get(s, self.fin()),
        )
        assert plan.symbols == ["NSE:BIGMATCH-EQ", "NSE:CLOSER-EQ", "NSE:CLOSE-EQ"]   # SMALLMATCH fails; CLOSER has no data -> kept
        assert plan.rejected_by_financials == 1
        assert any("no data" in n for n in plan.candidates[1].notes)

    def test_filters_need_a_financials_source(self):
        with pytest.raises(sync.SyncError, match="financials lookup"):
            sync.build_plan(FakeScreener(universe()), 7, filters=[{"source": "ratio", "metric": "roce_pct", "op": ">", "value": 1}])


# ------------------------------------------------------------------ writing the watchlist
@pytest.fixture
def repo(db):
    return WatchlistRepo(db)


def plan_for(entries, **settings):
    return sync.build_plan(FakeScreener(entries), 7, **settings)


class TestApply:
    def test_the_first_sync_creates_a_named_list_with_matches_then_near_misses(self, db, repo):
        res = sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=3))
        assert res["created"] is True
        assert res["watchlist"]["name"] == "Screen: Momentum · 1D"
        assert res["watchlist"]["symbols"] == ["NSE:BIGMATCH-EQ", "NSE:SMALLMATCH-EQ", "NSE:CLOSER-EQ", "NSE:CLOSE-EQ", "NSE:EDGE-EQ"]
        assert (res["matched"], res["near"]) == (2, 3)

    def test_each_symbol_remembers_why_it_is_there(self, db, repo):
        sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=3))
        (link,) = sync.list_links(db, repo.user)
        by = {m["symbol"]: m for m in link["members"]}
        assert by["NSE:BIGMATCH-EQ"]["status"] == "matched"
        assert by["NSE:CLOSE-EQ"]["status"] == "near" and by["NSE:CLOSE-EQ"]["gap_pct"] == 1.0
        assert "RSI(14)" in by["NSE:CLOSE-EQ"]["reason"]
        assert link["strategy_id"] == 7 and link["matched_count"] == 2 and link["last_asof"] == "2026-10-16"

    def test_syncing_again_with_nothing_new_changes_nothing(self, db, repo):
        sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=3))
        again = sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=3))
        assert again["created"] is False and again["added"] == [] and again["removed"] == []
        assert len(repo.list()) == 1

    def test_a_stock_that_stops_qualifying_is_taken_off_and_a_new_one_is_added(self, db, repo):
        sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=3))
        tomorrow = [e for e in universe() if e["symbol"] != "NSE:EDGE-EQ"] + [stock("NSE:NEW-EQ", rsi=75, ema=90, matched=True, market_cap=50)]
        res = sync.apply_plan(db, repo, plan_for(tomorrow, tolerance_pct=3))
        assert res["removed"] == ["NSE:EDGE-EQ"] and res["added"] == ["NSE:NEW-EQ"]
        assert "NSE:NEW-EQ" in res["watchlist"]["symbols"] and "NSE:EDGE-EQ" not in res["watchlist"]["symbols"]

    def test_a_near_miss_that_turns_into_a_match_is_relabelled_not_duplicated(self, db, repo):
        sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=3))
        upgraded = [stock("NSE:CLOSE-EQ", rsi=64, ema=90, matched=True, market_cap=10) if e["symbol"] == "NSE:CLOSE-EQ" else e for e in universe()]
        res = sync.apply_plan(db, repo, plan_for(upgraded, tolerance_pct=3))
        assert res["watchlist"]["symbols"].count("NSE:CLOSE-EQ") == 1
        (link,) = sync.list_links(db, repo.user)
        assert {m["symbol"]: m["status"] for m in link["members"]}["NSE:CLOSE-EQ"] == "matched"

    def test_symbols_you_added_by_hand_to_the_synced_list_are_never_removed(self, db, repo):
        res = sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=3))
        repo.add(res["watchlist"]["id"], ["NSE:MINE-EQ"])
        sync.apply_plan(db, repo, plan_for([stock("NSE:BIGMATCH-EQ", rsi=70, ema=90, matched=True)], tolerance_pct=3))
        symbols = repo.get(res["watchlist"]["id"])["symbols"]
        assert "NSE:MINE-EQ" in symbols and "NSE:SMALLMATCH-EQ" not in symbols

    def test_a_symbol_you_already_had_is_not_removed_when_it_later_drops_out_of_the_screen(self, db, repo):
        res = sync.apply_plan(db, repo, plan_for([stock("NSE:BIGMATCH-EQ", rsi=70, ema=90, matched=True)]))
        wid = res["watchlist"]["id"]
        repo.add(wid, ["NSE:CLOSE-EQ"])                                       # added by hand first ...
        sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=3))     # ... then the screen also picks it
        sync.apply_plan(db, repo, plan_for([stock("NSE:BIGMATCH-EQ", rsi=70, ema=90, matched=True)]))   # ... then it drops out
        assert "NSE:CLOSE-EQ" in repo.get(wid)["symbols"]

    def test_a_stock_with_an_open_paper_position_stays_even_when_it_stops_qualifying(self, db, repo):
        sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=3))
        db.execute(
            "INSERT INTO terminal_paper_positions (id, symbol, side, quantity, entry_price, status) "
            "VALUES ('p1', 'NSE:EDGE-EQ', 'BUY', 1, 100, 'open')"
        )
        res = sync.apply_plan(db, repo, plan_for([stock("NSE:BIGMATCH-EQ", rsi=70, ema=90, matched=True)], tolerance_pct=3))
        assert res["kept_for_trading"] == ["NSE:EDGE-EQ"] and "NSE:EDGE-EQ" in res["watchlist"]["symbols"]

    def test_a_stock_with_an_enabled_rule_stays_too(self, db, repo):
        sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=3))
        db.execute(
            "INSERT INTO terminal_rules (id, symbol, timeframe, side, condition, stop, quantity) "
            "VALUES ('r1', 'NSE:EDGE-EQ', '1H', 'BUY', CAST('{}' AS JSONB), CAST('{}' AS JSONB), 1)"
        )
        res = sync.apply_plan(db, repo, plan_for([stock("NSE:BIGMATCH-EQ", rsi=70, ema=90, matched=True)], tolerance_pct=3))
        assert "NSE:EDGE-EQ" in res["kept_for_trading"]

    def test_the_list_is_ordered_matches_first_after_every_sync(self, db, repo):
        sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=3))
        res = sync.apply_plan(db, repo, plan_for(universe() + [stock("NSE:LATE-EQ", rsi=80, ema=90, matched=True, market_cap=1)], tolerance_pct=3))
        assert res["watchlist"]["symbols"][:3] == ["NSE:BIGMATCH-EQ", "NSE:SMALLMATCH-EQ", "NSE:LATE-EQ"]

    def test_a_name_already_used_by_another_list_is_refused_with_a_clear_message(self, db, repo):
        repo.create("Screen: Momentum · 1D")
        with pytest.raises(sync.SyncError, match="choose another name"):
            sync.apply_plan(db, repo, plan_for(universe()))

    def test_you_can_choose_the_list_name(self, db, repo):
        res = sync.apply_plan(db, repo, plan_for(universe()), name="Swing candidates")
        assert res["watchlist"]["name"] == "Swing candidates"

    def test_two_strategies_get_two_lists(self, db, repo):
        sync.apply_plan(db, repo, plan_for(universe()))
        other = sync.build_plan(FakeScreener(universe(), strategy={**STRATEGY, "id": 8, "name": "Breakout"}), 8)
        sync.apply_plan(db, repo, other)
        assert {w["name"] for w in repo.list()} == {"Screen: Momentum · 1D", "Screen: Breakout · 1D"}

    def test_unlinking_keeps_the_list_and_stops_syncing_it(self, db, repo):
        res = sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=3))
        wid = res["watchlist"]["id"]
        assert sync.unlink(db, wid, repo.user) is True
        assert sync.list_links(db, repo.user) == []
        assert len(repo.get(wid)["symbols"]) == 5
        assert sync.unlink(db, wid, repo.user) is False

    def test_deleting_the_list_removes_its_link(self, db, repo):
        res = sync.apply_plan(db, repo, plan_for(universe()))
        repo.delete(res["watchlist"]["id"])
        assert sync.list_links(db, repo.user) == []

    def test_refresh_reruns_each_linked_list_with_the_settings_it_was_created_with(self, db, repo):
        sync.apply_plan(db, repo, plan_for(universe(), tolerance_pct=0.5))     # strict: only the 2 matches + CLOSER
        results = sync.re_sync_all(db, repo, FakeScreener(universe()))
        assert results == [{"strategy_id": 7, "ok": True, "added": 0, "removed": 0, "matched": 2, "near": 1}]

    def test_refresh_reports_a_failure_per_list_and_keeps_going(self, db, repo):
        sync.apply_plan(db, repo, plan_for(universe()))
        down = FakeScreener(universe())
        down.down = True
        (r,) = sync.re_sync_all(db, repo, down)
        assert r["ok"] is False and "not reachable" in r["error"]
        assert len(repo.list()[0]["symbols"]) > 0          # the list is untouched

    def test_sync_strategy_builds_the_plan_and_applies_it(self, db, repo):
        res = sync.sync_strategy(db, repo, FakeScreener(universe()), 7, tolerance_pct=3)
        assert res["plan"]["strategy_name"] == "Momentum" and len(res["watchlist"]["symbols"]) == 5

    def test_financial_filters_run_against_the_data_store_tables(self, db, repo):
        db.execute(
            "INSERT INTO stock_financials (tradingsymbol, nse_symbol, result_type, period, period_end_date, roce_pct) "
            "VALUES ('NSE:BIGMATCH-EQ', 'BIGMATCH', 'annual', 'Mar 2026', '2026-03-31', 31), ('NSE:SMALLMATCH-EQ', 'SMALLMATCH', 'annual', 'Mar 2026', '2026-03-31', 4)"
        )
        res = sync.sync_strategy(db, repo, FakeScreener(universe()), 7, tolerance_pct=0,
                                 filters=[{"source": "ratio", "metric": "roce_pct", "op": ">=", "value": 15}])
        assert res["watchlist"]["symbols"] == ["NSE:BIGMATCH-EQ"] and res["plan"]["rejected_by_financials"] == 1


# ---------------------------------------------------------------------------- the HTTP API
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
    app.state.screener_client = FakeScreener(universe())
    with TestClient(app) as c:
        c.screener = app.state.screener_client
        c.terminal = terminal
        yield c


class TestApi:
    def test_the_strategies_picker_lists_screener_strategies(self, client):
        (s,) = client.get("/terminal/screener-sync/strategies").json()["strategies"]
        assert s["id"] == 7 and s["conditions"] == ["RSI(14) > 60", "EMA(20) < close"]

    def test_preview_shows_matches_and_near_misses_without_creating_anything(self, client):
        r = client.post("/terminal/screener-sync/preview", json={"strategy_id": 7, "tolerance_pct": 3})
        assert r.status_code == 200
        plan = r.json()
        assert [c["status"] for c in plan["candidates"]] == ["matched", "matched", "near", "near", "near"]
        assert plan["candidates"][2]["symbol"] == "NSE:CLOSER-EQ" and plan["candidates"][2]["gap_pct"] == pytest.approx(0.17, abs=0.01)
        assert [w["name"] for w in client.get("/terminal/watchlists").json()["watchlists"]] == ["Watchlist"]

    def test_apply_creates_the_watchlist_the_ui_will_show(self, client):
        r = client.post("/terminal/screener-sync/apply", json={"strategy_id": 7, "tolerance_pct": 3})
        assert r.status_code == 200 and r.json()["created"] is True
        lists = {w["name"]: w["symbols"] for w in client.get("/terminal/watchlists").json()["watchlists"]}
        assert lists["Screen: Momentum · 1D"][0] == "NSE:BIGMATCH-EQ" and len(lists["Screen: Momentum · 1D"]) == 5
        (link,) = client.get("/terminal/screener-sync/links").json()["links"]
        assert link["strategy_id"] == 7 and link["near_count"] == 3 and len(link["members"]) == 5

    def test_refresh_updates_linked_lists(self, client):
        client.post("/terminal/screener-sync/apply", json={"strategy_id": 7, "tolerance_pct": 3})
        client.screener.entries = [stock("NSE:ONLY-EQ", rsi=70, ema=90, matched=True)]
        res = client.post("/terminal/screener-sync/refresh").json()["results"]
        assert res[0]["ok"] and res[0]["added"] == 1 and res[0]["removed"] == 5

    def test_unlink_keeps_the_list(self, client):
        wid = client.post("/terminal/screener-sync/apply", json={"strategy_id": 7}).json()["watchlist"]["id"]
        assert client.delete(f"/terminal/screener-sync/links/{wid}").status_code == 204
        assert client.get("/terminal/screener-sync/links").json()["links"] == []
        assert client.delete(f"/terminal/screener-sync/links/{wid}").status_code == 404
        assert wid in {w["id"] for w in client.get("/terminal/watchlists").json()["watchlists"]}

    def test_a_missing_or_bad_request_is_a_400(self, client):
        assert client.post("/terminal/screener-sync/preview", json={}).status_code == 400
        assert client.post("/terminal/screener-sync/preview", json={"strategy_id": 7, "tolerance_pct": 400}).status_code == 400
        assert client.post("/terminal/screener-sync/apply", json={"strategy_id": 7, "filters": [{"source": "x"}]}).status_code == 400

    def test_an_unknown_strategy_is_a_400_with_the_screeners_message(self, client):
        r = client.post("/terminal/screener-sync/preview", json={"strategy_id": 99})
        assert r.status_code == 400 and "404" in r.json()["detail"]

    def test_a_screener_that_is_down_is_a_502_not_a_crash(self, client):
        client.screener.down = True
        assert client.get("/terminal/screener-sync/strategies").status_code == 502
        r = client.post("/terminal/screener-sync/apply", json={"strategy_id": 7})
        assert r.status_code == 502 and "not reachable" in r.json()["detail"]

    def test_the_synced_symbols_are_watchable_straight_away(self, client):
        client.post("/terminal/screener-sync/apply", json={"strategy_id": 7, "tolerance_pct": 3})
        assert client.get("/terminal/symbols/search", params={"q": "BIGMATCH"}).status_code == 200


# ------------------------------------------------------------------------- the command line
class TestCli:
    def test_sync_screener_prints_what_it_did(self, db, capsys, monkeypatch):
        monkeypatch.setattr(cli, "get_db", lambda: db)
        monkeypatch.setattr(cli, "make_screener_client", lambda: FakeScreener(universe()))
        assert cli.main(["sync-screener", "--strategy", "7", "--tolerance", "3"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["watchlist"] == "Screen: Momentum · 1D" and out["matched"] == 2 and out["near"] == 3 and len(out["added"]) == 5

    def test_sync_all_refreshes_every_linked_list(self, db, capsys, monkeypatch):
        sync.apply_plan(db, WatchlistRepo(db), plan_for(universe(), tolerance_pct=3))
        monkeypatch.setattr(cli, "get_db", lambda: db)
        monkeypatch.setattr(cli, "make_screener_client", lambda: FakeScreener(universe()))
        assert cli.main(["sync-screener", "--all"]) == 0
        assert json.loads(capsys.readouterr().out)["results"][0]["ok"] is True

    def test_a_failure_exits_non_zero_so_a_scheduler_notices(self, db, capsys, monkeypatch):
        monkeypatch.setattr(cli, "get_db", lambda: db)
        down = FakeScreener(universe())
        down.down = True
        monkeypatch.setattr(cli, "make_screener_client", lambda: down)
        assert cli.main(["sync-screener", "--strategy", "7"]) == 1
        assert "not reachable" in capsys.readouterr().err

    def test_one_of_strategy_or_all_is_required(self, db, monkeypatch):
        monkeypatch.setattr(cli, "get_db", lambda: db)
        with pytest.raises(SystemExit):
            cli.main(["sync-screener"])


def test_exact_matches_only_asks_the_screener_for_matches_only():
    client = FakeScreener([stock("NSE:A-EQ", 65, 90, matched=True), stock("NSE:B-EQ", 50, 90)])
    got, _ = sync.fetch_entries(client, 7, matched_only=True)
    assert [e["symbol"] for e in got] == ["NSE:A-EQ"] and client.calls[-1][1]["matched_only"] == "true"


def test_per_cap_takes_the_best_n_from_each_size_bucket_and_skips_unknown_caps():
    cr = 1e7   # entries carry market cap in rupees
    entries = [stock(f"NSE:L{i}-EQ", 65, 90, matched=True, market_cap=(30000 - i) * cr) for i in range(3)]
    entries += [stock(f"NSE:M{i}-EQ", 65, 90, matched=True, market_cap=(9000 - i) * cr) for i in range(3)]
    entries += [stock("NSE:S0-EQ", 65, 90, matched=True, market_cap=0), stock("NSE:NONE-EQ", 65, 90, matched=True, market_cap=0)]
    fin = lambda sym: {"ratios": {"market_cap_cr": 2000.0}} if sym == "NSE:S0-EQ" else {"ratios": {}}   # screener.in fallback
    plan = sync.build_plan(FakeScreener(entries), 7, tolerance_pct=0, max_failing=0, per_cap=2, financials=fin)
    assert [c.symbol for c in plan.candidates] == ["NSE:L0-EQ", "NSE:L1-EQ", "NSE:M0-EQ", "NSE:M1-EQ", "NSE:S0-EQ"]
    assert [c.cap_bucket for c in plan.candidates] == ["large", "large", "mid", "mid", "small"] and plan.truncated
