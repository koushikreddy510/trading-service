"""Write the golden vectors that keep the TypeScript copies of the maths identical to Python.

The browser re-computes indicators, candle buckets and trend-line prices so charts
update instantly without a round trip. To make sure the two implementations cannot
drift, this script records what the PYTHON code produces for a fixed input, and the
frontend tests (web/src/workspace/lib/*.test.ts) must reproduce it exactly.

    python scripts/make_golden_fixtures.py                       # tests/terminal/fixtures/golden.json
    python scripts/make_golden_fixtures.py --also ../trade/web/src/workspace/lib/golden.json

`tests/terminal/test_golden.py` fails if the committed file is stale.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import timedelta
from pathlib import Path

import numpy as np

from trading_service.terminal import indicators as ind
from trading_service.terminal.drawings import Drawing, anchor_index, line_series
from trading_service.terminal.timeframes import (
    IST, TIMEFRAMES, UTC, bucket_end, bucket_start, from_epoch, in_session, to_epoch,
)
from datetime import datetime

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "tests" / "terminal" / "fixtures" / "golden.json"


def _clean(arr) -> list:
    return [None if (isinstance(v, float) and math.isnan(v)) else round(float(v), 9) for v in np.asarray(arr, dtype=float)]


def series() -> dict:
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0, 1.2, 140))
    open_ = np.concatenate(([100.0], close[:-1]))
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.6, 140))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.6, 140))
    r = lambda a: [round(float(x), 4) for x in a]  # noqa: E731
    return {"open": r(open_), "high": r(high), "low": r(low), "close": r(close)}


INDICATORS = [
    ("sma", {"period": 5}), ("sma", {"period": 20}), ("ema", {"period": 9}), ("ema", {"period": 20}),
    ("rsi", {"period": 14}), ("atr", {"period": 14}), ("bollinger", {"period": 20, "mult": 2.0}),
    ("macd", {"fast": 12, "slow": 26, "signal": 9}), ("supertrend", {"period": 10, "multiplier": 3.0}),
    ("stochastic", {"period": 14, "smooth_k": 3, "smooth_d": 3}), ("cci", {"period": 20}), ("williams_r", {"period": 14}), ("adx", {"period": 14}),
    ("donchian", {"period": 20}), ("keltner", {"period": 20, "atr_period": 10, "mult": 2.0}), ("roc", {"period": 12}), ("psar", {"step": 0.02, "max_step": 0.2}),
]


def indicator_cases(s: dict) -> list:
    out = []
    for name, params in INDICATORS:
        result = ind.compute(name, params, s["high"], s["low"], s["close"])
        out.append({"name": name, "params": params, "outputs": {k: _clean(v) for k, v in result.items()}})
    return out


def timeframe_cases() -> list:
    stamps = [
        (2026, 10, 19, 9, 14, 59), (2026, 10, 19, 9, 15, 0), (2026, 10, 19, 10, 14, 59), (2026, 10, 19, 10, 15, 0),
        (2026, 10, 19, 13, 20, 0), (2026, 10, 19, 15, 29, 59), (2026, 10, 19, 15, 30, 0), (2026, 10, 21, 12, 7, 31),
        (2026, 10, 23, 15, 29, 0), (2026, 10, 24, 11, 0, 0), (2026, 10, 25, 11, 0, 0), (2026, 10, 19, 23, 29, 59),
        (2026, 10, 19, 23, 30, 0), (2026, 10, 19, 8, 59, 0),
    ]
    cases = []
    for symbol, exchange in (("NSE:SBIN-EQ", "NSE"), ("MCX:SILVERM", "MCX")):
        for y, mo, d, h, mi, s in stamps:
            ts = datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(UTC)
            row = {"symbol": symbol, "exchange": exchange, "ts": to_epoch(ts), "in_session": in_session(ts, exchange), "buckets": {}}
            for tf in TIMEFRAMES:
                start = bucket_start(ts, tf, exchange)
                row["buckets"][tf] = {"start": to_epoch(start), "end": to_epoch(bucket_end(start, tf, exchange))}
            cases.append(row)
    return cases


def line_cases() -> list:
    # 1H NSE bars across a weekend (Fri 15:15 -> Mon 09:15) so index space differs from clock time
    times = []
    day = datetime(2026, 10, 16, tzinfo=IST)
    for d in range(0, 6):
        cur = day + timedelta(days=d)
        if cur.weekday() >= 5:
            continue
        for h, m in [(9, 15), (10, 15), (11, 15), (12, 15), (13, 15), (14, 15), (15, 15)]:
            times.append(to_epoch(cur.replace(hour=h, minute=m).astimezone(UTC)))
    p1, p2 = times[3], times[12]
    drawings = [
        Drawing("NSE:SBIN-EQ", "trend_line", [{"time": p1, "price": 100.0}, {"time": p2, "price": 110.0}], id="t1"),
        Drawing("NSE:SBIN-EQ", "ray", [{"time": p1 + 1800, "price": 105.0}, {"time": p2 + 600, "price": 101.0}], id="r1"),
        Drawing("NSE:SBIN-EQ", "horizontal_line", [{"time": p1, "price": 99.5}], id="h1"),
    ]
    probes = [times[0] - 7200, p1, p1 + 1800, times[8] + 100, times[-1], times[-1] + 3600 * 5]
    return [{
        "times": times, "timeframe": "1H",
        "drawings": [d.to_json() for d in drawings],
        "series": {d.id: _clean([v if v is not None else float("nan") for v in line_series(d, times, "1H")]) for d in drawings},
        "anchors": [{"t": t, "index": round(anchor_index(times, t, 3600.0), 9)} for t in probes],
    }]


def build() -> dict:
    s = series()
    return {"series": s, "indicators": indicator_cases(s), "timeframes": timeframe_cases(), "lines": line_cases()}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--also", action="append", default=[], help="extra copy to write (e.g. the web app's fixture)")
    args = p.parse_args()
    text = json.dumps(build(), indent=1, sort_keys=True) + "\n"
    for target in [args.out, *args.also]:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(text)
        print("wrote", target)


if __name__ == "__main__":
    main()
