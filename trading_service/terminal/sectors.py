"""Sector and theme grouping with an in-sector comparison.

`symbols.sector` holds ~185 granular NSE industries; `sector_groups.py` rolls them up into ~25 parent groups
("Banking & Finance", "IT & Technology"...). A *group* is what the UI offers first, a granular *sector* is the drill-down.

`compare` lines every member of a group up on the same numbers - valuation, quality, growth and price momentum - and
ranks each one within the group, so "where does this company stand among its peers" is one table, not twenty tabs.
Ranks are over the WHOLE group even when only the biggest names are returned.
"""
from __future__ import annotations

import statistics
from typing import Any, Optional

from .db import Database
from .sector_groups import PARENT_SECTORS, get_parent_sector

# key, label, higher_is_better (None: shown but not ranked)
COLUMNS: list[tuple[str, str, Optional[bool]]] = [
    ("market_cap_cr", "Market cap (Cr)", True),
    ("pe_ratio", "P/E", False),
    ("pb_ratio", "P/B", False),
    ("roe_pct", "ROE %", True),
    ("roce_pct", "ROCE %", True),
    ("dividend_yield", "Dividend yield %", True),
    ("revenue_growth_pct", "Revenue growth % (latest quarter)", True),
    ("profit_growth_pct", "Profit growth % (latest quarter)", True),
    ("change_pct", "1 day %", True),
    ("ret_20d", "1 month %", True),
    ("ret_60d", "3 months %", True),
    ("ret_120d", "6 months %", True),
    ("pct_from_52w_high", "From 52-week high %", True),
    ("rsi_14", "RSI 14", None),
]
RATIOS = ("market_cap_cr", "pe_ratio", "pb_ratio", "roe_pct", "roce_pct", "dividend_yield")
GROWTH = ("revenue_growth_pct", "profit_growth_pct")
PRICE = ("change_pct", "ret_20d", "ret_60d", "ret_120d", "pct_from_52w_high", "rsi_14")
POSITIVE_ONLY = {"pe_ratio", "pb_ratio"}   # a loss-maker's negative P/E is not "cheap"
DEFAULT_LIMIT = 60
MAX_LIMIT = 300


class UnknownSector(LookupError):
    pass


def _universe(db: Database) -> list[dict]:
    if not db.table_exists("symbols"):
        return []
    rows = db.rows(
        "SELECT tradingsymbol, name, sector FROM symbols "
        "WHERE tradingsymbol LIKE '%-EQ' AND COALESCE(TRIM(sector), '') <> '' ORDER BY tradingsymbol"
    )
    return [{"symbol": r["tradingsymbol"], "name": r["name"] or "", "sector": r["sector"].strip(), "group": get_parent_sector(r["sector"].strip())} for r in rows]


def list_groups(db: Database) -> list[dict]:
    """[{name, count, sectors: [{name, count}]}] in the UI's usual order; empty groups are left out."""
    by_group: dict[str, dict[str, int]] = {}
    for row in _universe(db):
        by_group.setdefault(row["group"], {}).setdefault(row["sector"], 0)
        by_group[row["group"]][row["sector"]] += 1
    order = [g for g in PARENT_SECTORS if g in by_group] + sorted(g for g in by_group if g not in PARENT_SECTORS)
    return [
        {"name": g, "count": sum(by_group[g].values()),
         "sectors": [{"name": s, "count": n} for s, n in sorted(by_group[g].items(), key=lambda kv: (-kv[1], kv[0]))]}
        for g in order
    ]


def _fundamentals(db: Database, symbols: list[str]) -> dict[str, dict]:
    """Each symbol's newest non-null value of every ratio, and its latest quarter's growth."""
    out: dict[str, dict] = {s: {} for s in symbols}
    if not symbols or not db.table_exists("stock_financials"):
        return out
    newest = ", ".join(f"(array_agg({c} ORDER BY scraped_at DESC, id DESC) FILTER (WHERE {c} IS NOT NULL))[1] AS {c}" for c in RATIOS)
    growth = ", ".join(
        f"(array_agg({c} ORDER BY period_end_date DESC NULLS LAST, id DESC) FILTER (WHERE {c} IS NOT NULL AND result_type = 'quarterly'))[1] AS {c}"
        for c in GROWTH
    )
    for r in db.rows(f"SELECT tradingsymbol, {newest}, {growth} FROM stock_financials WHERE tradingsymbol = ANY(:s) GROUP BY tradingsymbol", s=symbols):
        out[r["tradingsymbol"]] = {k: r[k] for k in (*RATIOS, *GROWTH)}
    return out


def _prices(db: Database, symbols: list[str]) -> dict[str, dict]:
    """Momentum numbers from the newest screener run (empty until the daily scan has run)."""
    if not symbols or not db.table_exists("terminal_screener_metrics"):
        return {}
    cols = ", ".join(PRICE)
    rows = db.rows(
        f"SELECT symbol, {cols} FROM terminal_screener_metrics "
        "WHERE run_date = (SELECT max(run_date) FROM terminal_screener_metrics) AND symbol = ANY(:s)",
        s=symbols,
    )
    return {r["symbol"]: {k: r[k] for k in PRICE} for r in rows}


def _rank(rows: list[dict]) -> dict[str, float]:
    """Fill each row's `ranks` (1 = best in the group) and return the group's median per column."""
    medians: dict[str, float] = {}
    for key, _label, higher in COLUMNS:
        usable = [(r["metrics"][key], r) for r in rows if r["metrics"].get(key) is not None and not (key in POSITIVE_ONLY and r["metrics"][key] <= 0)]
        if usable:
            medians[key] = statistics.median(v for v, _ in usable)
        if higher is None:
            continue
        usable.sort(key=lambda p: p[0], reverse=higher)
        place = 0
        previous: Any = object()
        for i, (value, row) in enumerate(usable, start=1):
            if value != previous:      # ties share a rank
                place, previous = i, value
            row["ranks"][key] = place
    return medians


def compare(db: Database, group: Optional[str] = None, sector: Optional[str] = None, limit: int = DEFAULT_LIMIT) -> dict:
    """Every member of a group (or of one granular sector), side by side, biggest first.

    `symbols` lists ALL members so the UI can turn the group into a watchlist; `rows` is capped at `limit`."""
    limit = max(1, min(limit, MAX_LIMIT))
    members = [m for m in _universe(db) if (m["sector"] == sector if sector else m["group"] == group)]
    if not members:
        raise UnknownSector(sector or group or "")
    names = [m["symbol"] for m in members]
    funds, prices = _fundamentals(db, names), _prices(db, names)
    rows = [{**m, "metrics": {**{k: None for k, _, _ in COLUMNS}, **funds.get(m["symbol"], {}), **prices.get(m["symbol"], {})}, "ranks": {}} for m in members]
    medians = _rank(rows)
    rows.sort(key=lambda r: (r["metrics"]["market_cap_cr"] is None, -(r["metrics"]["market_cap_cr"] or 0), r["symbol"]))
    ranked = {k: sum(1 for r in rows if k in r["ranks"]) for k, _, h in COLUMNS if h is not None}
    return {
        "group": group or get_parent_sector(sector or ""), "sector": sector, "count": len(rows),
        "columns": [{"key": k, "label": label, "higher_is_better": h} for k, label, h in COLUMNS],
        "medians": medians, "ranked": ranked,
        "symbols": [r["symbol"] for r in rows[:500]],   # biggest first, so a watchlist made from them starts with the leaders
        "rows": rows[:limit],
    }
