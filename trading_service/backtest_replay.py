"""
Dry-run / historical replay on **stored** daily OHLC (`ohlcv_1d` in market DB).

Extensible universes
--------------------
- **explicit** symbol list
- **all_eq**: all `*-EQ` names in `symbols` table (optional cap — large = slow / heavy)
- **sector**: filter `symbols.sector` (exact / contains / ilike)
- **screener-style lists**: pass **daily_universe** `{ "YYYY-MM-DD": ["SYM-EQ", ...] }` from archived runs

Multi-strategy: run isolated **lanes** (each gets a **cash_fraction** of initial capital), then aggregate.

Visualization helpers
---------------------
- **timeline_compact**: every day — date, mtm_close, daily_pnl, position_count, has_position_change
- **activity_days**: only days with trades or position set changes — for charts / tables
"""
from __future__ import annotations

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
        out = sorted({s.strip() for s in symbols if s and str(s).strip()})
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
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    cash = float(initial_cash)
    positions: Dict[str, float] = {}
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
                positions[sym] = positions.get(sym, 0) + qty
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
                positions[sym] = positions.get(sym, 0) + qty
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
                positions[sym] = positions.get(sym, 0) + qty
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
                positions[sym] = positions.get(sym, 0) + qty
                trades.append({
                    "date": _d(d0), "symbol": sym, "side": "BUY", "qty": qty, "price": o,
                    "reason": "buy_and_hold_open", "strategy_key": strategy_key,
                    "sizing": sm, "pct": pct,
                })
        else:
            return [], []
        for d in dates:
            day = bars[d]
            mtm_close = _mtm_cash_and_value(positions, cash, day, "close")
            timeline.append({
                "date": _d(d),
                "cash": round(cash, 2),
                "positions": _pos_snapshot(positions),
                "mtm_at_close": round(mtm_close, 2),
            })

    elif mode == "sequential_entries":
        for d in dates:
            day = bars[d]
            u_set = set(universe_for(d))
            if exit_if_not_in_universe and daily_universe:
                for sym in list(positions.keys()):
                    if sym not in u_set and sym in day:
                        o = day[sym]["open"]
                        qty = positions.pop(sym)
                        notional = qty * o
                        comm = _apply_commission(notional, commission_pct)
                        cash += notional - comm
                        trades.append({
                            "date": _d(d), "symbol": sym, "side": "SELL", "qty": qty, "price": o,
                            "reason": "exit_left_universe", "strategy_key": strategy_key,
                        })

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
                positions[sym] = qty
                bought += 1
                trades.append({
                    "date": _d(d), "symbol": sym, "side": "BUY", "qty": qty, "price": o,
                    "reason": "sequential_entry", "strategy_key": strategy_key,
                    "sizing": sm,
                })

            mtm_close = _mtm_cash_and_value(positions, cash, day, "close")
            timeline.append({
                "date": _d(d),
                "cash": round(cash, 2),
                "positions": _pos_snapshot(positions),
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
                notional = qty * o
                comm = _apply_commission(notional, commission_pct)
                cash += notional - comm
                trades.append({
                    "date": _d(d), "symbol": sym, "side": "SELL", "qty": qty, "price": o,
                    "reason": "rebalance_liquidate", "strategy_key": strategy_key,
                })

            if not target:
                mtm_close = cash
                timeline.append({
                    "date": _d(d),
                    "cash": round(cash, 2),
                    "positions": {},
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
                positions[sym] = qty
                trades.append({
                    "date": _d(d), "symbol": sym, "side": "BUY", "qty": qty, "price": o,
                    "reason": "rebalance_buy", "strategy_key": strategy_key,
                    "sizing": sm,
                })

            mtm_close = _mtm_cash_and_value(positions, cash, day, "close")
            timeline.append({
                "date": _d(d),
                "cash": round(cash, 2),
                "positions": _pos_snapshot(positions),
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
) -> Dict[str, Any]:
    market_url = os.getenv("MARKET_DATABASE_URL", "").strip()
    if not market_url:
        return {"error": "MARKET_DATABASE_URL not set; required for ohlcv_1d replay"}

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if end < start:
        return {"error": "end_date before start_date"}

    warnings: List[str] = []
    lanes_in: List[Dict[str, Any]]
    if strategies:
        lanes_in = strategies
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

        resolved, w = resolve_universe_symbols(
            market_url,
            universe_type=ut,
            symbols=sy,
            sector=sec,
            sector_match=sm,
            all_eq_limit=ael,
        )
        if w:
            warnings.append(f"{sk}: {w}")
        if not resolved and not du:
            warnings.append(f"{sk}: skipped — no symbols and no daily_universe")
            continue
        if du:
            for lst in du.values():
                all_syms.update(s.strip() for s in lst if s)
        all_syms.update(resolved)

        lane_results.append({
            "meta": {
                "strategy_key": sk,
                "cash_fraction": frac,
                "universe_type": ut,
                "resolved_symbol_count": len(resolved),
                "mode": lane.get("mode") or "buy_and_hold",
                "sizing_mode": lane.get("sizing_mode"),
                "sizing_value": lane.get("sizing_value"),
            },
            "resolved_symbols_preview": resolved[:30],
            "lane": lane,
            "resolved": resolved,
            "daily_universe": du,
        })

    all_syms = sorted(all_syms)
    if not all_syms or not lane_results:
        return {"error": "no symbols after universe resolution (check sectors / limits / daily_universe)", "warnings": warnings}

    bars, dates = fetch_ohlcv_1d(market_url, all_syms, start, end)
    if not dates:
        return {"error": "no ohlcv_1d rows in range", "warnings": warnings}

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
        )
        if not tl:
            per_strategy[sk] = {"error": "buy_and_hold: no overlap on first day" if lm == "buy_and_hold" else "no timeline produced", "meta": lr["meta"]}
            continue

        viz = _build_visualization(tl, tr, cash_lane, sk)
        final_mtm = tl[-1]["mtm_at_close"] if tl else cash_lane
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
            "timeline_compact": viz["timeline_compact"],
            "activity_days": viz["activity_days"],
            "activity_day_count": viz["activity_day_count"],
        }
        if include_full_timeline:
            out_lane["timeline"] = tl
        per_strategy[sk] = out_lane

        for row in viz["timeline_compact"]:
            ds = row["date"]
            if ds not in combined_compact:
                combined_compact[ds] = {
                    "date": ds,
                    "mtm_close": 0.0,
                    "daily_pnl": 0.0,
                    "position_count": 0,
                    "strategies_with_activity": [],
                }
            combined_compact[ds]["mtm_close"] += row["mtm_close"]
            combined_compact[ds]["daily_pnl"] += row["daily_pnl"]
            combined_compact[ds]["position_count"] += row["position_count"]
            if row.get("has_position_change"):
                combined_compact[ds]["strategies_with_activity"].append(row["strategy_key"])

    ordered_dates = sorted(combined_compact.keys())
    combined_timeline_compact = [combined_compact[d] for d in ordered_dates]

    total_initial = float(initial_cash)

    final_combined = combined_timeline_compact[-1]["mtm_close"] if combined_timeline_compact else total_initial

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

    return {
        "start_date": _d(start),
        "end_date": _d(end),
        "trading_days": len(dates),
        "initial_cash_total": total_initial,
        "final_mtm_close_combined": round(final_combined, 2),
        "pnl_abs_combined": round(final_combined - total_initial, 2),
        "pnl_pct_combined": round((final_combined / total_initial - 1) * 100, 4) if total_initial else None,
        "universe_symbol_count": len(all_syms),
        "warnings": warnings,
        "strategies": per_strategy,
        "combined_timeline_compact": combined_timeline_compact,
        "combined_activity_days": combined_activity_days,
        "caveat": "ohlcv_1d only; screener point-in-time needs daily_universe export; daily_screen_rebalance is high turnover.",
    }


def run_replay_legacy(**kwargs: Any) -> Dict[str, Any]:
    """Backward-compatible single-strategy call (old field names only)."""
    kw = dict(kwargs)
    kw.setdefault("universe_type", "explicit")
    kw.setdefault("include_full_timeline", True)
    kw.setdefault("strategies", None)
    return run_replay(**kw)
