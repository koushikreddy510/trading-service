"""Indicator maths. Small series are checked by hand so the expected numbers
double as a specification; RSI is also checked against Wilder's textbook example."""
import math

import numpy as np
import pytest

from trading_service.terminal import indicators as ind

nan = math.nan


def same(actual, expected, tol=1e-9):
    actual = list(actual)
    assert len(actual) == len(expected)
    for a, e in zip(actual, expected):
        if math.isnan(e):
            assert math.isnan(a), f"expected NaN, got {a}"
        else:
            assert a == pytest.approx(e, abs=tol)


def test_sma_is_nan_until_the_window_is_full():
    same(ind.sma([1, 2, 3, 4, 5], 3), [nan, nan, 2, 3, 4])


def test_ema_is_seeded_with_the_sma_then_smoothed():
    # seed = mean(1,2,3) = 2; alpha = 2/(3+1) = 0.5 -> 0.5*4+0.5*2 = 3 -> 0.5*5+0.5*3 = 4
    same(ind.ema([1, 2, 3, 4, 5], 3), [nan, nan, 2, 3, 4])


def test_ema_of_a_series_shorter_than_its_period_is_all_nan():
    assert np.isnan(ind.ema([1, 2], 3)).all()


def test_rsi_matches_wilders_textbook_example():
    # Closes and RSI(14) from Wilder's worked example (StockCharts "RSI" article).
    closes = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
              45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439]
    out = ind.rsi(closes, 14)
    assert np.isnan(out[:14]).all()
    assert out[14] == pytest.approx(70.53, abs=0.05)
    assert out[15] == pytest.approx(66.32, abs=0.05)
    assert out[19] == pytest.approx(57.97, abs=0.1)


def test_rsi_is_100_when_price_only_rises_and_0_when_it_only_falls():
    assert ind.rsi(list(range(1, 30)), 14)[-1] == 100.0
    assert ind.rsi(list(range(30, 1, -1)), 14)[-1] == 0.0


def test_true_range_uses_the_previous_close_to_capture_gaps():
    # Bar 2 gaps up: high-low is only 1 but the move from the previous close is 6.
    tr = ind.true_range([11, 17], [10, 16], [10.5, 16.5])
    same(tr, [1.0, 6.5])


def test_atr_is_wilder_smoothed_true_range():
    h = [10, 11, 12, 13]
    l = [9, 10, 11, 12]
    c = [9.5, 10.5, 11.5, 12.5]
    # TR = [1, 1.5, 1.5, 1.5]; seed(2) = 1.25; next = (1.25*1 + 1.5)/2 = 1.375; next = 1.4375
    same(ind.atr(h, l, c, 2), [nan, 1.25, 1.375, 1.4375])


def test_bollinger_uses_population_standard_deviation():
    b = ind.bollinger([1, 2, 3, 4, 5], 5, 2.0)
    sd = math.sqrt(2.0)  # population std of 1..5
    assert b["middle"][-1] == pytest.approx(3.0)
    assert b["upper"][-1] == pytest.approx(3.0 + 2 * sd)
    assert b["lower"][-1] == pytest.approx(3.0 - 2 * sd)
    assert np.isnan(b["middle"][:4]).all()


def test_bollinger_bands_collapse_on_a_flat_series():
    b = ind.bollinger([5.0] * 25, 20, 2.0)
    assert b["upper"][-1] == b["lower"][-1] == b["middle"][-1] == 5.0


def test_macd_signal_starts_after_slow_plus_signal_warmup_and_histogram_is_the_gap():
    closes = list(np.linspace(100, 140, 80) + np.sin(np.arange(80)))
    m = ind.macd(closes, 12, 26, 9)
    first_line = 26 - 1
    first_signal = first_line + 9 - 1
    assert np.isnan(m["macd"][:first_line]).all() and not np.isnan(m["macd"][first_line])
    assert np.isnan(m["signal"][:first_signal]).all() and not np.isnan(m["signal"][first_signal])
    assert m["histogram"][-1] == pytest.approx(m["macd"][-1] - m["signal"][-1])


def test_macd_rejects_fast_not_smaller_than_slow():
    with pytest.raises(ValueError):
        ind.macd([1] * 50, 26, 12, 9)


def _trend_series():
    up = [100 + i for i in range(40)]
    down = [139 - 2 * i for i in range(30)]
    closes = up + down
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    return highs, lows, closes


