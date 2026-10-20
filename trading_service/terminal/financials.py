"""Financial data for the panel at the bottom of the chart.

Reads what the data-store already scrapes: `stock_financials` (screener.in quarterly
and annual results) and `result_announcements` (NSE result dates). Both are optional -
a symbol with no data simply returns empty lists so the UI can hide the panel.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from .db import Database
from .timeframes import IST

QUARTERS = 8
YEARS = 5

FIELDS = [
    "period", "period_end_date", "revenue", "expenses", "operating_profit", "opm_pct", "other_income",
    "interest", "depreciation", "profit_before_tax", "tax", "net_profit", "npm_pct", "eps",
    "revenue_growth_pct", "profit_growth_pct",
]
# SEBI: quarterly results within 45 days of quarter end, the March quarter within 60. Used only when no
# announcement date is on file, and then as the LATEST day a result can come out, so a chart never
# "knows" a result before the market did.
DEADLINE_DAYS = 45
Q4_DEADLINE_DAYS = 60
MATCH_WINDOW_DAYS = 100     # an announcement belongs to the newest quarter that ended within this many days before it
TTM_QUARTERS = 4
MAX_QUARTER_GAP_DAYS = 100  # four quarters must be consecutive; a gap means a missing quarter and no TTM
HISTORY_QUARTERS = 24
RATIOS = ["pe_ratio", "pb_ratio", "market_cap_cr", "dividend_yield", "roce_pct", "roe_pct"]


def nse_name(symbol: str) -> str:
    """'NSE:SBIN-EQ' -> 'SBIN' (the key result_announcements uses)."""
    base = symbol.split(":", 1)[1] if ":" in symbol else symbol
    return base[:-3] if base.endswith("-EQ") else base


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _rows(db: Database, symbol: str, kind: str, limit: int) -> list[dict]:
    cols = ", ".join(FIELDS)
    rows = db.rows(
        f"SELECT {cols} FROM stock_financials WHERE tradingsymbol = :s AND result_type = :k "
        "ORDER BY period_end_date DESC NULLS LAST, id DESC LIMIT :n",
        s=symbol, k=kind, n=limit,
    )
    return [{k: _plain(v) for k, v in r.items()} for r in reversed(rows)]   # oldest first, like a results table


def get_financials(db: Database, symbol: str) -> dict:
    empty = {"symbol": symbol, "available": False, "company": "", "quarterly": [], "annual": [], "ratios": {}, "announcements": [],
             "events": [], "eps_ttm": []}
    if not db.table_exists("stock_financials"):
        return empty
    quarterly = _rows(db, symbol, "quarterly", QUARTERS)
    annual = _rows(db, symbol, "annual", YEARS)
    announcements = _announcements(db, symbol)
    if not (quarterly or annual or announcements):
        return empty

    # Ratios (P/E, ROE...) are stored on result rows; some rows (e.g. an annual one) may lack them, so take
    # each ratio from the newest row that has it rather than from whichever row was scraped last.
    recent = db.rows(
        f"SELECT company_name, {', '.join(RATIOS)} FROM stock_financials WHERE tradingsymbol = :s "
        "ORDER BY scraped_at DESC, id DESC LIMIT 12",
        s=symbol,
    )
    ratios = {k: next((r[k] for r in recent if r[k] is not None), None) for k in RATIOS}
    company = next((r["company_name"] for r in recent if r["company_name"]), "")
    return {
        "symbol": symbol,
        "available": bool(quarterly or annual),
        "company": company,
        "quarterly": quarterly,
        "annual": annual,
        "ratios": ratios,
        "announcements": announcements,
        **fundamentals(db, symbol),
    }


def _announcements(db: Database, symbol: str, limit: int = 5) -> list[dict]:
    if not db.table_exists("result_announcements"):
        return []
    rows = db.rows(
        "SELECT result_date, quarter FROM result_announcements WHERE nse_symbol = :n "
        "ORDER BY result_date DESC NULLS LAST LIMIT :k",
        n=nse_name(symbol), k=limit,
    )
    return [{"result_date": _plain(r["result_date"]), "quarter": r["quarter"] or ""} for r in rows]


def recent_results(db: Database, days: int = 7, limit: int = 100) -> list[dict]:
    """Companies that announced results recently, with their latest net profit and growth.
    Feeds the screener's 'fresh results' view."""
    if not (db.table_exists("result_announcements") and db.table_exists("stock_financials")):
        return []
    rows = db.rows(
        """
        SELECT a.nse_symbol, a.company_name, a.result_date, a.quarter,
               f.tradingsymbol, f.revenue, f.net_profit, f.revenue_growth_pct, f.profit_growth_pct, f.opm_pct, f.eps
          FROM result_announcements a
          LEFT JOIN LATERAL (
                SELECT * FROM stock_financials s
                 WHERE s.nse_symbol = a.nse_symbol AND s.result_type = 'quarterly'
                 ORDER BY s.period_end_date DESC NULLS LAST, s.id DESC LIMIT 1
          ) f ON TRUE
         WHERE a.result_date >= current_date - CAST(:d AS INTEGER)
         ORDER BY a.result_date DESC, a.nse_symbol
         LIMIT :n
        """,
        d=days, n=max(1, min(limit, 500)),
    )
    return [
        {
            "symbol": r["tradingsymbol"] or f"NSE:{r['nse_symbol']}-EQ",
            "company": r["company_name"] or "",
            "result_date": _plain(r["result_date"]),
            "quarter": r["quarter"] or "",
            "revenue": r["revenue"], "net_profit": r["net_profit"], "opm_pct": r["opm_pct"], "eps": r["eps"],
            "revenue_growth_pct": r["revenue_growth_pct"], "profit_growth_pct": r["profit_growth_pct"],
        }
        for r in rows
    ]


