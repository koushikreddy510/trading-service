"""The daily screener.

How it works, and why it is shaped this way:

1. ONCE A DAY (after the data-store's 1D OHLCV job) `run_scan` reads each symbol's
   daily candles, computes a fixed set of metrics from them (moving averages, RSI,
   ATR, 52-week range, returns, volume ratio, Supertrend) and stores one row per
   symbol in `terminal_screener_metrics`. This is the only expensive step and it never
   runs on a tick.
2. A "screen" is just a list of filters over those stored rows - plain SQL, so it
   answers instantly and any number of screens can be run or saved without
   recomputing anything.

The metrics use the same indicator code as the chart and the rule engine
(`indicators.py`), so a number shown in the screener is the number a rule would see.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np
from sqlalchemy import text

from . import indicators as ind
from .candles import Candle
from .db import Database
from .fundamentals import FUNDAMENTALS, UNITS as FUND_UNITS, load_fundamentals, magic_ranks
from .timeframes import IST

HISTORY_DAYS = 420          # ~ 1 year of trading days plus warm-up for the 200-day average
MIN_BARS = 20               # fewer daily bars than this and a symbol is not screened
STALE_DAYS = 7              # skip symbols whose last bar is older than this vs the newest bar anywhere
BATCH = 200
KEEP_RUNS_DAYS = 30

# every metric column and its meaning (also what the UI shows in the column picker)
METRICS: dict[str, str] = {
    "close": "Last close",
    "change_pct": "Change vs previous close, %",
    "volume": "Volume",
    "avg_volume_20": "Average volume of the previous 20 days",
    "volume_ratio": "Volume / 20-day average volume",
    "volume_ratio_5d": "Average volume of the last 5 days / average of the 20 before",
    "ema_20": "EMA 20", "ema_50": "EMA 50", "sma_50": "SMA 50", "sma_200": "SMA 200",
    "rsi_14": "RSI 14", "atr_14": "ATR 14", "atr_pct": "ATR 14 as % of close",
    "high_52w": "52-week high", "low_52w": "52-week low",
    "pct_from_52w_high": "% below the 52-week high (0 = at the high)",
    "pct_from_52w_low": "% above the 52-week low",
    "ret_20d": "Return over 20 trading days, %",
    "ret_60d": "Return over 60 trading days, %",
    "ret_120d": "Return over 120 trading days, %",
    "golden_cross": "50-day average crossed above the 200-day within 5 days: 1; crossed below: -1; else 0",
    "supertrend_dir": "Supertrend(10,3) direction: 1 up, -1 down",
    "supertrend_value": "Supertrend(10,3) line",
    "supertrend_52_dir": "Supertrend(5,2) direction: 1 up, -1 down",
    "st_agree": "Supertrend(10,3) and (5,2) agree: 1 both up, -1 both down, 0 mixed",
    "st_agree_fresh": "Both Supertrends started agreeing within the last 3 sessions: 1 up, -1 down, else 0",
    "bars": "Daily bars available",
}
EXTRA_COLUMNS = {"market_cap": "Market cap (from the symbols table)"}
STORED = [*METRICS, *FUNDAMENTALS]                  # every column of terminal_screener_metrics apart from run_date / symbol / asof
FILTERABLE = {**METRICS, **FUNDAMENTALS, **EXTRA_COLUMNS}
UNITS: dict[str, str] = {
    **FUND_UNITS, "close": "price", "change_pct": "pct2", "ret_20d": "pct", "ret_60d": "pct", "ret_120d": "pct", "pct_from_52w_high": "pct", "pct_from_52w_low": "pct",
    "atr_pct": "pct", "volume_ratio": "mult", "volume_ratio_5d": "mult", "rsi_14": "int", "market_cap": "cr", "golden_cross": "int", "supertrend_dir": "int",
    "supertrend_52_dir": "int", "st_agree": "int", "st_agree_fresh": "int",
}

# The field picker's groups, in order (technical first, then fundamentals). A field not listed lands in "Other".
FIELD_GROUPS: dict[str, list[str]] = {
    "Price": ["close", "change_pct", "market_cap"],
    "Trend": ["ema_20", "ema_50", "sma_50", "sma_200", "golden_cross", "supertrend_dir", "supertrend_52_dir", "st_agree", "st_agree_fresh", "supertrend_value"],
    "Momentum": ["rsi_14", "ret_20d", "ret_60d", "ret_120d"],
    "Volume": ["volume", "avg_volume_20", "volume_ratio", "volume_ratio_5d"],
    "Volatility": ["atr_14", "atr_pct"],
    "52-week range": ["high_52w", "low_52w", "pct_from_52w_high", "pct_from_52w_low"],
    "Valuation": ["pe_ratio", "pb_ratio", "earnings_yield_pct", "peg_ratio", "pe_avg_eps", "dividend_yield"],
    "Profitability": ["roce_pct", "roe_pct", "q_opm_pct", "q_opm_change", "magic_rank"],
    "Growth": ["sales_cagr_3y", "profit_cagr_3y", "rev_growth_min_3y"],
    "Quarterly results": ["q_net_profit", "q_revenue_yoy_pct", "q_profit_yoy_pct", "q_revenue_qoq_pct", "q_profit_qoq_pct", "q_growth_streak", "q_turnaround"],
    "Safety": ["interest_cover", "interest_change_pct"],
    "Data": ["bars", "eps_years"],
}
TECHNICAL_GROUPS = {"Price", "Trend", "Momentum", "Volume", "Volatility", "52-week range", "Data"}


def group_of(field: str) -> str:
    return next((g for g, fields in FIELD_GROUPS.items() if field in fields), "Other")


# One-click building blocks for a custom screen (each adds its filters; mix freely with others).
QUICK: list[dict[str, Any]] = [
    {"label": "Close above SMA 200", "kind": "technical", "filters": [{"field": "close", "op": ">", "value": {"field": "sma_200"}}]},
    {"label": "Close above EMA 20", "kind": "technical", "filters": [{"field": "close", "op": ">", "value": {"field": "ema_20"}}]},
    {"label": "SMA 50 above SMA 200", "kind": "technical", "filters": [{"field": "sma_50", "op": ">", "value": {"field": "sma_200"}}]},
    {"label": "Golden cross (last 5 days)", "kind": "technical", "filters": [{"field": "golden_cross", "op": "=", "value": 1}]},
    {"label": "Death cross (last 5 days)", "kind": "technical", "filters": [{"field": "golden_cross", "op": "=", "value": -1}]},
    {"label": "Supertrend 10,3 up", "kind": "technical", "filters": [{"field": "supertrend_dir", "op": "=", "value": 1}]},
    {"label": "Both Supertrends up", "kind": "technical", "filters": [{"field": "st_agree", "op": "=", "value": 1}]},
    {"label": "Both Supertrends down", "kind": "technical", "filters": [{"field": "st_agree", "op": "=", "value": -1}]},
    {"label": "Supertrends just turned up together", "kind": "technical", "filters": [{"field": "st_agree_fresh", "op": "=", "value": 1}]},
    {"label": "RSI 40 to 60", "kind": "technical", "filters": [{"field": "rsi_14", "op": ">=", "value": 40}, {"field": "rsi_14", "op": "<=", "value": 60}]},
    {"label": "RSI oversold (< 30)", "kind": "technical", "filters": [{"field": "rsi_14", "op": "<", "value": 30}]},
    {"label": "Volume above 1.5× average", "kind": "technical", "filters": [{"field": "volume_ratio", "op": ">", "value": 1.5}]},
    {"label": "Within 5% of 52-week high", "kind": "technical", "filters": [{"field": "pct_from_52w_high", "op": ">=", "value": -5}]},
    {"label": "Price ₹10 to ₹100", "kind": "technical", "filters": [{"field": "close", "op": ">=", "value": 10}, {"field": "close", "op": "<=", "value": 100}]},
    {"label": "Market cap above ₹1,000 Cr", "kind": "fundamental", "filters": [{"field": "market_cap", "op": ">", "value": 1000}]},
    {"label": "ROCE above 15%", "kind": "fundamental", "filters": [{"field": "roce_pct", "op": ">", "value": 15}]},
    {"label": "ROE above 15%", "kind": "fundamental", "filters": [{"field": "roe_pct", "op": ">", "value": 15}]},
    {"label": "P/E below 25", "kind": "fundamental", "filters": [{"field": "pe_ratio", "op": ">", "value": 0}, {"field": "pe_ratio", "op": "<", "value": 25}]},
    {"label": "Quarterly profit growth above 20% yoy", "kind": "fundamental", "filters": [{"field": "q_profit_yoy_pct", "op": ">", "value": 20}]},
    {"label": "Sales growing 3 years (min > 10%)", "kind": "fundamental", "filters": [{"field": "rev_growth_min_3y", "op": ">", "value": 10}]},
    {"label": "Interest cover above 3", "kind": "fundamental", "filters": [{"field": "interest_cover", "op": ">", "value": 3}]},
]


def list_saved(db: Database) -> list[dict]:
    rows = db.rows("SELECT id, name, filters, sort, descending FROM terminal_saved_screens ORDER BY name")
    return [dict(r) for r in rows]


def save_screen(db: Database, name: str, filters: Sequence[Mapping[str, Any]], sort: str, descending: bool) -> dict:
    """Validate (by running it once on nothing) and store a named custom screen; the same name replaces it."""
    from .db import jdump, new_id

    name = (name or "").strip()[:60]
    if not name:
        raise ScreenError("a saved screen needs a name")
    run_screen(db, filters, sort, descending, 1, run_date=date(1900, 1, 1))       # rejects bad filters
    sid = db.scalar("SELECT id FROM terminal_saved_screens WHERE lower(name) = lower(:n)", n=name) or new_id()
    db.execute(
        """INSERT INTO terminal_saved_screens (id, name, filters, sort, descending) VALUES (:i, :n, CAST(:f AS JSONB), :s, :d)
           ON CONFLICT (id) DO UPDATE SET filters = EXCLUDED.filters, sort = EXCLUDED.sort, descending = EXCLUDED.descending, updated_at = now()""",
        i=sid, n=name, f=jdump(list(filters)), s=sort, d=bool(descending),
    )
    return {"id": sid, "name": name, "filters": list(filters), "sort": sort, "descending": bool(descending)}


def delete_saved(db: Database, screen_id: str) -> bool:
    return bool(db.execute("DELETE FROM terminal_saved_screens WHERE id = :i", i=screen_id))
OPS = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "=": "=", "!=": "<>"}


class ScreenError(ValueError):
    pass


# ------------------------------------------------------------------ computing
def _last(arr: np.ndarray) -> Optional[float]:
    if len(arr) == 0:
        return None
    v = float(arr[-1])
    return None if math.isnan(v) else v


def _pct(new: float, old: float) -> Optional[float]:
    return None if old == 0 else (new / old - 1.0) * 100.0


def compute_metrics(bars: Sequence[Candle]) -> Optional[dict[str, Any]]:
    """Metrics as of the LAST daily bar, or None if there is not enough history."""
    if len(bars) < MIN_BARS:
        return None
    o = np.array([b.open for b in bars])
    h = np.array([b.high for b in bars])
    l = np.array([b.low for b in bars])
    c = np.array([b.close for b in bars])
    v = np.array([b.volume for b in bars], dtype=float)
    n = len(c)
    close = float(c[-1])

    prev20 = v[-21:-1]
    avg_vol = float(prev20.mean()) if len(prev20) == 20 else None
    atr14 = _last(ind.atr(h, l, c, 14))
    window_h, window_l = h[-252:], l[-252:]
    high52, low52 = float(window_h.max()), float(window_l.min())
    st = ind.supertrend(h, l, c, 10, 3.0)
    vol5 = v[-5:]
    prior20 = v[-25:-5]
    s50, s200 = ind.sma(c, 50), ind.sma(c, 200)
    gap = s50 - s200
    cross = 0
    for k in range(max(1, n - 5), n):                      # the last 5 bars
        if math.isnan(gap[k]) or math.isnan(gap[k - 1]):
            continue
        if gap[k - 1] <= 0 < gap[k]:
            cross = 1
        elif gap[k - 1] >= 0 > gap[k]:
            cross = -1
    st_dir = _last(st["direction"])
    st52 = ind.supertrend(h, l, c, 5, 2.0)["direction"]
    agree = np.where((st["direction"] > 0) & (st52 > 0), 1, np.where((st["direction"] < 0) & (st52 < 0), -1, 0))
    fresh = int(agree[-1]) if n >= 4 and agree[-1] != 0 and any(agree[-4:-1] != agree[-1]) else 0

    def ret(days: int) -> Optional[float]:
        return _pct(close, float(c[-1 - days])) if n > days else None

    return {
        "close": close,
        "change_pct": _pct(close, float(c[-2])),
        "volume": int(v[-1]),
        "avg_volume_20": avg_vol,
        "volume_ratio": (float(v[-1]) / avg_vol) if avg_vol else None,
        "volume_ratio_5d": (float(vol5.mean()) / float(prior20.mean())) if len(prior20) == 20 and prior20.mean() > 0 else None,
        "golden_cross": cross,
        "ema_20": _last(ind.ema(c, 20)),
        "ema_50": _last(ind.ema(c, 50)),
        "sma_50": _last(ind.sma(c, 50)),
        "sma_200": _last(ind.sma(c, 200)),
        "rsi_14": _last(ind.rsi(c, 14)),
        "atr_14": atr14,
        "atr_pct": (atr14 / close * 100.0) if atr14 is not None and close else None,
        "high_52w": high52,
        "low_52w": low52,
        "pct_from_52w_high": _pct(close, high52),
        "pct_from_52w_low": _pct(close, low52),
        "ret_20d": ret(20),
        "ret_60d": ret(60),
        "ret_120d": ret(120),
        "supertrend_dir": int(st_dir) if st_dir is not None else None,
        "supertrend_value": _last(st["line"]),
        "supertrend_52_dir": int(st52[-1]) if not math.isnan(st52[-1]) else None,
        "st_agree": int(agree[-1]),
        "st_agree_fresh": fresh,
        "bars": n,
        "asof": bars[-1].time,
    }


def add_fundamentals(db: Database, rows: list[dict]) -> None:
    """Put the financial columns on each scanned row (blank for a stock with no financials), plus the cross-stock magic-formula rank."""
    names = [r["symbol"] for r in rows]
    funds = load_fundamentals(db, names, {r["symbol"]: r["close"] for r in rows})
    sectors = {x["tradingsymbol"]: x["sector"] for x in db.rows("SELECT tradingsymbol, sector FROM symbols WHERE tradingsymbol = ANY(:s)", s=names)} if db.table_exists("symbols") else {}
    ranks = magic_ranks(funds, sectors)
    for r in rows:
        f = funds.get(r["symbol"])
        r.update(f if f else {k: None for k in FUNDAMENTALS})
        r["magic_rank"] = ranks.get(r["symbol"])


def run_scan(db: Database, run_date: Optional[date] = None, symbols: Optional[Iterable[str]] = None) -> dict:
    """Compute metrics for every symbol in `ohlcv_1d` and store them for `run_date`
    (default today, IST). Re-running the same day replaces that day's rows."""
    if not db.table_exists("ohlcv_1d"):
        return {"run_date": None, "scanned": 0, "stored": 0, "skipped": 0}
    run_date = run_date or datetime.now(IST).date()
    newest = db.scalar("SELECT max(date) FROM ohlcv_1d")
    if newest is None:
        return {"run_date": run_date.isoformat(), "scanned": 0, "stored": 0, "skipped": 0}
    since = newest - timedelta(days=HISTORY_DAYS)
    wanted = sorted(set(symbols)) if symbols is not None else [
        r["tradingsymbol"] for r in db.rows("SELECT DISTINCT tradingsymbol FROM ohlcv_1d WHERE date >= :d ORDER BY 1", d=since)
    ]

    stored = skipped = 0
    out_rows: list[dict] = []
    for i in range(0, len(wanted), BATCH):
        chunk = wanted[i : i + BATCH]
        rows = db.rows(
            "SELECT tradingsymbol, date, open, high, low, close, volume FROM ohlcv_1d "
            "WHERE tradingsymbol = ANY(:s) AND date >= :d ORDER BY tradingsymbol, date",
            s=chunk, d=since,
        )
        grouped: dict[str, list[Candle]] = {}
        for r in rows:
            grouped.setdefault(r["tradingsymbol"], []).append(
                Candle(datetime.combine(r["date"], datetime.min.time()), r["open"], r["high"], r["low"], r["close"], r["volume"])
            )
        for sym in chunk:
            bars = grouped.get(sym, [])
            m = compute_metrics(bars)
            if m is None or bars[-1].time.date() < newest - timedelta(days=STALE_DAYS):
                skipped += 1
                continue
            m["asof"] = bars[-1].time.date()
            out_rows.append({"run_date": run_date, "symbol": sym, **m})

    add_fundamentals(db, out_rows)
    with db.tx() as conn:
        conn.execute(text("DELETE FROM terminal_screener_metrics WHERE run_date = :d"), {"d": run_date})
        if out_rows:
            cols = ["run_date", "symbol", "asof", *STORED]
            conn.execute(
                text(f"INSERT INTO terminal_screener_metrics ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
                [{c: r.get(c) for c in cols} for r in out_rows],
            )
        conn.execute(text("DELETE FROM terminal_screener_metrics WHERE run_date < :d"), {"d": run_date - timedelta(days=KEEP_RUNS_DAYS)})
    stored = len(out_rows)
    return {"run_date": run_date.isoformat(), "scanned": len(wanted), "stored": stored, "skipped": skipped}


# ------------------------------------------------------------------- querying
def latest_run(db: Database) -> Optional[date]:
    return db.scalar("SELECT max(run_date) FROM terminal_screener_metrics")


def _col(field: str) -> str:
    # symbols.market_cap is in rupees (data-store: issued shares x price); the screen speaks crore
    return "s.market_cap / 1e7" if field == "market_cap" else f"m.{field}"


def _operand(raw: Any, params: dict, key: str) -> str:
    """A filter's right-hand side: a number, or {"field": "sma_200"} to compare two columns."""
    if isinstance(raw, Mapping):
        field = raw.get("field")
        if field not in FILTERABLE:
            raise ScreenError(f"unknown field {field!r}")
        return _col(field)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ScreenError(f"filter value must be a number or {{field: ...}}, got {raw!r}")
    params[key] = raw
    return f":{key}"


def run_screen(
    db: Database,
    filters: Sequence[Mapping[str, Any]] = (),
    sort: str = "volume_ratio",
    descending: bool = True,
    limit: int = 100,
    run_date: Optional[date] = None,
) -> dict:
    """Apply filters to the latest scan. Each filter is {"field", "op", "value"} where
    value is a number or {"field": other_column}; filters are AND-ed."""
    if sort not in FILTERABLE and sort != "symbol":
        raise ScreenError(f"cannot sort by {sort!r}")
    if len(filters) > 25:
        raise ScreenError("too many filters (max 25)")

    params: dict[str, Any] = {"n": max(1, min(int(limit), 1000))}
    where = ["m.run_date = :run"]
    for i, f in enumerate(filters):
        field, op = f.get("field"), f.get("op")
        if field not in FILTERABLE:
            raise ScreenError(f"unknown field {field!r}")
        if op not in OPS:
            raise ScreenError(f"unknown operator {op!r}; use one of {', '.join(OPS)}")
        where.append(f"{_col(field)} {OPS[op]} {_operand(f.get('value'), params, f'v{i}')}")

    run = run_date or latest_run(db)       # validated first, so a bad screen is an error even before any scan exists
    if run is None:
        return {"run_date": None, "count": 0, "rows": []}
    params["run"] = run
    sort_col = "m.symbol" if sort == "symbol" else _col(sort)
    cols = ", ".join(f"m.{c}" for c in ["symbol", "asof", *STORED])
    # `s` carries name/sector/market cap from the data-store; an empty stand-in keeps the
    # query identical when that table is not installed.
    join = (
        "LEFT JOIN symbols s ON s.tradingsymbol = m.symbol" if db.table_exists("symbols") else
        "LEFT JOIN (SELECT NULL::text AS tradingsymbol, NULL::text AS name, NULL::text AS sector, "
        "NULL::float8 AS market_cap WHERE false) s ON s.tradingsymbol = m.symbol"
    )
    extra = "s.name AS name, s.sector AS sector, s.market_cap / 1e7 AS market_cap"
    sql = (
        f"SELECT {cols}, {extra} FROM terminal_screener_metrics m {join} WHERE {' AND '.join(where)} "
        f"ORDER BY {sort_col} {'DESC' if descending else 'ASC'} NULLS LAST, m.symbol LIMIT :n"
    )
    rows = db.rows(sql, **params)
    total = db.scalar(f"SELECT count(*) FROM terminal_screener_metrics m {join} WHERE {' AND '.join(where)}", **{k: v for k, v in params.items() if k != "n"})
    for r in rows:
        r["asof"] = r["asof"].isoformat() if r["asof"] else None
    return {"run_date": run.isoformat(), "count": int(total or 0), "rows": rows}


# column headings for the results table (the long labels are for the filter picker)
SHORT: dict[str, str] = {
    "close": "Close", "change_pct": "Chg %", "volume_ratio": "Vol ×avg", "volume_ratio_5d": "5d vol ×", "rsi_14": "RSI", "ret_20d": "20d %", "ret_60d": "60d %",
    "pct_from_52w_high": "vs 52w high", "pct_from_52w_low": "vs 52w low", "market_cap": "Mkt cap (Cr)", "golden_cross": "Cross",
    "pe_ratio": "P/E", "pb_ratio": "P/B", "roce_pct": "ROCE %", "roe_pct": "ROE %", "dividend_yield": "Div yield %", "earnings_yield_pct": "Earn. yield %", "peg_ratio": "PEG",
    "q_net_profit": "Qtr profit (Cr)", "q_opm_pct": "Qtr OPM %", "q_opm_change": "OPM Δ yoy", "q_revenue_yoy_pct": "Rev yoy %", "q_profit_yoy_pct": "Profit yoy %",
    "q_revenue_qoq_pct": "Rev qoq %", "q_profit_qoq_pct": "Profit qoq %", "q_growth_streak": "Growth streak", "q_turnaround": "Turnaround",
    "sales_cagr_3y": "Sales 3y %", "profit_cagr_3y": "Profit 3y %", "rev_growth_min_3y": "Min rev gr. %", "interest_cover": "Int. cover", "interest_change_pct": "Interest Δ %",
    "pe_avg_eps": "P/avg EPS", "eps_years": "EPS yrs", "magic_rank": "Magic rank",
}


def f(field: str, op: str, value: Any) -> dict:
    return {"field": field, "op": op, "value": value}


TECH = ["close", "change_pct", "volume_ratio", "rsi_14", "ret_20d", "ret_60d", "pct_from_52w_high", "market_cap"]
VALUE = ["close", "market_cap", "pe_ratio", "pb_ratio", "roce_pct", "roe_pct", "dividend_yield", "profit_cagr_3y"]
RESULTS = ["close", "market_cap", "q_net_profit", "q_revenue_yoy_pct", "q_profit_yoy_pct", "q_opm_change", "q_growth_streak", "pe_ratio"]
# `group` sorts the chips in the UI, `show` is the columns the results table opens with. Fundamental screens need the
# data-store's financials (see the Data screen); without them they simply find nothing.
PRESETS: dict[str, dict[str, Any]] = {
    "uptrend": {
        "group": "Price and volume", "show": TECH, "label": "Uptrend",
        "description": "Close above the 200-day average, 50-day above 200-day, and above EMA 20.",
        "filters": [f("close", ">", {"field": "sma_200"}), f("sma_50", ">", {"field": "sma_200"}), f("close", ">", {"field": "ema_20"})],
        "sort": "ret_60d", "descending": True,
    },
    "near_52w_high": {
        "group": "Price and volume", "show": TECH, "label": "Near 52-week high",
        "description": "Within 3% of the 52-week high on at least average volume.",
        "filters": [f("pct_from_52w_high", ">=", -3), f("volume_ratio", ">=", 1)],
        "sort": "pct_from_52w_high", "descending": True,
    },
    "darvas": {
        "group": "Price and volume", "show": TECH, "label": "Darvas scan",
        "description": "Within 10% of the 52-week high, at least double its 52-week low, on above-average volume.",
        "filters": [f("pct_from_52w_high", ">=", -10), f("pct_from_52w_low", ">=", 100), f("volume_ratio", ">=", 1.2)],
        "sort": "pct_from_52w_low", "descending": True,
    },
    "volume_breakout": {
        "group": "Price and volume", "show": TECH, "label": "Volume breakout",
        "description": "Up more than 2% today on at least twice the usual volume.",
        "filters": [f("change_pct", ">", 2), f("volume_ratio", ">=", 2)],
        "sort": "volume_ratio", "descending": True,
    },
    "volume_week": {
        "group": "Price and volume", "show": TECH, "label": "Price volume action",
        "description": "The last 5 days' volume is at least 3 times the 20 days before, and the price is up over the week.",
        "filters": [f("volume_ratio_5d", ">=", 3), f("ret_20d", ">", 0), f("change_pct", ">", 0)],
        "sort": "volume_ratio_5d", "descending": True,
    },
    "golden_cross": {
        "group": "Price and volume", "show": TECH, "label": "Golden crossover",
        "description": "The 50-day average crossed above the 200-day average in the last 5 trading days.",
        "filters": [f("golden_cross", "=", 1)],
        "sort": "ret_20d", "descending": True,
    },
    "death_cross": {
        "group": "Price and volume", "show": TECH, "label": "Bearish crossover",
        "description": "The 50-day average crossed below the 200-day average in the last 5 trading days.",
        "filters": [f("golden_cross", "=", -1)],
        "sort": "ret_20d", "descending": False,
    },
    "oversold": {
        "group": "Price and volume", "show": TECH, "label": "Oversold",
        "description": "RSI(14) at or below 30.",
        "filters": [f("rsi_14", "<=", 30)],
        "sort": "rsi_14", "descending": False,
    },
    "supertrend_up": {
        "group": "Price and volume", "show": TECH, "label": "Supertrend up",
        "description": "Supertrend(10,3) is in an uptrend and price is above it.",
        "filters": [f("supertrend_dir", "=", 1), f("close", ">", {"field": "supertrend_value"})],
        "sort": "ret_20d", "descending": True,
    },
    "strong_quarter": {
        "group": "Quarterly results", "show": RESULTS, "label": "Strong latest quarter",
        "description": "Revenue up 20%+ and profit up 25%+ on a year ago, with the operating margin not lower.",
        "filters": [f("q_revenue_yoy_pct", ">=", 20), f("q_profit_yoy_pct", ">=", 25), f("q_opm_change", ">=", 0)],
        "sort": "q_profit_yoy_pct", "descending": True,
    },
    "quarterly_growers": {
        "group": "Quarterly results", "show": RESULTS, "label": "Quarterly growers",
        "description": "Revenue and profit above the year-ago quarter for at least 4 quarters in a row.",
        "filters": [f("q_growth_streak", ">=", 4)],
        "sort": "q_profit_yoy_pct", "descending": True,
    },
    "loss_to_profit": {
        "group": "Quarterly results", "show": RESULTS, "label": "Loss to profit",
        "description": "The latest quarter made a profit after a loss in the quarter before.",
        "filters": [f("q_turnaround", "=", 1)],
        "sort": "q_net_profit", "descending": True,
    },
    "magic_formula": {
        "group": "Value and quality", "show": VALUE, "label": "Magic formula",
        "description": "Ranked on return on capital (ROCE) and earnings yield together, banks and finance left out. Earnings yield is 100 / P/E, not Greenblatt's EBIT / enterprise value, which needs debt data we do not have.",
        "filters": [f("magic_rank", "<=", 50), f("market_cap", ">=", 500)],
        "sort": "magic_rank", "descending": False,
    },
    "growth_reasonable": {
        "group": "Value and quality", "show": VALUE, "label": "Growth at a fair price",
        "description": "Profit growing 15%+ a year for 3 years, PEG under 1.5, ROCE above 15%.",
        "filters": [f("profit_cagr_3y", ">=", 15), f("peg_ratio", "<=", 1.5), f("roce_pct", ">=", 15)],
        "sort": "peg_ratio", "descending": False,
    },
    "coffee_can": {
        "group": "Value and quality", "show": VALUE, "label": "Coffee can (3-year)",
        "description": "Revenue grew at least 10% in each of the last 3 years, with ROCE above 15%. The book asks for 10 years; 3 is what the data on file covers.",
        "filters": [f("rev_growth_min_3y", ">=", 10), f("roce_pct", ">=", 15), f("profit_cagr_3y", ">", 0)],
        "sort": "roce_pct", "descending": True,
    },
    "high_dividend": {
        "group": "Value and quality", "show": VALUE, "label": "High dividend yield",
        "description": "Yield of 3% or more from profitable companies worth at least 500 Cr.",
        "filters": [f("dividend_yield", ">=", 3), f("pe_ratio", ">", 0), f("market_cap", ">=", 500)],
        "sort": "dividend_yield", "descending": True,
    },
    "below_book": {
        "group": "Value and quality", "show": VALUE, "label": "Near or below book value",
        "description": "Price at or below book value while earning an ROE of 10% or more.",
        "filters": [f("pb_ratio", ">", 0), f("pb_ratio", "<=", 1), f("roe_pct", ">=", 10)],
        "sort": "pb_ratio", "descending": False,
    },
    "cheap_on_average_earnings": {
        "group": "Value and quality", "show": VALUE, "label": "Cheap on average earnings",
        "description": "Price is at most 15 times the average yearly EPS of at least 5 years on file.",
        "filters": [f("eps_years", ">=", 5), f("pe_avg_eps", ">", 0), f("pe_avg_eps", "<=", 15)],
        "sort": "pe_avg_eps", "descending": False,
    },
    "interest_falling": {
        "group": "Value and quality", "show": VALUE, "label": "Interest burden falling",
        "description": "Interest paid is down 10%+ on last year and covered at least 4 times by operating profit. A stand-in for debt reduction: debt itself is not on file.",
        "filters": [f("interest_change_pct", "<=", -10), f("interest_cover", ">=", 4)],
        "sort": "interest_change_pct", "descending": False,
    },
    "quality_in_uptrend": {
        "group": "Value and quality", "show": VALUE, "label": "Quality in an uptrend",
        "description": "ROCE 15%+, profit growing 10%+ a year, and the price above its 200-day average: financials and chart together.",
        "filters": [f("roce_pct", ">=", 15), f("profit_cagr_3y", ">=", 10), f("close", ">", {"field": "sma_200"})],
        "sort": "ret_60d", "descending": True,
    },
}


def run_preset(db: Database, name: str, limit: int = 100) -> dict:
    if name not in PRESETS:
        raise ScreenError(f"unknown preset {name!r}; expected one of {', '.join(PRESETS)}")
    p = PRESETS[name]
    result = run_screen(db, p["filters"], p["sort"], p["descending"], limit)
    return {**result, "preset": name}
