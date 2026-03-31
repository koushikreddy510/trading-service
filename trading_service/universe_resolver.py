"""
Resolve symbol universe for an execution strategy (screener run, pattern screens, full list, unions).
Uses STRATEGY_SCREENER_URL (default http://127.0.0.1:8000) and optional MARKET_DATABASE_URL for full_universe.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import requests

DEFAULT_SCREENER_URL = os.getenv("STRATEGY_SCREENER_URL", "http://127.0.0.1:8000")
REQUEST_TIMEOUT = 120


def _normalize_symbol(sym: str) -> str:
    s = (sym or "").strip()
    if not s:
        return ""
    return s


def _dedupe(symbols: List[str]) -> List[str]:
    seen = set()
    out = []
    for s in symbols:
        n = _normalize_symbol(s)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _get_json(url: str, params: Optional[dict] = None) -> Tuple[Optional[dict], Optional[str]]:
    try:
        r = requests.get(url, params=params or {}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json(), None
    except Exception as e:
        return None, str(e)


def _paginate_run_matches(base: str, strategy_id: int, cap_filter: str = "", sector_filter: str = "") -> Tuple[List[str], Dict[str, Any]]:
    """Fetch all pages from GET /run/{id} (page_size max 50)."""
    symbols: List[str] = []
    page = 1
    page_size = 50
    total_reported = 0
    while True:
        params = {
            "page": page,
            "page_size": page_size,
            "matched_only": "true",
        }
        if cap_filter and cap_filter != "all":
            params["cap_filter"] = cap_filter
        if sector_filter and sector_filter != "all":
            params["sector_filter"] = sector_filter
        data, err = _get_json(f"{base.rstrip('/')}/run/{strategy_id}", params)
        if err or not data:
            return _dedupe(symbols), {"error": err or "empty", "pages_fetched": page - 1}
        matches = data.get("matches") or []
        for m in matches:
            sym = m.get("symbol")
            if sym:
                symbols.append(sym)
        total_reported = data.get("total", total_reported)
        if len(matches) < page_size:
            break
        page += 1
        if page > 200:
            break
    return _dedupe(symbols), {"pages_fetched": page, "total_reported": total_reported}


def _paginate_pattern_results(
    base: str, path: str, result_key: str, symbol_key: str = "symbol", extra_params: Optional[dict] = None
) -> Tuple[List[str], Dict[str, Any]]:
    symbols: List[str] = []
    page = 1
    page_size = 100
    total_reported = 0
    while True:
        params = {"page": page, "page_size": page_size}
        if extra_params:
            for k, v in extra_params.items():
                if v is None:
                    continue
                params[k] = "true" if v is True else ("false" if v is False else v)
        data, err = _get_json(f"{base.rstrip('/')}{path}", params)
        if err or not data:
            return _dedupe(symbols), {"error": err or "empty", "path": path}
        rows = data.get(result_key) or []
        for row in rows:
            sym = row.get(symbol_key)
            if sym:
                symbols.append(sym)
        total_reported = data.get("total", total_reported)
        if len(rows) < page_size:
            break
        page += 1
        if page > 500:
            break
    return _dedupe(symbols), {"path": path, "total_reported": total_reported}


def _full_universe_from_market_db() -> Tuple[List[str], Dict[str, Any]]:
    url = os.getenv("MARKET_DATABASE_URL", "").strip()
    if not url:
        return [], {"error": "MARKET_DATABASE_URL not set; required for full_universe"}
    try:
        from sqlalchemy import create_engine, text
        eng = create_engine(url, pool_pre_ping=True)
        with eng.connect() as conn:
            rows = conn.execute(
                text("SELECT tradingsymbol FROM symbols WHERE tradingsymbol LIKE '%-EQ' ORDER BY tradingsymbol")
            ).fetchall()
        syms = [r[0] for r in rows if r[0]]
        return syms, {"source": "market_db", "count": len(syms)}
    except Exception as e:
        return [], {"error": str(e)}


def resolve_universe(strategy: Dict[str, Any], screener_base_url: Optional[str] = None) -> Tuple[List[str], Dict[str, Any]]:
    """
    strategy: row dict with universe_source, screener_strategy_id, config (merged config_json).
    Returns (symbols, meta).
    """
    base = (screener_base_url or DEFAULT_SCREENER_URL).rstrip("/")
    src = strategy.get("universe_source") or "screener_filtered"
    cfg = strategy.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    meta: Dict[str, Any] = {"universe_source": src}

    if src == "screener_filtered":
        sid = strategy.get("screener_strategy_id")
        if sid is None:
            return [], {**meta, "error": "screener_strategy_id not set"}
        cap = cfg.get("universe_cap_filter") or ""
        sec = cfg.get("universe_sector_filter") or ""
        syms, m = _paginate_run_matches(base, int(sid), cap_filter=cap, sector_filter=sec)
        return syms, {**meta, **m}

    if src == "full_universe":
        syms, m = _full_universe_from_market_db()
        return syms, {**meta, **m}

    if src == "pattern_52w":
        params = {}
        if cfg.get("pct_from_high_min") is not None:
            params["pct_from_high_min"] = cfg["pct_from_high_min"]
        if cfg.get("pct_from_high_max") is not None:
            params["pct_from_high_max"] = cfg["pct_from_high_max"]
        if cfg.get("near_pct") is not None:
            params["near_pct"] = cfg["near_pct"]
        if cfg.get("at_52w_high"):
            params["at_52w_high"] = "true"
        syms, m = _paginate_pattern_results(base, "/patterns/52w", "results", extra_params=params)
        return syms, {**meta, **m}

    if src == "screen_52w":
        screen_cfg = cfg.get("universe_screen") or {}
        if not isinstance(screen_cfg, dict):
            screen_cfg = {}
        params = {}
        for k in (
            "near_pct",
            "pct_from_high_min",
            "pct_from_high_max",
            "at_52w_high",
            "cap_filter",
            "sector_filter",
            "chart_timeframe",
            "chart_bars",
        ):
            if screen_cfg.get(k) is not None:
                if k == "at_52w_high" and screen_cfg[k]:
                    params["at_52w_high"] = "true"
                else:
                    params[k] = screen_cfg[k]
        syms, m = _paginate_pattern_results(base, "/patterns/52w", "results", extra_params=params)
        return syms, {**meta, **m}

    if src == "screen_structural":
        screen_cfg = cfg.get("universe_screen") or {}
        if not isinstance(screen_cfg, dict):
            screen_cfg = {}
        params = {}
        for k in ("signal", "pattern", "cap_filter", "sector_filter", "lookback", "timeframe", "chart_bars"):
            if screen_cfg.get(k) is not None:
                params[k] = screen_cfg[k]
        syms, m = _paginate_pattern_results(base, "/patterns/structural", "results", extra_params=params)
        return syms, {**meta, **m}

    if src == "screen_candle":
        screen_cfg = cfg.get("universe_screen") or {}
        if not isinstance(screen_cfg, dict):
            screen_cfg = {}
        params = {}
        for k in ("signal", "pattern", "cap_filter", "sector_filter", "lookback"):
            if screen_cfg.get(k) is not None:
                params[k] = screen_cfg[k]
        syms, m = _paginate_pattern_results(base, "/patterns", "results", extra_params=params)
        return syms, {**meta, **m}

    if src == "union_screener_screen":
        sid = strategy.get("screener_strategy_id")
        if sid is None:
            return [], {**meta, "error": "screener_strategy_id required for union"}
        screen = (cfg.get("union_screen") or "52w").lower()
        cap = cfg.get("universe_cap_filter") or ""
        sec = cfg.get("universe_sector_filter") or ""
        a, ma = _paginate_run_matches(base, int(sid), cap_filter=cap, sector_filter=sec)
        screen_cfg = cfg.get("universe_screen") or {}
        if not isinstance(screen_cfg, dict):
            screen_cfg = {}
        params = {k: v for k, v in screen_cfg.items() if v is not None}
        if screen == "52w":
            b, mb = _paginate_pattern_results(base, "/patterns/52w", "results", extra_params=params)
        elif screen == "structural":
            b, mb = _paginate_pattern_results(base, "/patterns/structural", "results", extra_params=params)
        elif screen == "candle":
            b, mb = _paginate_pattern_results(base, "/patterns", "results", extra_params=params)
        else:
            return [], {**meta, "error": f"unknown union_screen: {screen}"}
        merged = _dedupe(a + b)
        return merged, {**meta, "union": True, "screener_count": len(a), "screen_count": len(b), "screen_meta": mb}

    return [], {**meta, "error": f"unknown universe_source: {src}"}
