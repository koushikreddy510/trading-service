"""The daily screener: one scan of 1D candles, then instant filters over its results.

Read these top to bottom: first what each stored metric means (on tiny hand-made
series), then how a scan fills the table, then how screens filter it.
"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text

from trading_service.terminal import screener
from trading_service.terminal.candles import Candle

D0 = date(2026, 1, 1)


def daily(closes, volumes=None, spread=1.0):
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        v = volumes[i] if volumes else 1000
        out.append(Candle(datetime.combine(D0 + timedelta(days=i), datetime.min.time()), prev, max(prev, c) + spread, min(prev, c) - spread, c, v))
        prev = c
    return out


class TestMetrics:
    def test_too_little_history_is_not_screened(self):
        assert screener.compute_metrics(daily([100.0] * (screener.MIN_BARS - 1))) is None

    def test_close_change_and_volume_describe_the_last_bar(self):
        m = screener.compute_metrics(daily([100.0] * 29 + [110.0], [1000] * 29 + [3000]))
        assert m["close"] == 110.0
        assert m["change_pct"] == pytest.approx(10.0)
        assert m["volume"] == 3000

    def test_volume_ratio_compares_today_to_the_previous_twenty_days(self):
        m = screener.compute_metrics(daily([100.0] * 30, [1000] * 29 + [3000]))
        assert m["avg_volume_20"] == 1000          # today is NOT part of its own average
        assert m["volume_ratio"] == pytest.approx(3.0)

    def test_moving_averages_need_their_full_window(self):
        m = screener.compute_metrics(daily([100.0] * 60))
        assert m["sma_50"] == pytest.approx(100.0) and m["ema_20"] == pytest.approx(100.0)
        assert m["sma_200"] is None, "only 60 bars: a 200-day average would be misleading, so it is empty"

    def test_returns_look_back_a_fixed_number_of_trading_days(self):
        closes = [100.0 + i for i in range(70)]            # 100 .. 169
        m = screener.compute_metrics(daily(closes))
        assert m["ret_20d"] == pytest.approx((169 / 149 - 1) * 100)
        assert m["ret_60d"] == pytest.approx((169 / 109 - 1) * 100)
        assert m["ret_120d"] is None

    def test_52_week_range_and_distance_from_it(self):
        closes = [100.0] * 20 + [150.0] + [120.0] * 20
        m = screener.compute_metrics(daily(closes, spread=0))
        assert m["high_52w"] == 150.0 and m["low_52w"] == 100.0
        assert m["pct_from_52w_high"] == pytest.approx(-20.0)     # 120 is 20% below 150
        assert m["pct_from_52w_low"] == pytest.approx(20.0)

    def test_a_steady_rise_is_overbought_and_in_a_supertrend_uptrend(self):
        m = screener.compute_metrics(daily([100.0 + i for i in range(60)]))
        assert m["rsi_14"] == pytest.approx(100.0)
        assert m["supertrend_dir"] == 1 and m["supertrend_value"] < m["close"]

    def test_a_steady_fall_is_oversold_and_in_a_downtrend(self):
        m = screener.compute_metrics(daily([200.0 - i for i in range(60)]))
        assert m["rsi_14"] == pytest.approx(0.0)
        assert m["supertrend_dir"] == -1

    def test_atr_percent_is_relative_to_price(self):
        m = screener.compute_metrics(daily([100.0] * 30, spread=2.0))     # high-low range is 4 every day
        assert m["atr_14"] == pytest.approx(4.0)
        assert m["atr_pct"] == pytest.approx(4.0)


def load_daily(db, symbol, bars):
    with db.tx() as conn:
        conn.execute(
            text("INSERT INTO ohlcv_1d (tradingsymbol, date, open, high, low, close, volume) VALUES (:s,:d,:o,:h,:l,:c,:v)"),
            [{"s": symbol, "d": b.time.date(), "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume} for b in bars],
        )


@pytest.fixture
def market(db):
    """Five symbols that behave differently, all ending on the same day."""
    n = 260
    load_daily(db, "NSE:UP-EQ", daily([100.0 + i * 0.5 for i in range(n)], [1000] * n))
    load_daily(db, "NSE:DOWN-EQ", daily([300.0 - i * 0.5 for i in range(n)], [1000] * n))
    spike = [100.0] * (n - 1) + [106.0]
    load_daily(db, "NSE:SPIKE-EQ", daily(spike, [1000] * (n - 1) + [4000]))
    load_daily(db, "NSE:NEW-EQ", daily([50.0] * 10))                                       # too little history
    old = daily([100.0] * 60)
    load_daily(db, "NSE:STALE-EQ", [Candle(b.time - timedelta(days=60), *[getattr(b, f) for f in ("open", "high", "low", "close", "volume")]) for b in old])
    for sym, name, sector, cap in [("NSE:UP-EQ", "Up Co", "IT", 5000.0), ("NSE:DOWN-EQ", "Down Co", "Banks", 90000.0), ("NSE:SPIKE-EQ", "Spike Co", "IT", 12000.0)]:
        db.execute("INSERT INTO symbols (tradingsymbol, name, sector, market_cap) VALUES (:s,:n,:c,:m)", s=sym, n=name, c=sector, m=cap * 1e7)  # rupees, as the data-store writes it
    return db


RUN = date(2026, 10, 19)


class TestScan:
    def test_stores_one_row_per_screenable_symbol(self, market):
        out = screener.run_scan(market, RUN)
        assert out == {"run_date": "2026-10-19", "scanned": 5, "stored": 3, "skipped": 2}
        rows = market.rows("SELECT symbol FROM terminal_screener_metrics WHERE run_date = :d ORDER BY symbol", d=RUN)
        assert [r["symbol"] for r in rows] == ["NSE:DOWN-EQ", "NSE:SPIKE-EQ", "NSE:UP-EQ"]

    def test_short_history_and_stale_symbols_are_skipped(self, market):
        screener.run_scan(market, RUN)
        stored = {r["symbol"] for r in market.rows("SELECT symbol FROM terminal_screener_metrics")}
        assert "NSE:NEW-EQ" not in stored       # only 10 bars
        assert "NSE:STALE-EQ" not in stored     # last bar two months before everyone else's

    def test_running_the_scan_twice_the_same_day_replaces_not_duplicates(self, market):
        screener.run_scan(market, RUN)
        screener.run_scan(market, RUN)
        assert market.scalar("SELECT count(*) FROM terminal_screener_metrics WHERE run_date = :d", d=RUN) == 3

    def test_old_runs_are_pruned_but_recent_ones_kept(self, market):
        screener.run_scan(market, RUN - timedelta(days=45))
        screener.run_scan(market, RUN - timedelta(days=2))
        screener.run_scan(market, RUN)
        dates = {r["run_date"] for r in market.rows("SELECT DISTINCT run_date FROM terminal_screener_metrics")}
        assert dates == {RUN - timedelta(days=2), RUN}

    def test_asof_is_the_date_of_the_last_candle_used(self, market):
        screener.run_scan(market, RUN)
        row = market.row("SELECT asof FROM terminal_screener_metrics WHERE symbol = 'NSE:UP-EQ'")
        assert row["asof"] == market.scalar("SELECT max(date) FROM ohlcv_1d WHERE tradingsymbol = 'NSE:UP-EQ'")

    def test_a_scan_can_be_limited_to_some_symbols(self, market):
        out = screener.run_scan(market, RUN, symbols=["NSE:UP-EQ"])
        assert out["stored"] == 1

    def test_without_daily_data_the_scan_does_nothing(self, db):
        db.execute("DROP TABLE ohlcv_1d")
        assert screener.run_scan(db, RUN)["stored"] == 0


@pytest.fixture
def scanned(market):
    screener.run_scan(market, RUN)
    return market


def syms(result):
    return [r["symbol"] for r in result["rows"]]


class TestScreens:
    def test_no_scan_yet_means_an_empty_result(self, db):
        assert screener.run_screen(db) == {"run_date": None, "count": 0, "rows": []}

    def test_a_bad_filter_is_an_error_even_before_any_scan_exists(self, db):
        with pytest.raises(screener.ScreenError):
            screener.run_screen(db, [{"field": "nope", "op": ">", "value": 1}])

    def test_a_numeric_filter(self, scanned):
        assert syms(screener.run_screen(scanned, [{"field": "rsi_14", "op": "<=", "value": 30}])) == ["NSE:DOWN-EQ"]

    def test_filters_are_anded(self, scanned):
        both = [{"field": "volume_ratio", "op": ">=", "value": 2}, {"field": "change_pct", "op": ">", "value": 5}]
        assert syms(screener.run_screen(scanned, both)) == ["NSE:SPIKE-EQ"]
        none = both + [{"field": "rsi_14", "op": "<", "value": 10}]
        assert syms(screener.run_screen(scanned, none)) == []

    def test_a_column_can_be_compared_with_another_column(self, scanned):
        above_200 = [{"field": "close", "op": ">", "value": {"field": "sma_200"}}]
        assert set(syms(screener.run_screen(scanned, above_200))) == {"NSE:UP-EQ", "NSE:SPIKE-EQ"}

    def test_results_carry_the_company_details(self, scanned):
        (row,) = screener.run_screen(scanned, [{"field": "market_cap", "op": ">", "value": 50000}])["rows"]
        assert (row["symbol"], row["name"], row["sector"], row["market_cap"]) == ("NSE:DOWN-EQ", "Down Co", "Banks", 90000.0)

    def test_sorting_and_limit(self, scanned):
        res = screener.run_screen(scanned, sort="close", descending=True, limit=2)
        assert len(res["rows"]) == 2 and res["count"] == 3, "count is the total matches, rows is the page"
        closes = [r["close"] for r in res["rows"]]
        assert closes == sorted(closes, reverse=True)

    def test_rows_without_a_value_sort_last(self, scanned):
        # 260 bars is enough for SMA200 on all three, so make one metric missing
        scanned.execute("UPDATE terminal_screener_metrics SET ret_120d = NULL WHERE symbol = 'NSE:UP-EQ'")
        res = screener.run_screen(scanned, sort="ret_120d", descending=True)
        assert syms(res)[-1] == "NSE:UP-EQ"

    @pytest.mark.parametrize(
        "bad_filter",
        [
            {"field": "close; DROP TABLE x", "op": ">", "value": 1},
            {"field": "close", "op": "LIKE", "value": 1},
            {"field": "close", "op": ">", "value": "100"},
            {"field": "close", "op": ">", "value": {"field": "nope"}},
            {"field": "close", "op": ">", "value": float("nan")},
        ],
    )
    def test_filters_are_validated_never_pasted_into_sql(self, scanned, bad_filter):
        with pytest.raises(screener.ScreenError):
            screener.run_screen(scanned, [bad_filter])

    def test_sort_column_is_validated(self, scanned):
        with pytest.raises(screener.ScreenError):
            screener.run_screen(scanned, sort="close; DROP TABLE x")

    def test_works_without_the_symbols_table(self, scanned):
        scanned.execute("DROP TABLE symbols")
        res = screener.run_screen(scanned)
        assert res["count"] == 3 and res["rows"][0]["name"] is None


class TestPresets:
    def test_every_preset_is_a_valid_screen(self, scanned):
        for name in screener.PRESETS:
            assert screener.run_preset(scanned, name)["preset"] == name

    def test_uptrend_picks_the_rising_stock_not_the_falling_one(self, scanned):
        res = syms(screener.run_preset(scanned, "uptrend"))
        assert "NSE:UP-EQ" in res and "NSE:DOWN-EQ" not in res

    def test_oversold(self, scanned):
        assert syms(screener.run_preset(scanned, "oversold")) == ["NSE:DOWN-EQ"]

    def test_volume_breakout_finds_the_spike(self, scanned):
        assert syms(screener.run_preset(scanned, "volume_breakout")) == ["NSE:SPIKE-EQ"]

    def test_near_52w_high(self, scanned):
        assert "NSE:UP-EQ" in syms(screener.run_preset(scanned, "near_52w_high"))
        assert "NSE:DOWN-EQ" not in syms(screener.run_preset(scanned, "near_52w_high"))

    def test_unknown_preset(self, scanned):
        with pytest.raises(screener.ScreenError, match="unknown preset"):
            screener.run_preset(scanned, "nope")
