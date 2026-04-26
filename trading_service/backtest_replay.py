"""
Dry-run / historical replay on **stored** daily OHLC (`ohlcv_1d` in market DB).

Extensible universes
--------------------
- **explicit** symbol list
- **all_eq**: all `*-EQ` names in `symbols` table (optional cap — large = slow / heavy)
- **sector**: filter `symbols.sector` (exact / contains / ilike)
- **screener-style lists**: pass **daily_universe** `{ "YYYY-MM-DD": ["SYM-EQ", ...] }` from archived runs

Multi-strategy: run isolated **lanes** (each gets a **cash_fraction** × initial capital, or full **initial_cash** each when comparing in **isolated** mode), then aggregate.

**Catalog compare:** set ``compare_catalog_strategies`` to build one lane per row in ``trading_execution_strategies`` (same replay **mode** and **sizing** for all; **capital_per_lane** = ``split`` divides one portfolio, ``isolated`` gives each strategy its own **initial_cash** for apples-to-apples % returns). Optional **daily_universe_by_strategy** maps slug → daily universe for per-algo screener exports.

Visualization helpers
---------------------
- **timeline_compact**: every day — date, mtm_close, daily_pnl, position_count, has_position_change
- **activity_days**: only days with trades or position set changes — for charts / tables
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.exc import ProgrammingError


def _parse_date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _d(d: date) -> str:
    return d.isoformat()


def resolve_universe_symbols(
    market_url: str,
    *,
    universe_type: str = "explicit",
    symbols: Optional[List[str]] = None,
    sector: Optional[str] = None,
    sector_match: str = "contains",
    all_eq_limit: int = 2000,
) -> Tuple[List[str], Optional[str]]:
    """
    Returns (symbol_list, warning_message).
    universe_type: explicit | all_eq | sector
    """
    symbols = symbols or []
    eng = create_engine(market_url, pool_pre_ping=True)
    if universe_type == "explicit":
        out = []
        seen = set()
        for sym in symbols:
            s = str(sym or "").strip()
            if s and s not in seen:
                out.append(s)
                seen.add(s)
        return out, None if out else "explicit list empty (use daily_universe-only is OK)"

    with eng.connect() as conn:
        if universe_type == "all_eq":
            lim = max(1, min(int(all_eq_limit or 2000), 20000))
            rows = conn.execute(
                text("""
                    SELECT tradingsymbol FROM symbols
                    WHERE tradingsymbol LIKE '%-EQ'
                    ORDER BY tradingsymbol
                    LIMIT :lim
                """),
                {"lim": lim},
            ).fetchall()
            out = [r[0] for r in rows if r[0]]
            w = None
            if len(out) >= lim:
                w = f"all_eq capped at {lim} symbols; raise all_eq_limit if needed"
            return out, w

        if universe_type == "sector":
            if not sector or not str(sector).strip():
                return [], "sector required for universe_type=sector"
            sec = str(sector).strip()
            sm = (sector_match or "contains").lower()
            try:
                if sm == "exact":
                    rows = conn.execute(
                        text("""
                            SELECT tradingsymbol FROM symbols
                            WHERE tradingsymbol LIKE '%-EQ'
                              AND sector = :sec
                            ORDER BY tradingsymbol
                        """),
                        {"sec": sec},
                    ).fetchall()
                else:
                    rows = conn.execute(
                        text("""
                            SELECT tradingsymbol FROM symbols
                            WHERE tradingsymbol LIKE '%-EQ'
                              AND sector ILIKE :pat
                            ORDER BY tradingsymbol
                        """),
                        {"pat": f"%{sec}%"},
                    ).fetchall()
            except ProgrammingError as e:
                return [], f"sector query failed (run enrich on symbols table?): {e}"
            out = [r[0] for r in rows if r[0]]
            return out, None if out else f"no symbols for sector filter={sec!r}"

    return [], f"unknown universe_type: {universe_type}"


def _symbol_lookup_candidates(symbol: str) -> List[str]:
    raw = str(symbol or "").strip()
    if not raw:
        return []
    upper = raw.upper()
    suffix = upper.split(":", 1)[1] if ":" in upper else upper
    candidates = [raw, upper, suffix, f"NSE:{suffix}"]
    out: List[str] = []
    seen = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            out.append(candidate)
            seen.add(candidate)
    return out


def resolve_market_symbol_aliases(market_url: str, symbols: List[str]) -> Dict[str, str]:
    """
    Map requested symbols to the stored OHLCV symbol format.

    The UI and screener often use RELIANCE-EQ, while ohlcv_1d may store
    NSE:RELIANCE-EQ. Normalize once before replay so position keys match bars.
    """
    originals = [str(s).strip() for s in symbols if s and str(s).strip()]
    if not originals:
        return {}

    variant_to_originals: Dict[str, List[str]] = {}
    for original in originals:
        for candidate in _symbol_lookup_candidates(original):
            variant_to_originals.setdefault(candidate, []).append(original)

    variants = sorted(variant_to_originals)
    stmt = (
        text("""
            SELECT DISTINCT tradingsymbol
            FROM ohlcv_1d
            WHERE tradingsymbol IN :syms
        """)
        .bindparams(bindparam("syms", expanding=True))
    )
    eng = create_engine(market_url, pool_pre_ping=True)
    with eng.connect() as conn:
        rows = conn.execute(stmt, {"syms": variants}).fetchall()

    available = {r[0] for r in rows if r[0]}
    aliases: Dict[str, str] = {}
    for original in originals:
        for candidate in _symbol_lookup_candidates(original):
            if candidate in available:
                aliases[original] = candidate
                break
    return aliases


def _apply_symbol_aliases(symbols: List[str], aliases: Dict[str, str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for sym in symbols:
        key = str(sym or "").strip()
        if not key:
            continue
        mapped = aliases.get(key, key)
        if mapped not in seen:
            out.append(mapped)
            seen.add(mapped)
    return out


def filter_symbols_by_financials(
    market_url: str,
    symbols: List[str],
    filters: Optional[Dict[str, Any]],
) -> Tuple[List[str], Optional[str], Dict[str, Any]]:
    filters = filters or {}
    max_symbols = int(filters.get("max_symbols") or 0)
    if (not filters.get("enabled") and max_symbols <= 0) or not symbols:
        return symbols, None, {}

    eng = create_engine(market_url, pool_pre_ping=True)
    stmt = (
        text("""
            WITH latest_financials AS (
                SELECT DISTINCT ON (tradingsymbol)
                    tradingsymbol, opm_pct, pe_ratio, pb_ratio, roce_pct, roe_pct,
                    revenue_growth_pct, profit_growth_pct, period_end_date
                FROM stock_financials
                WHERE result_type = 'quarterly'
                  AND tradingsymbol IN :syms
                ORDER BY tradingsymbol, period_end_date DESC NULLS LAST, scraped_at DESC NULLS LAST
            )
            SELECT lf.tradingsymbol, COALESCE(s.sector, '') AS sector,
                   lf.opm_pct, lf.pe_ratio, lf.pb_ratio, lf.roce_pct, lf.roe_pct,
                   lf.revenue_growth_pct, lf.profit_growth_pct
            FROM latest_financials lf
            LEFT JOIN symbols s ON s.tradingsymbol = lf.tradingsymbol
        """)
        .bindparams(bindparam("syms", expanding=True))
    )
    with eng.connect() as conn:
        rows = conn.execute(stmt, {"syms": symbols}).fetchall()

    fin_by_symbol: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        fin_by_symbol[r[0]] = {
            "sector": r[1] or "",
            "opm_pct": r[2],
            "pe_ratio": r[3],
            "pb_ratio": r[4],
            "roce_pct": r[5],
            "roe_pct": r[6],
            "revenue_growth_pct": r[7],
            "profit_growth_pct": r[8],
        }

    def passes_thresholds(sym: str) -> bool:
        f = fin_by_symbol.get(sym)
        if not f:
            return not bool(filters.get("require_financials", True))
        checks = [
            ("min_opm_pct", "opm_pct", ">="),
            ("min_roce_pct", "roce_pct", ">="),
            ("min_roe_pct", "roe_pct", ">="),
            ("min_revenue_growth_pct", "revenue_growth_pct", ">="),
            ("min_profit_growth_pct", "profit_growth_pct", ">="),
            ("max_pe_ratio", "pe_ratio", "<="),
            ("max_pb_ratio", "pb_ratio", "<="),
        ]
        for filter_key, field, op in checks:
            raw = filters.get(filter_key)
            if raw in (None, ""):
                continue
            val = f.get(field)
            if val is None:
                return False
            threshold = float(raw)
            if op == ">=" and float(val) < threshold:
                return False
            if op == "<=" and float(val) > threshold:
                return False
        return True

    kept = [s for s in symbols if passes_thresholds(s)] if filters.get("enabled") else list(symbols)

    def quality_score(sym: str) -> float:
        f = fin_by_symbol.get(sym) or {}
        score = 0.0
        for field in ("opm_pct", "roce_pct", "roe_pct", "revenue_growth_pct", "profit_growth_pct"):
            if f.get(field) is not None:
                score += float(f[field])
        for field in ("pe_ratio", "pb_ratio"):
            if f.get(field) is not None and float(f[field]) > 0:
                score -= float(f[field]) * 0.25
        return score

    top_pct = float(filters.get("sector_top_pct") or 0)
    if filters.get("enabled") and top_pct > 0 and kept:
        top_pct = max(1.0, min(top_pct, 100.0))

        by_sector: Dict[str, List[str]] = {}
        for sym in kept:
            sector = (fin_by_symbol.get(sym) or {}).get("sector") or "Unknown"
            by_sector.setdefault(sector, []).append(sym)
        ranked_keep = set()
        for sector_syms in by_sector.values():
            ordered = sorted(sector_syms, key=quality_score, reverse=True)
            n = max(1, math.ceil(len(ordered) * top_pct / 100.0))
            ranked_keep.update(ordered[:n])
        kept = [s for s in kept if s in ranked_keep]

    if max_symbols > 0 and len(kept) > max_symbols:
        if fin_by_symbol:
            kept = sorted(kept, key=quality_score, reverse=True)[:max_symbols]
        else:
            kept = kept[:max_symbols]

    meta = {
        "input_count": len(symbols),
        "with_financials": len(fin_by_symbol),
        "output_count": len(kept),
        "max_symbols": max_symbols or None,
    }
    warning = f"financial filters kept {len(kept)} of {len(symbols)} symbols ({len(fin_by_symbol)} had financials)"
    return kept, warning, meta


def fetch_ohlcv_1d(
    market_url: str,
    symbols: List[str],
    start: date,
    end: date,
) -> Tuple[Dict[date, Dict[str, Dict[str, float]]], List[date]]:
    if not symbols:
        return {}, []
    eng = create_engine(market_url, pool_pre_ping=True)
    sym_set = list({s.strip() for s in symbols if s.strip()})
    stmt = (
        text("""
            SELECT tradingsymbol, date, open, high, low, close
            FROM ohlcv_1d
            WHERE tradingsymbol IN :syms
              AND date >= :start
              AND date <= :end
            ORDER BY date ASC, tradingsymbol ASC
        """)
        .bindparams(bindparam("syms", expanding=True))
    )
    with eng.connect() as conn:
        rows = conn.execute(stmt, {"syms": sym_set, "start": start, "end": end}).fetchall()

    bars: Dict[date, Dict[str, Dict[str, float]]] = {}
    for r in rows:
        sym, d, o, h, l, c = r[0], r[1], r[2], r[3], r[4], r[5]
        if hasattr(d, "date"):
            d = d.date()
        bars.setdefault(d, {})[sym] = {
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
        }
    return bars, sorted(bars.keys())


def _mtm_cash_and_value(
    positions: Dict[str, float],
    cash: float,
    day_bars: Dict[str, Dict[str, float]],
    price_field: str,
) -> float:
    v = cash
    for sym, qty in positions.items():
        b = day_bars.get(sym)
        if not b:
            continue
        v += qty * float(b[price_field])
    return v


def _apply_commission(notional: float, commission_pct: float) -> float:
    return notional * (commission_pct / 100.0)


def _pos_snapshot(positions: Dict[str, float]) -> Dict[str, float]:
    return {k: round(v, 6) for k, v in sorted(positions.items()) if v and v > 0}


def _pos_detail_snapshot(
    positions: Dict[str, float],
    entry_prices: Dict[str, float],
    day_bars: Dict[str, Dict[str, float]],
) -> Dict[str, Dict[str, float]]:
    details: Dict[str, Dict[str, float]] = {}
    for sym, qty in sorted(positions.items()):
        if not qty or qty <= 0:
            continue
        b = day_bars.get(sym) or {}
        entry = float(entry_prices.get(sym) or 0.0)
        current = float(b.get("close") or 0.0)
        value = qty * current
        cost = qty * entry
        pnl = value - cost
        details[sym] = {
            "qty": round(qty, 6),
            "entry_price": round(entry, 4),
            "current_price": round(current, 4),
            "current_value": round(value, 2),
            "running_pnl_abs": round(pnl, 2),
            "running_pnl_pct": round((pnl / cost) * 100.0, 4) if cost > 0 else 0.0,
        }
    return details


def _build_position_ledgers(timeline: List[Dict[str, Any]], trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    final_positions = (timeline[-1].get("positions_detail") if timeline else {}) or {}
    open_positions = [
        {"symbol": sym, **detail}
        for sym, detail in sorted(
            final_positions.items(),
            key=lambda item: float(item[1].get("running_pnl_abs") or 0.0),
            reverse=True,
        )
    ]
    closed_positions = []
    for t in trades:
        if t.get("side") != "SELL":
            continue
        qty = float(t.get("qty") or 0.0)
        entry_price = t.get("entry_price")
        exit_price = float(t.get("price") or 0.0)
        closed_positions.append({
            "symbol": t.get("symbol"),
            "entry_date": t.get("entry_date"),
            "exit_date": t.get("date"),
            "qty": round(qty, 6),
            "entry_price": entry_price,
            "exit_price": round(exit_price, 4),
            "entry_value": round(qty * float(entry_price or 0.0), 2),
            "exit_value": round(qty * exit_price, 2),
            "closed_pnl_abs": t.get("pnl_abs"),
            "closed_pnl_pct": t.get("pnl_pct"),
            "reason": t.get("reason"),
        })
    closed_positions.sort(key=lambda x: (x.get("exit_date") or "", x.get("symbol") or ""))
    return {
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        "open_count": len(open_positions),
        "closed_count": len(closed_positions),
        "open_running_pnl_abs": round(sum(float(p.get("running_pnl_abs") or 0.0) for p in open_positions), 2),
        "closed_pnl_abs": round(sum(float(p.get("closed_pnl_abs") or 0.0) for p in closed_positions), 2),
    }


def _ema(values: List[float], period: int) -> List[Optional[float]]:
    if period <= 1:
        return [float(v) for v in values]
    out: List[Optional[float]] = []
    k = 2.0 / (period + 1.0)
    ema_val: Optional[float] = None
    for i, v in enumerate(values):
        fv = float(v)
        if ema_val is None:
            ema_val = fv
        else:
            ema_val = fv * k + ema_val * (1.0 - k)
        out.append(ema_val if i + 1 >= period else None)
    return out


def _rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    if len(values) < period + 1:
        return [None for _ in values]
    out: List[Optional[float]] = [None for _ in values]
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(values)):
        chg = float(values[i]) - float(values[i - 1])
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
        if i < period:
            continue
        if i == period:
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
        else:
            avg_gain = ((avg_gain * (period - 1)) + gains[-1]) / period  # type: ignore[name-defined]
            avg_loss = ((avg_loss * (period - 1)) + losses[-1]) / period  # type: ignore[name-defined]
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def build_indicator_series(
    bars: Dict[date, Dict[str, Dict[str, float]]],
    dates: List[date],
    symbols: List[str],
    exit_rules: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Dict[date, Optional[float]]]]:
    rules = exit_rules or {}
    ema_period = int(rules.get("close_below_ema_period") or 0)
    rsi_period = int(rules.get("rsi_period") or 14)
    need_ema = bool(rules.get("exit_on_close_below_ema") and ema_period > 1)
    need_rsi = bool(rules.get("exit_on_rsi_below"))
    if not need_ema and not need_rsi:
        return {}

    result: Dict[str, Dict[str, Dict[date, Optional[float]]]] = {}
    for sym in symbols:
        closes = [float((bars.get(d) or {}).get(sym, {}).get("close", math.nan)) for d in dates]
        valid = [c if not math.isnan(c) else (closes[i - 1] if i else 0.0) for i, c in enumerate(closes)]
        result[sym] = {}
        if need_ema:
            values = _ema(valid, ema_period)
            result[sym][f"ema_{ema_period}"] = {d: values[i] for i, d in enumerate(dates)}
        if need_rsi:
            values = _rsi(valid, rsi_period)
            result[sym][f"rsi_{rsi_period}"] = {d: values[i] for i, d in enumerate(dates)}
    return result


def _build_visualization(
    timeline: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    initial_cash: float,
    strategy_key: str,
) -> Dict[str, Any]:
    trades_by_date: Dict[str, List[dict]] = {}
    for t in trades:
        trades_by_date.setdefault(t["date"], []).append(t)

    prev_mtm = float(initial_cash)
    prev_pos: Dict[str, float] = {}
    timeline_compact: List[Dict[str, Any]] = []
    activity_days: List[Dict[str, Any]] = []

    for row in timeline:
        ds = row["date"]
        mtm = float(row["mtm_at_close"])
        daily_pnl = round(mtm - prev_mtm, 2)
        pos = row.get("positions") or {}
        day_trades = trades_by_date.get(ds, [])
        has_trade = len(day_trades) > 0
        pos_changed = pos != prev_pos
        timeline_compact.append({
            "date": ds,
            "strategy_key": strategy_key,
            "mtm_close": round(mtm, 2),
            "daily_pnl": daily_pnl,
            "position_count": len(pos),
            "has_position_change": pos_changed or has_trade,
        })
        if has_trade or pos_changed:
            entries = [t for t in day_trades if t.get("side") == "BUY"]
            exits = [t for t in day_trades if t.get("side") == "SELL"]
            activity_days.append({
                "date": ds,
                "strategy_key": strategy_key,
                "mtm_close": round(mtm, 2),
                "daily_pnl": daily_pnl,
                "entries": entries,
                "exits": exits,
                "positions_after": pos,
            })
        prev_mtm = mtm
        prev_pos = dict(pos)

    return {
        "timeline_compact": timeline_compact,
        "activity_days": activity_days,
        "activity_day_count": len(activity_days),
    }


def _build_analytics(
    timeline_compact: List[Dict[str, Any]],
    *,
    initial_cash: float,
) -> Dict[str, Any]:
    if not timeline_compact:
        return {
            "timeline_compact": [],
            "trading_days": 0,
            "best_day_pnl": None,
            "worst_day_pnl": None,
            "avg_daily_pnl": None,
            "avg_daily_return_pct": None,
            "volatility_daily_pct": None,
            "annualized_return_pct": None,
            "sharpe_like": None,
            "max_drawdown_abs": None,
            "max_drawdown_pct": None,
            "positive_days": 0,
            "negative_days": 0,
            "flat_days": 0,
            "avg_position_count": None,
            "max_position_count": 0,
            "activity_days": 0,
        }

    peak = float(initial_cash)
    prev_mtm = float(initial_cash)
    returns: List[float] = []
    enriched: List[Dict[str, Any]] = []
    pos_days = 0
    neg_days = 0
    flat_days = 0

    for row in timeline_compact:
        mtm = float(row.get("mtm_close") or 0.0)
        pnl = float(row.get("daily_pnl") or 0.0)
        peak = max(peak, mtm)
        drawdown_abs = peak - mtm
        drawdown_pct = (drawdown_abs / peak * 100.0) if peak > 0 else 0.0
        daily_return = ((mtm / prev_mtm) - 1.0) if prev_mtm > 0 else 0.0

        if pnl > 0:
            pos_days += 1
        elif pnl < 0:
            neg_days += 1
        else:
            flat_days += 1

        returns.append(daily_return)
        enriched.append({
            **row,
            "equity_peak": round(peak, 2),
            "drawdown_abs": round(drawdown_abs, 2),
            "drawdown_pct": round(drawdown_pct, 4),
            "daily_return_pct": round(daily_return * 100.0, 4),
        })
        prev_mtm = mtm

    n = len(enriched)
    final_mtm = float(enriched[-1]["mtm_close"])
    avg_daily_pnl = sum(float(r.get("daily_pnl") or 0.0) for r in enriched) / n
    avg_daily_return = sum(returns) / n if n else 0.0
    volatility = None
    sharpe_like = None
    if n > 1:
        variance = sum((r - avg_daily_return) ** 2 for r in returns) / (n - 1)
        volatility = math.sqrt(variance)
        if volatility > 0:
            sharpe_like = (avg_daily_return / volatility) * math.sqrt(252.0)

    annualized_return = None
    if n > 0 and initial_cash > 0 and final_mtm > 0:
        annualized_return = (final_mtm / float(initial_cash)) ** (252.0 / n) - 1.0

    max_drawdown_abs = max(float(r["drawdown_abs"]) for r in enriched)
    max_drawdown_pct = max(float(r["drawdown_pct"]) for r in enriched)
    avg_position_count = sum(int(r.get("position_count") or 0) for r in enriched) / n
    max_position_count = max(int(r.get("position_count") or 0) for r in enriched)
    activity_days = sum(1 for r in enriched if r.get("has_position_change"))

    return {
        "timeline_compact": enriched,
        "trading_days": n,
        "best_day_pnl": round(max(float(r["daily_pnl"]) for r in enriched), 2),
        "worst_day_pnl": round(min(float(r["daily_pnl"]) for r in enriched), 2),
        "avg_daily_pnl": round(avg_daily_pnl, 2),
        "avg_daily_return_pct": round(avg_daily_return * 100.0, 4),
        "volatility_daily_pct": round(volatility * 100.0, 4) if volatility is not None else None,
        "annualized_return_pct": round(annualized_return * 100.0, 4) if annualized_return is not None else None,
        "sharpe_like": round(sharpe_like, 4) if sharpe_like is not None else None,
        "max_drawdown_abs": round(max_drawdown_abs, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "positive_days": pos_days,
        "negative_days": neg_days,
        "flat_days": flat_days,
        "avg_position_count": round(avg_position_count, 2),
        "max_position_count": max_position_count,
        "activity_days": activity_days,
    }


def lane_from_execution_strategy(
    es: Dict[str, Any],
    *,
    request_universe_type: str,
    symbols: List[str],
    sector: Optional[str],
    sector_match: str,
    all_eq_limit: int,
    daily_universe: Optional[Dict[str, List[str]]],
    daily_universe_by_strategy: Optional[Dict[str, Dict[str, List[str]]]],
    mode: str,
    entry_cash_per_trade: float,
    max_positions: int,
    deploy_fraction: float,
    exit_if_not_in_universe: bool,
    sizing_mode: Optional[str],
    sizing_value: float,
) -> Dict[str, Any]:
    """Map DB execution strategy row → replay lane (universe from catalog + request)."""
    us = (es.get("universe_source") or "screener_filtered").strip()
    if us == "full_universe":
        ut = "all_eq"
    elif us == "pattern_52w":
        ut = "all_eq"
    else:
        ut = request_universe_type

    du: Optional[Dict[str, List[str]]] = None
    if daily_universe_by_strategy:
        raw = daily_universe_by_strategy.get(es.get("slug") or "")
        if isinstance(raw, dict):
            du = {str(k): list(v) if v is not None else [] for k, v in raw.items()}
    if du is None and daily_universe is not None:
        du = {str(k): list(v) if v is not None else [] for k, v in daily_universe.items()}

    return {
        "strategy_key": es["slug"],
        "cash_fraction": 1.0,
        "universe_type": ut,
        "symbols": list(symbols or []),
        "sector": sector,
        "sector_match": sector_match,
        "all_eq_limit": all_eq_limit,
        "daily_universe": du,
        "mode": mode,
        "entry_cash_per_trade": entry_cash_per_trade,
        "max_positions": max_positions,
        "deploy_fraction": deploy_fraction,
        "exit_if_not_in_universe": exit_if_not_in_universe,
        "sizing_mode": sizing_mode,
        "sizing_value": sizing_value,
        "_catalog_meta": {
            "execution_strategy_id": es.get("id"),
            "display_name": es.get("display_name"),
            "universe_source_db": us,
            "replay_universe_type": ut,
        },
    }


def _normalize_sizing_mode(mode: str, sizing_mode: Optional[str]) -> str:
    sm = (sizing_mode or "").strip().lower()
    if sm in ("", "auto"):
        return "fixed_inr" if mode == "sequential_entries" else "equal_weight"
    return sm


def _run_lane(
    *,
    bars: Dict[date, Dict[str, Dict[str, float]]],
    dates: List[date],
    base_symbols: List[str],
    daily_universe: Optional[Dict[str, List[str]]],
    initial_cash: float,
    mode: str,
    entry_cash_per_trade: float,
    max_positions: int,
    deploy_fraction: float,
    commission_pct: float,
    exit_if_not_in_universe: bool,
    strategy_key: str,
    sizing_mode: Optional[str] = None,
    sizing_value: float = 0.0,
    stop_loss_pct: float = 0.0,
    exit_rules: Optional[Dict[str, Any]] = None,
    indicator_series: Optional[Dict[str, Dict[str, Dict[date, Optional[float]]]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    cash = float(initial_cash)
    positions: Dict[str, float] = {}
    entry_prices: Dict[str, float] = {}
    entry_dates: Dict[str, str] = {}
    timeline: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    sm = _normalize_sizing_mode(mode, sizing_mode)
    _valid_sz = frozenset({"equal_weight", "fixed_inr", "fixed_qty", "percent_equity"})
    if sm not in _valid_sz:
        sm = "fixed_inr" if mode == "sequential_entries" else "equal_weight"
    sv = float(sizing_value or 0.0)

    def universe_for(d: date) -> List[str]:
        if daily_universe:
            key = _d(d)
            raw = daily_universe.get(key)
            if raw is not None:
                return [s.strip() for s in raw if s and str(s).strip()]
        return list(base_symbols)

    def add_position(sym: str, qty: float, price: float) -> None:
        old_qty = float(positions.get(sym) or 0.0)
        old_entry = float(entry_prices.get(sym) or price)
        new_qty = old_qty + qty
        positions[sym] = new_qty
        entry_prices[sym] = ((old_qty * old_entry) + (qty * price)) / new_qty if new_qty > 0 else price

    def sell_position(sym: str, qty: float, price: float, d: date, reason: str) -> None:
        nonlocal cash
        notional = qty * price
        comm = _apply_commission(notional, commission_pct)
        cash += notional - comm
        positions.pop(sym, None)
        entry_price = entry_prices.pop(sym, None)
        entry_date = entry_dates.pop(sym, None)
        pnl_abs = (price - entry_price) * qty - comm if entry_price is not None else None
        entry_value = entry_price * qty if entry_price is not None else None
        trades.append({
            "date": _d(d), "symbol": sym, "side": "SELL", "qty": qty, "price": price,
            "reason": reason, "strategy_key": strategy_key,
            "entry_date": entry_date,
            "entry_price": round(entry_price, 4) if entry_price is not None else None,
            "pnl_abs": round(pnl_abs, 2) if pnl_abs is not None else None,
            "pnl_pct": round((pnl_abs / entry_value) * 100.0, 4) if pnl_abs is not None and entry_value and entry_value > 0 else None,
            "exit_value": round(notional, 2),
        })

    # Positions flagged for exit at next day's open: {sym: reason}
    # SL fires intraday (same day), indicator exits are detected at close
    # and executed at the *next* day's open so timing is realistic.
    _pending_exits: Dict[str, str] = {}

    def apply_sl_exits(d: date, day: Dict[str, Dict[str, float]]) -> None:
        """Intraday stop-loss: fires same day at stop price when low <= stop."""
        if not (stop_loss_pct and stop_loss_pct > 0):
            return
        for sym in list(positions.keys()):
            b = day.get(sym)
            if not b:
                continue
            entry = entry_prices.get(sym)
            if not entry:
                continue
            stop_price = entry * (1.0 - float(stop_loss_pct) / 100.0)
            if float(b["low"]) <= stop_price:
                sell_position(sym, float(positions[sym]), stop_price, d,
                              f"stop_loss_{stop_loss_pct:g}pct")

    def detect_indicator_exits(d: date, day: Dict[str, Dict[str, float]]) -> None:
        """Detect EMA/RSI exit conditions at close; execution deferred to next open."""
        rules = exit_rules or {}
        ema_period = int(rules.get("close_below_ema_period") or 0)
        rsi_period = int(rules.get("rsi_period") or 14)
        for sym in list(positions.keys()):
            if sym in _pending_exits:
                continue
            b = day.get(sym)
            if not b:
                continue
            series = (indicator_series or {}).get(sym) or {}
            if rules.get("exit_on_close_below_ema") and ema_period > 1:
                ema_val = (series.get(f"ema_{ema_period}") or {}).get(d)
                if ema_val is not None and float(b["close"]) < float(ema_val):
                    _pending_exits[sym] = f"close_below_ema_{ema_period}"
                    continue
            if rules.get("exit_on_rsi_below"):
                threshold = float(rules.get("rsi_below") or 35.0)
                rsi_val = (series.get(f"rsi_{rsi_period}") or {}).get(d)
                if rsi_val is not None and float(rsi_val) < threshold:
                    _pending_exits[sym] = f"rsi_below_{threshold:g}"

    # Keep old name as alias for buy_and_hold mode (which runs exits before timeline snap)
    def apply_risk_exits(d: date, day: Dict[str, Dict[str, float]]) -> None:
        apply_sl_exits(d, day)
        detect_indicator_exits(d, day)

    if mode == "buy_and_hold":
        d0 = dates[0]
        day = bars[d0]
        want = [s for s in universe_for(d0) if s in day]
        if not want:
            return [], []
        deployable = cash * deploy_fraction
        equity0 = cash
        n = len(want)

        if sm == "equal_weight":
            each = deployable / n
            for sym in want:
                o = day[sym]["open"]
                if o <= 0:
                    continue
                qty = each / o
                cost = qty * o
                comm = _apply_commission(cost, commission_pct)
                if cash < cost + comm:
                    break
                cash -= cost + comm
                add_position(sym, qty, o)
                entry_dates.setdefault(sym, _d(d0))
                trades.append({
                    "date": _d(d0), "symbol": sym, "side": "BUY", "qty": qty, "price": o,
                    "reason": "buy_and_hold_open", "strategy_key": strategy_key,
                    "sizing": sm,
                })
        elif sm == "fixed_inr":
            inr = sv if sv > 0 else entry_cash_per_trade
            for sym in want:
                o = day[sym]["open"]
                if o <= 0:
                    continue
                alloc = min(inr, cash * 0.99)
                if alloc < o:
                    continue
                qty = alloc / o
                cost = qty * o
                comm = _apply_commission(cost, commission_pct)
                if cash < cost + comm:
                    break
                cash -= cost + comm
                add_position(sym, qty, o)
                entry_dates.setdefault(sym, _d(d0))
                trades.append({
                    "date": _d(d0), "symbol": sym, "side": "BUY", "qty": qty, "price": o,
                    "reason": "buy_and_hold_open", "strategy_key": strategy_key,
                    "sizing": sm, "notional_inr": cost,
                })
        elif sm == "fixed_qty":
            if sv <= 0:
                return [], []
            qty = sv
            for sym in want:
                o = day[sym]["open"]
                if o <= 0:
                    continue
                cost = qty * o
                comm = _apply_commission(cost, commission_pct)
                if cash < cost + comm:
                    break
                cash -= cost + comm
                add_position(sym, qty, o)
                entry_dates.setdefault(sym, _d(d0))
                trades.append({
                    "date": _d(d0), "symbol": sym, "side": "BUY", "qty": qty, "price": o,
                    "reason": "buy_and_hold_open", "strategy_key": strategy_key,
                    "sizing": sm,
                })
        elif sm == "percent_equity":
            pct = sv if sv > 0 else (100.0 / max(n, 1))
            for sym in want:
                o = day[sym]["open"]
                if o <= 0:
                    continue
                alloc = min(equity0 * (pct / 100.0), cash * 0.99)
                if alloc < o:
                    continue
                qty = alloc / o
                cost = qty * o
                comm = _apply_commission(cost, commission_pct)
                if cash < cost + comm:
                    break
                cash -= cost + comm
                add_position(sym, qty, o)
                entry_dates.setdefault(sym, _d(d0))
                trades.append({
                    "date": _d(d0), "symbol": sym, "side": "BUY", "qty": qty, "price": o,
                    "reason": "buy_and_hold_open", "strategy_key": strategy_key,
                    "sizing": sm, "pct": pct,
                })
        else:
            return [], []
        for d in dates:
            day = bars[d]
            apply_risk_exits(d, day)
            mtm_close = _mtm_cash_and_value(positions, cash, day, "close")
            timeline.append({
                "date": _d(d),
                "cash": round(cash, 2),
                "positions": _pos_snapshot(positions),
                "positions_detail": _pos_detail_snapshot(positions, entry_prices, day),
                "mtm_at_close": round(mtm_close, 2),
            })

    elif mode == "sequential_entries":
        for d in dates:
            day = bars[d]

            # ── OPEN ──────────────────────────────────────────────────────────
            # 1. Execute deferred indicator exits from previous day at today's open.
            #    This frees slots so the replacement entries below can fill them.
            for sym, reason in list(_pending_exits.items()):
                if sym in positions and sym in day:
                    o = day[sym]["open"]
                    sell_position(sym, float(positions[sym]), o, d, reason)
                elif sym in positions:
                    # No bar today; carry forward, will execute when bar available
                    continue
                _pending_exits.pop(sym, None)

            # 2. Exit if symbol left the screener universe (at open)
            u_set = set(universe_for(d))
            if exit_if_not_in_universe and daily_universe:
                for sym in list(positions.keys()):
                    if sym not in u_set and sym in day:
                        o = day[sym]["open"]
                        sell_position(sym, float(positions[sym]), o, d, "exit_left_universe")

            # 3. Fill open slots with today's new signals (at open)
            #    Held accounts for positions we *just* freed above.
            held = set(positions.keys())
            open_slots = max(0, max_positions - len(held))
            candidates = [s for s in universe_for(d) if s in day and s not in held][: open_slots + 50]
            mtm_pre = _mtm_cash_and_value(positions, cash, day, "open")
            bought = 0
            per_slot_eq = (cash * deploy_fraction) / max(open_slots, 1) if open_slots else 0
            for sym in candidates:
                if bought >= open_slots:
                    break
                o = day[sym]["open"]
                if o <= 0:
                    continue
                if sm == "equal_weight":
                    budget = min(per_slot_eq, cash * 0.99)
                    if budget < o:
                        continue
                    qty = budget / o
                    cost = qty * o
                elif sm == "fixed_qty":
                    if sv <= 0:
                        continue
                    qty = sv
                    cost = qty * o
                elif sm == "percent_equity":
                    pct = sv if sv > 0 else 10.0
                    budget = min(mtm_pre * (pct / 100.0), cash * 0.99)
                    if budget < o:
                        continue
                    qty = budget / o
                    cost = qty * o
                else:
                    inr = sv if sv > 0 else entry_cash_per_trade
                    budget = min(inr, cash * 0.99)
                    if budget < o:
                        continue
                    qty = budget / o
                    cost = qty * o
                comm = _apply_commission(cost, commission_pct)
                if cash < cost + comm:
                    break
                cash -= cost + comm
                add_position(sym, qty, o)
                entry_dates.setdefault(sym, _d(d))
                bought += 1
                trades.append({
                    "date": _d(d), "symbol": sym, "side": "BUY", "qty": qty, "price": o,
                    "reason": "sequential_entry", "strategy_key": strategy_key,
                    "sizing": sm,
                })

            # ── INTRADAY ──────────────────────────────────────────────────────
            # 4. Stop-loss fires intraday; execute immediately at stop price.
            apply_sl_exits(d, day)

            # ── CLOSE ────────────────────────────────────────────────────────
            # 5. Detect indicator exit conditions (EMA/RSI) at close.
            #    These are flagged and will execute at *next* day's open (step 1).
            detect_indicator_exits(d, day)

            mtm_close = _mtm_cash_and_value(positions, cash, day, "close")
            timeline.append({
                "date": _d(d),
                "cash": round(cash, 2),
                "positions": _pos_snapshot(positions),
                "positions_detail": _pos_detail_snapshot(positions, entry_prices, day),
                "mtm_at_close": round(mtm_close, 2),
            })

    elif mode == "daily_screen_rebalance":
        for d in dates:
            day = bars[d]
            target = [s for s in universe_for(d) if s in day]
            mtm_open_pre = _mtm_cash_and_value(positions, cash, day, "open")

            for sym in list(positions.keys()):
                if sym not in day:
                    continue
                o = day[sym]["open"]
                qty = positions.pop(sym)
                sell_position(sym, qty, o, d, "rebalance_liquidate")

            if not target:
                mtm_close = cash
                timeline.append({
                    "date": _d(d),
                    "cash": round(cash, 2),
                    "positions": {},
                    "positions_detail": {},
                    "mtm_at_close": round(mtm_close, 2),
                    "mtm_at_open": round(mtm_open_pre, 2),
                })
                continue

            deploy = cash * deploy_fraction
            nt = len(target)
            equity_open = cash
            for sym in target:
                o = day[sym]["open"]
                if o <= 0:
                    continue
                if sm == "equal_weight":
                    each = deploy / nt
                    qty = each / o
                elif sm == "fixed_inr":
                    inr = sv if sv > 0 else entry_cash_per_trade
                    alloc = min(inr, cash * 0.99)
                    if alloc < o:
                        continue
                    qty = alloc / o
                elif sm == "fixed_qty":
                    if sv <= 0:
                        continue
                    qty = sv
                elif sm == "percent_equity":
                    pct = sv if sv > 0 else (100.0 / max(nt, 1))
                    alloc = min(equity_open * (pct / 100.0), cash * 0.99)
                    if alloc < o:
                        continue
                    qty = alloc / o
                else:
                    qty = (deploy / nt) / o
                cost = qty * o
                comm = _apply_commission(cost, commission_pct)
                if cash < cost + comm:
                    break
                cash -= cost + comm
                add_position(sym, qty, o)
                entry_dates.setdefault(sym, _d(d))
                trades.append({
                    "date": _d(d), "symbol": sym, "side": "BUY", "qty": qty, "price": o,
                    "reason": "rebalance_buy", "strategy_key": strategy_key,
                    "sizing": sm,
                })

            apply_risk_exits(d, day)
            mtm_close = _mtm_cash_and_value(positions, cash, day, "close")
            timeline.append({
                "date": _d(d),
                "cash": round(cash, 2),
                "positions": _pos_snapshot(positions),
                "positions_detail": _pos_detail_snapshot(positions, entry_prices, day),
                "mtm_at_close": round(mtm_close, 2),
                "mtm_at_open": round(mtm_open_pre, 2),
            })
    else:
        return [], []

    return timeline, trades


def run_replay(
    *,
    start_date: str,
    end_date: str,
    symbols: List[str],
    daily_universe: Optional[Dict[str, List[str]]] = None,
    initial_cash: float = 1_000_000.0,
    mode: str = "buy_and_hold",
    entry_cash_per_trade: float = 50_000.0,
    max_positions: int = 15,
    deploy_fraction: float = 0.98,
    commission_pct: float = 0.0,
    universe_type: str = "explicit",
    sector: Optional[str] = None,
    sector_match: str = "contains",
    all_eq_limit: int = 2000,
    exit_if_not_in_universe: bool = False,
    include_full_timeline: bool = True,
    strategies: Optional[List[Dict[str, Any]]] = None,
    sizing_mode: Optional[str] = None,
    sizing_value: float = 0.0,
    compare_catalog_strategies: bool = False,
    catalog_include_inactive: bool = False,
    capital_per_lane: str = "split",
    daily_universe_by_strategy: Optional[Dict[str, Dict[str, List[str]]]] = None,
    financial_filters: Optional[Dict[str, Any]] = None,
    stop_loss_pct: float = 0.0,
    exit_rules: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    market_url = os.getenv("MARKET_DATABASE_URL", "").strip()
    if not market_url:
        return {"error": "MARKET_DATABASE_URL not set; required for ohlcv_1d replay"}

    cpl = (capital_per_lane or "split").strip().lower()
    if cpl not in ("split", "isolated"):
        return {"error": "capital_per_lane must be 'split' or 'isolated'"}

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if end < start:
        return {"error": "end_date before start_date"}

    warnings: List[str] = []
    lanes_in: List[Dict[str, Any]]

    if compare_catalog_strategies:
        if strategies:
            warnings.append("compare_catalog_strategies=true: explicit strategies list is ignored")
        try:
            from trading_service.db import get_execution_strategies
        except Exception as e:
            return {"error": f"compare_catalog_strategies: could not load DB: {e}"}
        rows = get_execution_strategies(active_only=not catalog_include_inactive)
        if not rows:
            return {"error": "compare_catalog_strategies: no execution strategies in trading DB"}
        lanes_in = [
            lane_from_execution_strategy(
                es,
                request_universe_type=universe_type,
                symbols=symbols,
                sector=sector,
                sector_match=sector_match,
                all_eq_limit=all_eq_limit,
                daily_universe=daily_universe,
                daily_universe_by_strategy=daily_universe_by_strategy,
                mode=mode,
                entry_cash_per_trade=entry_cash_per_trade,
                max_positions=max_positions,
                deploy_fraction=deploy_fraction,
                exit_if_not_in_universe=exit_if_not_in_universe,
                sizing_mode=sizing_mode,
                sizing_value=sizing_value,
            )
            for es in rows
        ]
        n_cat = len(lanes_in)
        if cpl == "split":
            share = 1.0 / n_cat
            for L in lanes_in:
                L["cash_fraction"] = share
            warnings.append(
                f"catalog compare: {n_cat} lanes, capital_per_lane=split → each lane gets initial_cash × {share:.4f}"
            )
        else:
            for L in lanes_in:
                L["cash_fraction"] = 1.0
            warnings.append(
                f"catalog compare: {n_cat} lanes, capital_per_lane=isolated → each lane gets full initial_cash={initial_cash}"
            )
        if any((es.get("universe_source") or "") == "pattern_52w" for es in rows):
            warnings.append(
                "catalog: pattern_52w strategies use all_eq in replay — 52w band is not applied without daily_universe export"
            )
    elif strategies:
        lanes_in = [dict(x) for x in strategies]
        sfrac = sum(float(x.get("cash_fraction") or 1.0) for x in lanes_in)
        if lanes_in and abs(sfrac - 1.0) > 0.02:
            warnings.append(f"cash_fraction sum is {sfrac:.3f}, not 1.0 — lanes still use their fractions × initial_cash")
    else:
        lanes_in = [{
            "strategy_key": "default",
            "cash_fraction": 1.0,
            "universe_type": universe_type,
            "symbols": symbols,
            "sector": sector,
            "sector_match": sector_match,
            "all_eq_limit": all_eq_limit,
            "daily_universe": daily_universe,
            "mode": mode,
            "entry_cash_per_trade": entry_cash_per_trade,
            "max_positions": max_positions,
            "deploy_fraction": deploy_fraction,
            "exit_if_not_in_universe": exit_if_not_in_universe,
            "sizing_mode": sizing_mode,
            "sizing_value": sizing_value,
            "financial_filters": financial_filters,
            "stop_loss_pct": stop_loss_pct,
            "exit_rules": exit_rules,
        }]

    lane_results: List[Dict[str, Any]] = []
    all_syms: set = set()

    for lane in lanes_in:
        sk = str(lane.get("strategy_key") or "lane")
        frac = float(lane.get("cash_fraction") or 1.0)
        ut = lane.get("universe_type") or "explicit"
        sy = list(lane.get("symbols") or [])
        sec = lane.get("sector")
        sm = lane.get("sector_match") or "contains"
        ael = int(lane.get("all_eq_limit") or all_eq_limit)
        du = lane.get("daily_universe")
        if isinstance(du, dict):
            du = {str(k): list(v) if v is not None else [] for k, v in du.items()}
        else:
            du = None

        cat = lane.pop("_catalog_meta", None)

        resolved, w = resolve_universe_symbols(
            market_url,
            universe_type=ut,
            symbols=sy,
            sector=sec,
            sector_match=sm,
            all_eq_limit=ael,
        )
        if w and not (ut == "explicit" and not sy and du):
            warnings.append(f"{sk}: {w}")
        if not resolved and not du:
            warnings.append(f"{sk}: skipped — no symbols and no daily_universe")
            continue
        if du:
            for lst in du.values():
                all_syms.update(s.strip() for s in lst if s)
        all_syms.update(resolved)

        meta = {
            "strategy_key": sk,
            "cash_fraction": frac,
            "universe_type": ut,
            "resolved_symbol_count": len(resolved),
            "mode": lane.get("mode") or "buy_and_hold",
            "sizing_mode": lane.get("sizing_mode"),
            "sizing_value": lane.get("sizing_value"),
        }
        if cat:
            meta["catalog"] = cat

        lane_results.append({
            "meta": meta,
            "resolved_symbols_preview": resolved[:30],
            "lane": lane,
            "resolved": resolved,
            "daily_universe": du,
        })

    all_syms = sorted(all_syms)
    if not all_syms or not lane_results:
        return {"error": "no symbols after universe resolution (check sectors / limits / daily_universe)", "warnings": warnings}

    aliases = resolve_market_symbol_aliases(market_url, all_syms)
    alias_changes = sum(1 for original, mapped in aliases.items() if original != mapped)
    if alias_changes:
        warnings.append(f"normalized {alias_changes} symbols to market OHLCV format")

    for lr in lane_results:
        lr["resolved"] = _apply_symbol_aliases(lr["resolved"], aliases)
        du = lr["daily_universe"]
        if isinstance(du, dict):
            lr["daily_universe"] = {
                str(day): _apply_symbol_aliases(list(symbols or []), aliases)
                for day, symbols in du.items()
            }

    for lr in lane_results:
        lane_filters = lr["lane"].get("financial_filters") or financial_filters
        du_symbols = sorted({
            sym
            for values in (lr["daily_universe"] or {}).values()
            for sym in values
            if sym
        })
        filter_input = lr["resolved"] or du_symbols
        filtered, w, meta = filter_symbols_by_financials(market_url, filter_input, lane_filters)
        if w:
            warnings.append(f"{lr['meta']['strategy_key']}: {w}")
        if meta:
            lr["meta"]["financial_filter"] = meta
        lr["resolved"] = [sym for sym in lr["resolved"] if sym in set(filtered)] if lr["resolved"] else []
        lr["meta"]["resolved_symbol_count"] = len(filtered)
        should_filter_daily = bool(lane_filters and (lane_filters.get("enabled") or int(lane_filters.get("max_symbols") or 0) > 0))
        if isinstance(lr["daily_universe"], dict) and should_filter_daily:
            allowed = set(filtered)
            lr["daily_universe"] = {
                day: [sym for sym in symbols if sym in allowed]
                for day, symbols in lr["daily_universe"].items()
            }

    all_syms = sorted({aliases.get(sym, sym) for sym in all_syms})
    all_syms = sorted({
        sym
        for lr in lane_results
        for sym in (list(lr["resolved"]) + [s for values in (lr["daily_universe"] or {}).values() for s in values])
    })
    lane_results = [
        lr for lr in lane_results
        if lr["resolved"] or (isinstance(lr["daily_universe"], dict) and any(lr["daily_universe"].values()))
    ]
    if not all_syms or not lane_results:
        return {"error": "no symbols after financial/universe filtering", "warnings": warnings}

    bars, dates = fetch_ohlcv_1d(market_url, all_syms, start, end)
    if not dates:
        return {"error": "no ohlcv_1d rows in range", "warnings": warnings}

    indicator_series = build_indicator_series(bars, dates, all_syms, exit_rules)

    per_strategy: Dict[str, Any] = {}
    combined_compact: Dict[str, Dict[str, Any]] = {}

    valid_modes = frozenset({"buy_and_hold", "sequential_entries", "daily_screen_rebalance"})

    for lr in lane_results:
        lane = lr["lane"]
        sk = lr["meta"]["strategy_key"]
        cash_lane = float(initial_cash) * float(lane.get("cash_fraction") or 1.0)
        res_syms = lr["resolved"]
        du = lr["daily_universe"]
        lm = str(lane.get("mode") or "buy_and_hold")
        if lm not in valid_modes:
            per_strategy[sk] = {"error": f"unknown mode {lm!r}", "meta": lr["meta"]}
            continue

        tl, tr = _run_lane(
            bars=bars,
            dates=dates,
            base_symbols=res_syms,
            daily_universe=du,
            initial_cash=cash_lane,
            mode=lm,
            entry_cash_per_trade=float(lane.get("entry_cash_per_trade") or entry_cash_per_trade),
            max_positions=int(lane.get("max_positions") or max_positions),
            deploy_fraction=float(lane.get("deploy_fraction") or deploy_fraction),
            commission_pct=commission_pct,
            exit_if_not_in_universe=bool(lane.get("exit_if_not_in_universe", exit_if_not_in_universe)),
            strategy_key=sk,
            sizing_mode=lane.get("sizing_mode") if lane.get("sizing_mode") is not None else sizing_mode,
            sizing_value=float(lane.get("sizing_value", sizing_value)),
            stop_loss_pct=float(lane.get("stop_loss_pct", stop_loss_pct) or 0.0),
            exit_rules=lane.get("exit_rules") or exit_rules,
            indicator_series=indicator_series,
        )
        if not tl:
            per_strategy[sk] = {"error": "buy_and_hold: no overlap on first day" if lm == "buy_and_hold" else "no timeline produced", "meta": lr["meta"]}
            continue

        viz = _build_visualization(tl, tr, cash_lane, sk)
        analytics = _build_analytics(viz["timeline_compact"], initial_cash=cash_lane)
        ledgers = _build_position_ledgers(tl, tr)
        final_mtm = tl[-1]["mtm_at_close"] if tl else cash_lane
        # Enrich timeline rows with that day's entries/exits so the replay UI
        # can show activity (entries, exits with reason/PnL) per day.
        if tl and tr:
            trades_by_day: Dict[str, Dict[str, List]] = {}
            for t in tr:
                ds = t["date"]
                bucket = trades_by_day.setdefault(ds, {"entries": [], "exits": []})
                if t.get("side") == "BUY":
                    bucket["entries"].append(t)
                else:
                    bucket["exits"].append(t)
            for row in tl:
                day_trades = trades_by_day.get(row["date"], {})
                row["entries_today"] = day_trades.get("entries", [])
                row["exits_today"] = day_trades.get("exits", [])

        out_lane = {
            "strategy_key": sk,
            "meta": lr["meta"],
            "initial_cash": cash_lane,
            "final_mtm_close": round(final_mtm, 2),
            "pnl_abs": round(final_mtm - cash_lane, 2),
            "pnl_pct": round((final_mtm / cash_lane - 1) * 100, 4) if cash_lane else None,
            "trades_count": len(tr),
            "trades": tr[:400],
            "trades_truncated": len(tr) > 400,
            "position_ledgers": ledgers,
            "timeline_compact": analytics["timeline_compact"],
            "activity_days": viz["activity_days"],
            "activity_day_count": viz["activity_day_count"],
            "analytics": {k: v for k, v in analytics.items() if k != "timeline_compact"},
        }
        if include_full_timeline:
            out_lane["timeline"] = tl
        per_strategy[sk] = out_lane

        for row in analytics["timeline_compact"]:
            ds = row["date"]
            if ds not in combined_compact:
                combined_compact[ds] = {
                    "date": ds,
                    "mtm_close": 0.0,
                    "daily_pnl": 0.0,
                    "position_count": 0,
                    "strategies_with_activity": [],
                    "has_position_change": False,
                }
            combined_compact[ds]["mtm_close"] += row["mtm_close"]
            combined_compact[ds]["daily_pnl"] += row["daily_pnl"]
            combined_compact[ds]["position_count"] += row["position_count"]
            if row.get("has_position_change"):
                combined_compact[ds]["strategies_with_activity"].append(row["strategy_key"])
                combined_compact[ds]["has_position_change"] = True

    ordered_dates = sorted(combined_compact.keys())
    combined_timeline_compact = [combined_compact[d] for d in ordered_dates]

    ok_runs = [
        v for v in per_strategy.values()
        if isinstance(v, dict) and "pnl_abs" in v and "error" not in v
    ]
    if ok_runs:
        total_initial_deployed = sum(float(v["initial_cash"]) for v in ok_runs)
        final_combined = sum(float(v["final_mtm_close"]) for v in ok_runs)
    else:
        total_initial_deployed = float(initial_cash)
        final_combined = (
            combined_timeline_compact[-1]["mtm_close"] if combined_timeline_compact else total_initial_deployed
        )

    comparison_leaderboard: List[Dict[str, Any]] = []
    for v in ok_runs:
        cat = (v.get("meta") or {}).get("catalog") or {}
        comparison_leaderboard.append({
            "strategy_key": v["strategy_key"],
            "display_name": cat.get("display_name"),
            "execution_strategy_id": cat.get("execution_strategy_id"),
            "initial_cash": v["initial_cash"],
            "final_mtm_close": v["final_mtm_close"],
            "pnl_abs": v["pnl_abs"],
            "pnl_pct": v["pnl_pct"],
            "trades_count": v["trades_count"],
        })
    comparison_leaderboard.sort(
        key=lambda x: (x.get("pnl_pct") is None, -float(x["pnl_pct"] or 0.0))
    )

    combined_activity_by_date: Dict[str, Dict[str, Any]] = {}
    for sk, block in per_strategy.items():
        if not isinstance(block, dict) or "activity_days" not in block:
            continue
        for ad in block["activity_days"]:
            ds = ad["date"]
            if ds not in combined_activity_by_date:
                combined_activity_by_date[ds] = {
                    "date": ds,
                    "mtm_close": 0.0,
                    "daily_pnl": 0.0,
                    "lanes": [],
                }
            combined_activity_by_date[ds]["mtm_close"] += ad.get("mtm_close", 0)
            combined_activity_by_date[ds]["daily_pnl"] += ad.get("daily_pnl", 0)
            combined_activity_by_date[ds]["lanes"].append({
                "strategy_key": sk,
                "entries": ad.get("entries", []),
                "exits": ad.get("exits", []),
                "positions_after": ad.get("positions_after", {}),
            })
    combined_activity_days = [combined_activity_by_date[k] for k in sorted(combined_activity_by_date.keys())]
    combined_analytics = _build_analytics(
        combined_timeline_compact,
        initial_cash=total_initial_deployed,
    )

    return {
        "start_date": _d(start),
        "end_date": _d(end),
        "trading_days": len(dates),
        "capital_model": cpl if compare_catalog_strategies else None,
        "initial_cash_request": float(initial_cash),
        "initial_cash_total_deployed": round(total_initial_deployed, 2),
        "initial_cash_total": round(total_initial_deployed, 2),
        "final_mtm_close_combined": round(final_combined, 2),
        "pnl_abs_combined": round(final_combined - total_initial_deployed, 2),
        "pnl_pct_combined": round((final_combined / total_initial_deployed - 1) * 100, 4) if total_initial_deployed else None,
        "comparison_leaderboard": comparison_leaderboard,
        "universe_symbol_count": len(all_syms),
        "warnings": warnings,
        "strategies": per_strategy,
        "combined_timeline_compact": combined_analytics["timeline_compact"],
        "combined_activity_days": combined_activity_days,
        "analytics_combined": {k: v for k, v in combined_analytics.items() if k != "timeline_compact"},
        "caveat": "ohlcv_1d only; screener point-in-time needs daily_universe export; daily_screen_rebalance is high turnover.",
    }


def run_replay_legacy(**kwargs: Any) -> Dict[str, Any]:
    """Backward-compatible single-strategy call (old field names only)."""
    kw = dict(kwargs)
    kw.setdefault("universe_type", "explicit")
    kw.setdefault("include_full_timeline", True)
    kw.setdefault("strategies", None)
    kw.setdefault("compare_catalog_strategies", False)
    kw.setdefault("catalog_include_inactive", False)
    kw.setdefault("capital_per_lane", "split")
    kw.setdefault("daily_universe_by_strategy", None)
    return run_replay(**kw)
