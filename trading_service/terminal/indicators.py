"""Technical indicators (numpy).

Conventions follow TradingView so numbers on the chart match what the backend
evaluates for rules and the daily screener:

* every function returns an array the same length as its input, with NaN during
  the warm-up period (never a misleading partial value);
* EMA is seeded with the SMA of its first `period` values;
* RSI and ATR use Wilder's smoothing (RMA), seeded with a simple average;
* Bollinger bands use the population standard deviation (ddof=0).

`web/src/lib/indicators.ts` mirrors this file. `tests/terminal/test_indicators.py`
writes golden vectors that the frontend tests replay, so both stay identical.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np

Array = np.ndarray


def _arr(values: Sequence[float] | Array) -> Array:
    return np.asarray(values, dtype=float)


def _check_period(n: int, name: str = "period") -> int:
    if int(n) != n or n < 1:
        raise ValueError(f"{name} must be a positive integer, got {n!r}")
    return int(n)


def sma(values: Sequence[float] | Array, period: int) -> Array:
    v = _arr(values)
    n = _check_period(period)
    out = np.full(len(v), np.nan)
    if len(v) >= n:
        csum = np.cumsum(np.insert(v, 0, 0.0))
        out[n - 1 :] = (csum[n:] - csum[:-n]) / n
    return out


def ema(values: Sequence[float] | Array, period: int) -> Array:
    """EMA seeded with the SMA of the first `period` valid values.

    Leading NaNs in the input are skipped, which is what lets MACD's signal line
    be an EMA of the (warm-up-padded) MACD line.
    """
    v = _arr(values)
    n = _check_period(period)
    out = np.full(len(v), np.nan)
    valid = np.flatnonzero(~np.isnan(v))
    if valid.size == 0:
        return out
    first = int(valid[0])
    seed = first + n - 1
    if seed >= len(v):
        return out
    out[seed] = v[first : seed + 1].mean()
    alpha = 2.0 / (n + 1)
    for i in range(seed + 1, len(v)):
        out[i] = alpha * v[i] + (1.0 - alpha) * out[i - 1]
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    # Same edge cases as Pine: no losses -> 100, no gains -> 0.
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi(values: Sequence[float] | Array, period: int = 14) -> Array:
    v = _arr(values)
    n = _check_period(period)
    out = np.full(len(v), np.nan)
    if len(v) <= n:
        return out
    delta = np.diff(v)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = gain[:n].mean()
    avg_loss = loss[:n].mean()
    out[n] = _rsi_from(avg_gain, avg_loss)
    for i in range(n, len(delta)):
        avg_gain = (avg_gain * (n - 1) + gain[i]) / n
        avg_loss = (avg_loss * (n - 1) + loss[i]) / n
        out[i + 1] = _rsi_from(avg_gain, avg_loss)
    return out


def true_range(high: Sequence[float] | Array, low: Sequence[float] | Array, close: Sequence[float] | Array) -> Array:
    h, l, c = _arr(high), _arr(low), _arr(close)
    tr = np.empty(len(h))
    if len(h) == 0:
        return tr
    tr[0] = h[0] - l[0]
    if len(h) > 1:
        prev = c[:-1]
        tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - prev), np.abs(l[1:] - prev)])
    return tr


def atr(high: Sequence[float] | Array, low: Sequence[float] | Array, close: Sequence[float] | Array, period: int = 14) -> Array:
    n = _check_period(period)
    tr = true_range(high, low, close)
    out = np.full(len(tr), np.nan)
    if len(tr) < n:
        return out
    out[n - 1] = tr[:n].mean()
    for i in range(n, len(tr)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def bollinger(values: Sequence[float] | Array, period: int = 20, mult: float = 2.0) -> dict[str, Array]:
    v = _arr(values)
    n = _check_period(period)
    mid = sma(v, n)
    sd = np.full(len(v), np.nan)
    if len(v) >= n:
        windows = np.lib.stride_tricks.sliding_window_view(v, n)
        sd[n - 1 :] = windows.std(axis=1)  # population std, like TradingView
    return {"upper": mid + mult * sd, "middle": mid, "lower": mid - mult * sd}


def macd(values: Sequence[float] | Array, fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, Array]:
    if fast >= slow:
        raise ValueError("macd: fast period must be smaller than slow period")
    v = _arr(values)
    line = ema(v, fast) - ema(v, slow)
    sig = ema(line, signal)
    return {"macd": line, "signal": sig, "histogram": line - sig}


def supertrend(
    high: Sequence[float] | Array,
    low: Sequence[float] | Array,
    close: Sequence[float] | Array,
    period: int = 10,
    multiplier: float = 3.0,
) -> dict[str, Array]:
    """Supertrend line and direction (+1 uptrend, -1 downtrend; NaN in warm-up).

    The first direction is seeded from the close versus the bar midpoint (a tie
    counts as up), so the first few bars can flip once while it settles - the usual
    Supertrend warm-up. After that the bands only tighten in the trend's favour and
    the direction flips when the close crosses the active band.
    """
    h, l, c = _arr(high), _arr(low), _arr(close)
    a = atr(h, l, c, period)
    line = np.full(len(c), np.nan)
    direction = np.full(len(c), np.nan)
    start = period - 1
    if len(c) <= start:
        return {"line": line, "direction": direction}
    hl2 = (h + l) / 2.0
    final_upper = final_lower = 0.0
    trend = 1
    for i in range(start, len(c)):
        upper = hl2[i] + multiplier * a[i]
        lower = hl2[i] - multiplier * a[i]
        if i == start:
            final_upper, final_lower = upper, lower
            trend = 1 if c[i] >= hl2[i] else -1
        else:
            prev_upper, prev_lower = final_upper, final_lower
            final_upper = upper if (upper < prev_upper or c[i - 1] > prev_upper) else prev_upper
            final_lower = lower if (lower > prev_lower or c[i - 1] < prev_lower) else prev_lower
            if trend == -1 and c[i] > final_upper:
                trend = 1
            elif trend == 1 and c[i] < final_lower:
                trend = -1
        line[i] = final_lower if trend == 1 else final_upper
        direction[i] = trend
    return {"line": line, "direction": direction}


# ------------------------------------------------- oscillators and channels (high / low / close only)
def _rolling(values: Array, period: int, fn: Any) -> Array:
    out = np.full(len(values), np.nan)
    if len(values) >= period:
        out[period - 1 :] = fn(np.lib.stride_tricks.sliding_window_view(values, period), axis=1)
    return out


def _rma(values: Array, period: int) -> Array:
    """Wilder's moving average, seeded with the simple average of the first `period` valid values (leading NaNs skipped)."""
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if valid.size == 0:
        return out
    seed = int(valid[0]) + period - 1
    if seed >= len(values):
        return out
    out[seed] = values[seed - period + 1 : seed + 1].mean()
    for i in range(seed + 1, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def stochastic(high, low, close, period: int = 14, smooth_k: int = 3, smooth_d: int = 3) -> dict[str, Array]:
    """Stochastic oscillator: %K is the close's place in the last `period` bars' range (0-100), smoothed; %D smooths %K again."""
    h, l, c = _arr(high), _arr(low), _arr(close)
    hh, ll = _rolling(h, period, np.max), _rolling(l, period, np.min)
    span = hh - ll
    with np.errstate(invalid="ignore", divide="ignore"):
        raw = np.where(span > 0, 100.0 * (c - ll) / span, 0.0)
    raw[np.isnan(hh)] = np.nan
    k = sma(raw[period - 1 :], smooth_k) if len(raw) >= period else np.full(0, np.nan)
    k = np.concatenate([np.full(min(period - 1, len(raw)), np.nan), k])
    d = np.full(len(k), np.nan)
    first = period - 1 + smooth_k - 1
    if len(k) > first:
        d[first:] = sma(k[first:], smooth_d)
    return {"k": k, "d": d}


def cci(high, low, close, period: int = 20) -> Array:
    """Commodity Channel Index: how far the typical price is from its average, in units of mean deviation (x 0.015)."""
    tp = (_arr(high) + _arr(low) + _arr(close)) / 3.0
    mean = sma(tp, period)
    out = np.full(len(tp), np.nan)
    if len(tp) >= period:
        w = np.lib.stride_tricks.sliding_window_view(tp, period)
        dev = np.abs(w - w.mean(axis=1, keepdims=True)).mean(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[period - 1 :] = np.where(dev > 0, (tp[period - 1 :] - mean[period - 1 :]) / (0.015 * dev), 0.0)
    return out


def williams_r(high, low, close, period: int = 14) -> Array:
    """Williams %R: the close's place in the recent range, from 0 (at the high) to -100 (at the low)."""
    h, l, c = _arr(high), _arr(low), _arr(close)
    hh, ll = _rolling(h, period, np.max), _rolling(l, period, np.min)
    span = hh - ll
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(span > 0, -100.0 * (hh - c) / span, 0.0)
    out[np.isnan(hh)] = np.nan
    return out


def adx(high, low, close, period: int = 14) -> dict[str, Array]:
    """Average Directional Index with +DI / -DI (Wilder smoothing, as in TradingView's DMI)."""
    h, l, c = _arr(high), _arr(low), _arr(close)
    n = len(c)
    out = {"adx": np.full(n, np.nan), "plus_di": np.full(n, np.nan), "minus_di": np.full(n, np.nan)}
    if n <= period:
        return out
    up, down = np.full(n, np.nan), np.full(n, np.nan)
    up[1:], down[1:] = h[1:] - h[:-1], l[:-1] - l[1:]
    plus_dm = np.where(np.isnan(up), np.nan, np.where((up > down) & (up > 0), up, 0.0))
    minus_dm = np.where(np.isnan(down), np.nan, np.where((down > up) & (down > 0), down, 0.0))
    tr = _rma(true_range(h, l, c), period)
    with np.errstate(invalid="ignore", divide="ignore"):
        plus = 100.0 * _rma(plus_dm, period) / tr
        minus = 100.0 * _rma(minus_dm, period) / tr
        total = plus + minus
        dx = np.where(total > 0, 100.0 * np.abs(plus - minus) / total, 0.0)
    dx[np.isnan(plus) | np.isnan(minus)] = np.nan
    out.update(plus_di=plus, minus_di=minus, adx=_rma(dx, period))
    return out


def donchian(high, low, period: int = 20) -> dict[str, Array]:
    """Donchian channel: the highest high and lowest low of the last `period` bars, and the middle between them."""
    upper, lower = _rolling(_arr(high), period, np.max), _rolling(_arr(low), period, np.min)
    return {"upper": upper, "middle": (upper + lower) / 2.0, "lower": lower}


def keltner(high, low, close, period: int = 20, atr_period: int = 10, mult: float = 2.0) -> dict[str, Array]:
    """Keltner channel: an EMA of the close with bands a multiple of ATR either side."""
    mid = ema(close, period)
    band = mult * atr(high, low, close, atr_period)
    return {"upper": mid + band, "middle": mid, "lower": mid - band}


def roc(close, period: int = 12) -> Array:
    """Rate of change: % move of the close over `period` bars."""
    c = _arr(close)
    out = np.full(len(c), np.nan)
    if len(c) > period:
        with np.errstate(invalid="ignore", divide="ignore"):
            out[period:] = np.where(c[:-period] != 0, 100.0 * (c[period:] - c[:-period]) / c[:-period], np.nan)
    return out


def psar(high, low, close, step: float = 0.02, max_step: float = 0.2) -> Array:
    """Parabolic SAR, a port of TradingView's ta.sar (the first bar has no value)."""
    h, l, c = _arr(high), _arr(low), _arr(close)
    n = len(c)
    out = np.full(n, np.nan)
    result = max_min = accel = 0.0
    below = False
    for i in range(1, n):
        first_trend_bar = False
        if i == 1:
            if c[1] > c[0]:
                below, max_min, result = True, h[1], l[0]
            else:
                below, max_min, result = False, l[1], h[0]
            first_trend_bar, accel = True, step
        result = result + accel * (max_min - result)
        if below:
            if result > l[i]:
                first_trend_bar, below = True, False
                result, max_min, accel = max(h[i], max_min), l[i], step
        elif result < h[i]:
            first_trend_bar, below = True, True
            result, max_min, accel = min(l[i], max_min), h[i], step
        if not first_trend_bar:
            if below and h[i] > max_min:
                max_min, accel = h[i], min(accel + step, max_step)
            elif not below and l[i] < max_min:
                max_min, accel = l[i], min(accel + step, max_step)
        if below:
            result = min(result, l[i - 1])
            if i > 1:
                result = min(result, l[i - 2])
        else:
            result = max(result, h[i - 1])
            if i > 1:
                result = max(result, h[i - 2])
        out[i] = result
    return out


# ------------------------------------------------------------------ registry
# name -> (default params, output names; the first output is the default one)
SPECS: dict[str, tuple[dict[str, float], tuple[str, ...]]] = {
    "sma": ({"period": 20}, ("value",)),
    "ema": ({"period": 20}, ("value",)),
    "rsi": ({"period": 14}, ("value",)),
    "atr": ({"period": 14}, ("value",)),
    "bollinger": ({"period": 20, "mult": 2.0}, ("middle", "upper", "lower")),
    "macd": ({"fast": 12, "slow": 26, "signal": 9}, ("macd", "signal", "histogram")),
    "supertrend": ({"period": 10, "multiplier": 3.0}, ("line", "direction")),
    "stochastic": ({"period": 14, "smooth_k": 3, "smooth_d": 3}, ("k", "d")),
    "cci": ({"period": 20}, ("value",)),
    "williams_r": ({"period": 14}, ("value",)),
    "adx": ({"period": 14}, ("adx", "plus_di", "minus_di")),
    "donchian": ({"period": 20}, ("middle", "upper", "lower")),
    "keltner": ({"period": 20, "atr_period": 10, "mult": 2.0}, ("middle", "upper", "lower")),
    "roc": ({"period": 12}, ("value",)),
    "psar": ({"step": 0.02, "max_step": 0.2}, ("value",)),
}
FLOAT_PARAMS = {"mult", "multiplier", "step", "max_step"}


def default_output(name: str) -> str:
    return SPECS[name][1][0]


def normalise_params(name: str, params: Mapping[str, Any] | None) -> dict[str, float]:
    """Fill defaults and reject unknown or invalid parameters."""
    if name not in SPECS:
        raise ValueError(f"unknown indicator {name!r}; expected one of {', '.join(SPECS)}")
    defaults = SPECS[name][0]
    given = dict(params or {})
    unknown = set(given) - set(defaults)
    if unknown:
        raise ValueError(f"{name}: unknown parameter(s) {', '.join(sorted(unknown))}")
    merged = {**defaults, **given}
    for key, value in merged.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name}: parameter {key!r} must be a positive number")
    return {k: (float(v) if k in FLOAT_PARAMS else int(v)) for k, v in merged.items()}


def compute(
    name: str,
    params: Mapping[str, Any] | None,
    high: Sequence[float] | Array,
    low: Sequence[float] | Array,
    close: Sequence[float] | Array,
) -> dict[str, Array]:
    """Run an indicator by name. Always returns {output_name: array}."""
    p = normalise_params(name, params)
    if name == "sma":
        return {"value": sma(close, p["period"])}
    if name == "ema":
        return {"value": ema(close, p["period"])}
    if name == "rsi":
        return {"value": rsi(close, p["period"])}
    if name == "atr":
        return {"value": atr(high, low, close, p["period"])}
    if name == "bollinger":
        return bollinger(close, p["period"], p["mult"])
    if name == "macd":
        return macd(close, p["fast"], p["slow"], p["signal"])
    if name == "supertrend":
        return supertrend(high, low, close, p["period"], p["multiplier"])
    if name == "stochastic":
        return stochastic(high, low, close, p["period"], p["smooth_k"], p["smooth_d"])
    if name == "cci":
        return {"value": cci(high, low, close, p["period"])}
    if name == "williams_r":
        return {"value": williams_r(high, low, close, p["period"])}
    if name == "adx":
        return adx(high, low, close, p["period"])
    if name == "donchian":
        return donchian(high, low, p["period"])
    if name == "keltner":
        return keltner(high, low, close, p["period"], p["atr_period"], p["mult"])
    if name == "roc":
        return {"value": roc(close, p["period"])}
    return {"value": psar(high, low, close, p["step"], p["max_step"])}
