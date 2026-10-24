"""Financial-data columns for the screener.

The daily scan adds these to every stock's row next to the price metrics, so one screen can mix them
("ROCE above 15 AND above its 200-day average"). They are worked out from what the data-store scrapes from
screener.in into `stock_financials`: ratios (P/E, ROCE...), the latest quarters and the annual results.

Only what that table holds can be screened. There is no balance sheet, cash flow or shareholding in it, so screens
that need debt, free cash flow, a Piotroski score or FII holding are not possible yet (see docs).
Money amounts are in crore, as screener.in prints them.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional, Sequence

from .db import Database
from .sector_groups import get_parent_sector

# column -> label (what the screener's column picker shows)
FUNDAMENTALS: dict[str, str] = {
    "pe_ratio": "P/E", "pb_ratio": "Price / book", "roce_pct": "ROCE, %", "roe_pct": "ROE, %", "dividend_yield": "Dividend yield, %",
    "earnings_yield_pct": "Earnings yield (100 / P/E), %", "peg_ratio": "PEG (P/E / 3-year profit CAGR)",
    "q_revenue": "Latest quarter revenue, Cr", "q_npm_pct": "Latest quarter net profit margin, %",
    "q_net_profit": "Latest quarter net profit, Cr", "q_opm_pct": "Latest quarter operating margin, %",
    "q_opm_change": "Operating margin vs a year ago, percentage points",
    "q_revenue_yoy_pct": "Latest quarter revenue vs a year ago, %", "q_profit_yoy_pct": "Latest quarter profit vs a year ago, %",
    "q_revenue_qoq_pct": "Latest quarter revenue vs the quarter before, %", "q_profit_qoq_pct": "Latest quarter profit vs the quarter before, %",
    "q_growth_streak": "Quarters in a row with revenue AND profit above the year-ago quarter",
    "q_turnaround": "1 if the latest quarter made a profit after a loss",
    "sales_cagr_3y": "Revenue growth per year over 3 years, %", "profit_cagr_3y": "Net profit growth per year over 3 years, %",
    "rev_growth_min_3y": "Weakest yearly revenue growth of the last 3 years, %",
    "interest_cover": "Operating profit / interest (latest year)", "interest_change_pct": "Interest paid vs previous year, %",
    "pe_avg_eps": "Price / average yearly EPS over the years on file", "eps_years": "Years of EPS on file",
    "magic_rank": "Magic formula rank (1 = best; ROCE and earnings yield; excludes banks and finance)",
}
# how the UI should write each column; anything not listed is a plain number
UNITS: dict[str, str] = {
    **{k: "pct" for k in ("roce_pct", "roe_pct", "dividend_yield", "earnings_yield_pct", "q_opm_pct", "q_revenue_yoy_pct", "q_profit_yoy_pct", "q_revenue_qoq_pct",
                          "q_profit_qoq_pct", "sales_cagr_3y", "profit_cagr_3y", "rev_growth_min_3y", "interest_change_pct", "q_opm_change", "q_npm_pct")},
    **{k: "x" for k in ("pe_ratio", "pb_ratio", "peg_ratio", "interest_cover", "pe_avg_eps")},
    "q_net_profit": "cr", "q_revenue": "cr", "q_growth_streak": "int", "eps_years": "int", "q_turnaround": "int", "magic_rank": "int",
}
RATIO_COLUMNS = ("pe_ratio", "pb_ratio", "roce_pct", "roe_pct", "dividend_yield")
MAX_STREAK = 8
FINANCIAL_GROUP = "Banking & Finance"


def _num(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def growth(new: Optional[float], old: Optional[float]) -> Optional[float]:
    """% change, undefined when the base is zero or negative (growth off a loss means nothing)."""
    return None if new is None or old is None or old <= 0 else (new / old - 1.0) * 100.0


def cagr(new: Optional[float], old: Optional[float], years: int) -> Optional[float]:
    return None if new is None or old is None or new <= 0 or old <= 0 else ((new / old) ** (1.0 / years) - 1.0) * 100.0


def _days(a: Optional[date], b: Optional[date]) -> Optional[int]:
    return None if a is None or b is None else (a - b).days


def _year_ago(rows: Sequence[dict], i: int) -> Optional[dict]:
    """The quarter about a year before rows[i] (screener.in lists quarters oldest first, so normally rows[i-4])."""
    for j in range(i - 1, -1, -1):
        gap = _days(rows[i].get("period_end_date"), rows[j].get("period_end_date"))
        if gap is not None and 330 <= gap <= 400:
            return rows[j]
        if gap is not None and gap > 400:
            return None
    return None


def _previous(rows: Sequence[dict], i: int) -> Optional[dict]:
    if i == 0:
        return None
    gap = _days(rows[i].get("period_end_date"), rows[i - 1].get("period_end_date"))
    return rows[i - 1] if gap is not None and 60 <= gap <= 130 else None


def compute_fundamentals(quarterly: Sequence[dict], annual: Sequence[dict], ratios: dict, close: Optional[float] = None) -> dict[str, Any]:
    """Every FUNDAMENTALS column for one stock except the cross-sectional magic_rank. Rows are oldest first;
    rows without a period_end_date (screener.in's TTM column) are ignored."""
    out: dict[str, Any] = {k: None for k in FUNDAMENTALS}
    for k in RATIO_COLUMNS:
        out[k] = _num(ratios.get(k))
    pe = out["pe_ratio"]
    out["earnings_yield_pct"] = 100.0 / pe if pe and pe > 0 else None

    q = [r for r in quarterly if r.get("period_end_date")]
    if q:
        i = len(q) - 1
        last, prev, ago = q[i], _previous(q, i), _year_ago(q, i)
        out["q_net_profit"] = _num(last.get("net_profit"))
        out["q_revenue"] = _num(last.get("revenue"))
        out["q_npm_pct"] = out["q_net_profit"] / out["q_revenue"] * 100.0 if out["q_net_profit"] is not None and out["q_revenue"] else None
        out["q_opm_pct"] = _num(last.get("opm_pct"))
        if ago and last.get("opm_pct") is not None and ago.get("opm_pct") is not None:
            out["q_opm_change"] = float(last["opm_pct"]) - float(ago["opm_pct"])
        if ago:
            out["q_revenue_yoy_pct"] = growth(_num(last.get("revenue")), _num(ago.get("revenue")))
            out["q_profit_yoy_pct"] = growth(_num(last.get("net_profit")), _num(ago.get("net_profit")))
        if prev:
            out["q_revenue_qoq_pct"] = growth(_num(last.get("revenue")), _num(prev.get("revenue")))
            out["q_profit_qoq_pct"] = growth(_num(last.get("net_profit")), _num(prev.get("net_profit")))
            if last.get("net_profit") is not None and prev.get("net_profit") is not None:
                out["q_turnaround"] = int(float(last["net_profit"]) > 0 and float(prev["net_profit"]) <= 0)
        streak = 0
        for j in range(i, -1, -1):
            a = _year_ago(q, j)
            if a is None or None in (q[j].get("revenue"), a.get("revenue"), q[j].get("net_profit"), a.get("net_profit")):
                break
            if not (q[j]["revenue"] > a["revenue"] and q[j]["net_profit"] > a["net_profit"]):
                break
            streak += 1
            if streak == MAX_STREAK:
                break
        out["q_growth_streak"] = streak

    a = [r for r in annual if r.get("period_end_date")]
    if len(a) >= 4:
        new, old = a[-1], a[-4]
        out["sales_cagr_3y"] = cagr(_num(new.get("revenue")), _num(old.get("revenue")), 3)
        out["profit_cagr_3y"] = cagr(_num(new.get("net_profit")), _num(old.get("net_profit")), 3)
        yearly = [growth(_num(a[k].get("revenue")), _num(a[k - 1].get("revenue"))) for k in (-3, -2, -1)]
        out["rev_growth_min_3y"] = None if None in yearly else min(yearly)  # type: ignore[type-var]
    if a:
        latest = a[-1]
        op, interest = _num(latest.get("operating_profit")), _num(latest.get("interest"))
        out["interest_cover"] = op / interest if op is not None and interest and interest > 0 else None
        if len(a) >= 2:
            out["interest_change_pct"] = growth(interest, _num(a[-2].get("interest")))
        eps = [float(r["eps"]) for r in a[-10:] if r.get("eps") is not None]
        out["eps_years"] = len(eps)
        mean = sum(eps) / len(eps) if eps else 0.0
        out["pe_avg_eps"] = close / mean if close and len(eps) >= 3 and mean > 0 else None
    growth_pct = out["profit_cagr_3y"]
    out["peg_ratio"] = pe / growth_pct if pe and pe > 0 and growth_pct and growth_pct > 0 else None
    return out


def magic_ranks(rows: dict[str, dict], sectors: dict[str, str]) -> dict[str, int]:
    """Greenblatt's idea: rank stocks on return on capital (ROCE) and on cheapness (earnings yield), add the two ranks, lowest total is best.
    Banks and finance companies are left out, as the formula does."""
    pool = [s for s, r in rows.items() if (r.get("roce_pct") or 0) > 0 and (r.get("earnings_yield_pct") or 0) > 0 and get_parent_sector(sectors.get(s) or "") != FINANCIAL_GROUP]
    by_roce = {s: n for n, s in enumerate(sorted(pool, key=lambda s: -rows[s]["roce_pct"]), 1)}
    by_yield = {s: n for n, s in enumerate(sorted(pool, key=lambda s: -rows[s]["earnings_yield_pct"]), 1)}
    ordered = sorted(pool, key=lambda s: (by_roce[s] + by_yield[s], by_roce[s], s))
    return {s: n for n, s in enumerate(ordered, 1)}


def load_fundamentals(db: Database, symbols: Sequence[str], closes: dict[str, float]) -> dict[str, dict]:
    """Fundamentals for each symbol that has financial data (others are simply absent)."""
    if not symbols or not db.table_exists("stock_financials"):
        return {}
    cols = "tradingsymbol, period_end_date, revenue, operating_profit, opm_pct, interest, net_profit, eps"
    grouped: dict[str, dict[str, list[dict]]] = {}
    for kind, keep in (("quarterly", 12), ("annual", 10)):
        for r in db.rows(
            f"SELECT {cols} FROM stock_financials WHERE tradingsymbol = ANY(:s) AND result_type = :k AND period_end_date IS NOT NULL ORDER BY tradingsymbol, period_end_date",
            s=list(symbols), k=kind,
        ):
            grouped.setdefault(r["tradingsymbol"], {"quarterly": [], "annual": []})[kind].append(r)
    ratios: dict[str, dict] = {}
    for r in db.rows(
        f"SELECT tradingsymbol, {', '.join(RATIO_COLUMNS)} FROM stock_financials WHERE tradingsymbol = ANY(:s) ORDER BY tradingsymbol, scraped_at, id", s=list(symbols),
    ):
        cur = ratios.setdefault(r["tradingsymbol"], {})
        for k in RATIO_COLUMNS:                      # newest non-empty value wins
            if r[k] is not None:
                cur[k] = r[k]
    out = {}
    for s, g in grouped.items():
        out[s] = compute_fundamentals(g["quarterly"][-12:], g["annual"][-10:], ratios.get(s, {}), closes.get(s))
    return out
