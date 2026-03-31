"""
Execution-strategy consumer (entry side): read DB config + enabled flags, resolve universe, diff vs open positions.

Operational model
-----------------
Jobs are **short-lived ticks** on a schedule (e.g. cron via scheduler → POST /admin/...). Each run:
re-reads Postgres (strategies, account map, positions) and calls screener HTTP. Nothing stays running
until a position closes; multi-day holds are fine because the **next** scheduled run sees updated positions.

This module does **not** place orders yet; it produces **candidate symbols** for a future risk/router step.
Exit / trailing logic would be a separate scheduled job reading positions + trading_trailing_stops (when wired).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from trading_service.db import get_accounts, get_account_execution_strategies, get_execution_strategy_by_id, get_positions
from trading_service.universe_resolver import resolve_universe


def _norm_symbol(s: str) -> str:
    return (s or "").strip().upper()


def run_entry_candidate_scan(account_id: Optional[int] = None) -> Dict[str, Any]:
    """
    For each active account (or one account), consider execution strategies enabled in
    trading_strategy_account_map. Resolve universe; candidates = universe symbols not already
    held (any strategy) on that account.
    """
    accounts = get_accounts(active_only=True)
    if account_id is not None:
        accounts = [a for a in accounts if a["id"] == account_id]

    rows: List[Dict[str, Any]] = []

    for acc in accounts:
        aid = acc["id"]
        strategies = get_account_execution_strategies(aid)
        pos_list = get_positions(account_id=aid)
        held = {_norm_symbol(p["symbol"]) for p in pos_list if p.get("quantity", 0) != 0}

        for st in strategies:
            if not st.get("enabled_for_account"):
                continue
            eid = st["id"]
            full = get_execution_strategy_by_id(eid)
            if not full:
                continue
            syms, meta = resolve_universe(full)
            err = meta.get("error")
            candidates = sorted({_norm_symbol(s) for s in syms if _norm_symbol(s) not in held})
            rows.append({
                "account_id": aid,
                "broker": acc.get("broker", ""),
                "account_display": acc.get("display_name") or "",
                "execution_strategy_id": eid,
                "slug": st["slug"],
                "display_name": st["display_name"],
                "universe_source": full.get("universe_source"),
                "universe_symbol_count": len(syms),
                "candidate_symbol_count": len(candidates),
                "candidates_preview": candidates[:40],
                "universe_meta": meta,
                "universe_error": err,
            })

    return {
        "model": "scheduled_ticks_not_long_running",
        "accounts_scanned": len(accounts),
        "strategy_runs": len(rows),
        "rows": rows,
    }