# ------------------------------------------------------- chart overlays: results + TTM EPS
def _epoch(day: date) -> int:
    """Midnight IST at the start of `day`, as epoch seconds."""
    return int(datetime(day.year, day.month, day.day, tzinfo=IST).timestamp())


def _quarters(db: Database, symbol: str) -> list[dict]:
    """Quarterly results with a period end date, oldest first."""
    rows = db.rows(
        "SELECT period, period_end_date, eps, net_profit, revenue_growth_pct, profit_growth_pct FROM stock_financials "
        "WHERE tradingsymbol = :s AND result_type = 'quarterly' AND period_end_date IS NOT NULL "
        "ORDER BY period_end_date DESC LIMIT :n",
        s=symbol, n=HISTORY_QUARTERS,
    )
    return list(reversed(rows))


def _announced(db: Database, symbol: str, quarters: list[dict]) -> dict[date, date]:
    """period_end_date -> the first day a result for that quarter was announced.

    The table has no usable period key (`quarter` is free text), so each announcement is matched to the
    newest quarter that ended within MATCH_WINDOW_DAYS before it. Earliest wins when a quarter has several."""
    if not db.table_exists("result_announcements"):
        return {}
    dates = [r["result_date"] for r in db.rows(
        "SELECT result_date FROM result_announcements WHERE nse_symbol = :n AND result_date IS NOT NULL ORDER BY result_date",
        n=nse_name(symbol),
    )]
    found: dict[date, date] = {}
    for announced in dates:
        ends = [q["period_end_date"] for q in quarters if 0 < (announced - q["period_end_date"]).days <= MATCH_WINDOW_DAYS]
        if ends:
            found.setdefault(max(ends), announced)
    return found


def _reaction(db: Database, symbol: str, day: date) -> Optional[float]:
    """% move from the close on `day` to the next session's close (results usually land after the bell)."""
    if not db.table_exists("ohlcv_1d"):
        return None
    r = db.row(
        "SELECT (SELECT close FROM ohlcv_1d WHERE tradingsymbol = :s AND date <= :d ORDER BY date DESC LIMIT 1) AS ref, "
        "       (SELECT close FROM ohlcv_1d WHERE tradingsymbol = :s AND date >  :d ORDER BY date LIMIT 1) AS nxt",
        s=symbol, d=day,
    )
    if not r or not r["ref"] or r["nxt"] is None:
        return None
    return round((r["nxt"] / r["ref"] - 1) * 100, 2)


def fundamentals(db: Database, symbol: str) -> dict:
    """What the chart overlays need: a marker per announced result, and TTM EPS as a step series.

    * `events`: one per announcement date on file, with the quarter's growth and the price reaction.
    * `eps_ttm`: trailing-twelve-month EPS, effective from the day AFTER the result came out (announcement
      date when known, else the SEBI deadline - flagged `estimated`). Times are strictly non-decreasing."""
    out: dict = {"events": [], "eps_ttm": []}
    if not db.table_exists("stock_financials"):
        return out
    quarters = _quarters(db, symbol)
    announced = _announced(db, symbol, quarters)

    for q in quarters:
        day = announced.get(q["period_end_date"])
        if day is None:
            continue
        out["events"].append({
            "date": day.isoformat(), "quarter": q["period"], "net_profit": q["net_profit"], "eps": q["eps"],
            "revenue_growth_pct": q["revenue_growth_pct"], "profit_growth_pct": q["profit_growth_pct"],
            "reaction_pct": _reaction(db, symbol, day),
        })
    out["events"].sort(key=lambda e: e["date"])

    for i in range(TTM_QUARTERS - 1, len(quarters)):
        window = quarters[i - TTM_QUARTERS + 1: i + 1]
        if any(q["eps"] is None for q in window):
            continue
        if any((b["period_end_date"] - a["period_end_date"]).days > MAX_QUARTER_GAP_DAYS for a, b in zip(window, window[1:])):
            continue
        last = window[-1]
        day = announced.get(last["period_end_date"])
        estimated = day is None
        if estimated:
            end = last["period_end_date"]
            day = end + timedelta(days=Q4_DEADLINE_DAYS if end.month == 3 else DEADLINE_DAYS)
        out["eps_ttm"].append({"time": _epoch(day + timedelta(days=1)), "value": round(sum(q["eps"] for q in window), 4), "period": last["period"], "estimated": estimated})
    return out