def test_supertrend_sits_below_price_in_an_uptrend_then_flips_above_after_a_selloff():
    h, l, c = _trend_series()
    st = ind.supertrend(h, l, c, 10, 3.0)
    assert np.isnan(st["line"][:9]).all()
    assert st["direction"][30] == 1 and st["line"][30] < c[30]       # rising market: line below price
    assert st["direction"][-1] == -1 and st["line"][-1] > c[-1]      # after the fall: line above price
    settled = st["direction"][20:]                                   # ignore the seed's warm-up bars
    assert len(np.flatnonzero(np.diff(settled) != 0)) == 1           # one clean reversal, no whipsaw


def test_supertrend_line_never_loosens_against_the_trend():
    h, l, c = _trend_series()
    st = ind.supertrend(h, l, c, 10, 3.0)
    up = [i for i in range(9, 40) if st["direction"][i] == 1]
    line = [st["line"][i] for i in up]
    assert line == sorted(line)  # in an uptrend the stop-line can only ratchet up


def test_compute_fills_default_parameters_and_names_outputs():
    h, l, c = _trend_series()
    out = ind.compute("macd", None, h, l, c)
    assert set(out) == {"macd", "signal", "histogram"}
    assert ind.default_output("bollinger") == "middle"
    assert ind.default_output("supertrend") == "line"


@pytest.mark.parametrize(
    "name, params, message",
    [
        ("vwma", None, "unknown indicator"),
        ("ema", {"length": 10}, "unknown parameter"),
        ("ema", {"period": 0}, "must be a positive number"),
        ("ema", {"period": "ten"}, "must be a positive number"),
    ],
)
def test_compute_rejects_bad_input(name, params, message):
    with pytest.raises(ValueError, match=message):
        ind.compute(name, params, [1, 2, 3], [1, 2, 3], [1, 2, 3])


# --- the second batch: stochastic, CCI, Williams %R, ADX, Donchian, Keltner, ROC, Parabolic SAR ---

def test_williams_r_is_minus_100_at_the_low_and_zero_at_the_high():
    same(ind.williams_r([3, 4, 5], [1, 2, 3], [2, 3, 5], 3)[2:], [0.0])
    same(ind.williams_r([5, 4, 3], [3, 2, 1], [4, 3, 1], 3)[2:], [-100.0])


def test_stochastic_k_is_where_the_close_sits_in_the_period_range():
    h, l, c = [10, 12, 14, 12], [8, 9, 10, 9], [9, 11, 14, 10]
    k = ind.stochastic(h, l, c, 3, 1, 1)["k"]
    same(k[2:], [100.0, 100 * (10 - 9) / (14 - 9)])


def test_donchian_is_the_rolling_high_low_and_their_midpoint():
    d = ind.donchian([1, 5, 3, 4], [0, 2, 1, 2], 2)
    same(d["upper"], [nan, 5, 5, 4])
    same(d["lower"], [nan, 0, 1, 1])
    same(d["middle"], [nan, 2.5, 3, 2.5])


def test_roc_is_percent_change_over_the_period():
    same(ind.roc([100, 110, 121], 1), [nan, 10.0, 10.0])


def test_cci_is_zero_on_a_flat_series():
    out = ind.cci([5] * 6, [5] * 6, [5] * 6, 3)
    same(out[2:], [0.0] * 4)


def test_adx_reads_high_and_plus_di_leads_in_a_steady_uptrend():
    h, l, c = (x[:40] for x in _trend_series())  # the rising leg only
    out = ind.adx(h, l, c, 14)
    assert out["adx"][-1] > 25 and out["plus_di"][-1] > out["minus_di"][-1]


def test_keltner_bands_straddle_the_ema():
    h, l, c = _trend_series()
    k = ind.keltner(h, l, c, 20, 10, 2.0)
    assert k["upper"][-1] > k["middle"][-1] > k["lower"][-1]


def test_psar_trails_below_price_in_an_uptrend():
    h, l, c = (x[:40] for x in _trend_series())
    sar = ind.psar(h, l, c, 0.02, 0.2)
    assert sar[-1] < c[-1] and sar[-1] > sar[-10]


def test_new_indicators_keep_float_parameters():
    assert ind.normalise_params("keltner", {"mult": 1.5})["mult"] == 1.5
    assert ind.normalise_params("psar", {"step": 0.05})["step"] == 0.05
    assert set(ind.compute("adx", None, *_trend_series())) == {"adx", "plus_di", "minus_di"}
