"""
Trading service API: accounts, orders, positions, PnL.
Prometheus metrics at /metrics for Grafana dashboards.
Run: uvicorn main:app --host 0.0.0.0 --port 8001
"""
import os
from contextlib import asynccontextmanager
from datetime import date
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from trading_service.db import ensure_tables
        ensure_tables()
    except Exception:
        pass  # Tables may already exist or DATABASE_URL not set yet
    yield


app = FastAPI(title="Trading Service", version="1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    """Prometheus metrics for PnL, positions, orders. Scrape this endpoint."""
    from trading_service.metrics import get_metrics_output
    from prometheus_client import CONTENT_TYPE_LATEST
    return Response(
        content=get_metrics_output(),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.get("/trading/accounts")
def trading_accounts():
    try:
        from trading_service.db import get_accounts
        return {"accounts": get_accounts()}
    except Exception as e:
        return {"accounts": [], "error": str(e)}


@app.get("/trading/orders")
def trading_orders(
    account_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    try:
        from trading_service.db import get_orders
        orders = get_orders(account_id=account_id, status=status, limit=limit, offset=offset)
        return {"orders": orders, "total": len(orders)}
    except Exception as e:
        return {"orders": [], "total": 0, "error": str(e)}


@app.get("/trading/positions")
def trading_positions(account_id: Optional[int] = Query(None)):
    try:
        from trading_service.db import get_positions
        return {"positions": get_positions(account_id=account_id)}
    except Exception as e:
        return {"positions": [], "error": str(e)}


@app.get("/trading/execution-strategies")
def trading_execution_strategies(active_only: bool = Query(True)):
    """Catalog of execution strategies (risk/entry/exit). See docs/TRADING_STRATEGIES_AND_PIPELINE.md."""
    try:
        from trading_service.db import get_execution_strategies
        strategies = get_execution_strategies(active_only=active_only)
        return {
            "strategies": strategies,
            "universe_note": (
                "Universe options: screener_filtered, full_universe, pattern_52w, screen_structural, "
                "screen_52w, screen_candle, union_screener_screen (see docs). "
                "While an execution strategy has open positions, universe_source, screener link, and universe-related "
                "config cannot change (PATCH returns 409)."
            ),
        }
    except Exception as e:
        return {"strategies": [], "error": str(e)}


@app.get("/trading/execution-strategies/{slug}")
def trading_execution_strategy_by_slug(slug: str):
    try:
        from trading_service.db import get_execution_strategy_by_slug
        s = get_execution_strategy_by_slug(slug)
        if not s:
            raise HTTPException(status_code=404, detail="Unknown strategy slug")
        return s
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/trading/accounts/{account_id}/execution-strategies")
def trading_account_execution_strategies(account_id: int):
    """Per-account enablement and overrides for each execution strategy."""
    try:
        from trading_service.db import get_account_execution_strategies, get_accounts
        ids = {a["id"] for a in get_accounts(active_only=False)}
        if account_id not in ids:
            raise HTTPException(status_code=404, detail="Unknown account_id")
        return {"account_id": account_id, "strategies": get_account_execution_strategies(account_id)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AccountStrategyToggle(BaseModel):
    is_enabled: bool


class ExecutionStrategyPatchBody(BaseModel):
    universe_source: Optional[str] = None
    screener_strategy_id: Optional[int] = None
    screener_strategy_name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


@app.patch("/trading/execution-strategies/{strategy_id}")
def trading_patch_execution_strategy(strategy_id: int, body: ExecutionStrategyPatchBody):
    try:
        from trading_service.db import patch_execution_strategy

        patch = body.model_dump(exclude_unset=True)
        if not patch:
            raise HTTPException(status_code=400, detail="No fields to update")
        ok, err, row = patch_execution_strategy(strategy_id, patch)
        if err == "unknown_strategy_id":
            raise HTTPException(status_code=404, detail="Unknown execution strategy id")
        if err and err.startswith("unknown_fields"):
            raise HTTPException(status_code=400, detail=err)
        if err == "universe_locked_open_positions":
            raise HTTPException(
                status_code=409,
                detail="Universe settings are locked while this strategy has open positions. Close or reassign positions first.",
            )
        if not ok or not row:
            raise HTTPException(status_code=500, detail=err or "update failed")
        return row
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/trading/execution-strategies/{strategy_id}/universe-preview")
def trading_execution_strategy_universe_preview(strategy_id: int, limit: int = Query(500, ge=1, le=10000)):
    try:
        from trading_service.db import get_execution_strategy_by_id
        from trading_service.universe_resolver import resolve_universe

        row = get_execution_strategy_by_id(strategy_id)
        if not row:
            raise HTTPException(status_code=404, detail="Unknown execution strategy id")
        syms, meta = resolve_universe(row)
        truncated = len(syms) > limit
        return {
            "strategy_id": strategy_id,
            "symbols": syms[:limit],
            "symbol_count": len(syms),
            "truncated": truncated,
            "meta": meta,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/trading/universe/resolve-all")
def trading_resolve_all_universes(
    active_only: bool = Query(True),
    include_symbols: bool = Query(False),
    symbol_limit: int = Query(50, ge=0, le=2000),
):
    """
    Resolve entry universes for all (active) execution strategies in one call.
    For schedulers and observability: default is counts + meta only (no symbol list).
    Set STRATEGY_SCREENER_URL / MARKET_DATABASE_URL on trading-service as needed.
    """
    try:
        from trading_service.db import get_execution_strategies
        from trading_service.universe_resolver import resolve_universe

        strategies = get_execution_strategies(active_only=active_only)
        items = []
        for s in strategies:
            syms, meta = resolve_universe(s)
            item = {
                "id": s["id"],
                "slug": s["slug"],
                "display_name": s["display_name"],
                "universe_source": s["universe_source"],
                "symbol_count": len(syms),
                "meta": meta,
            }
            if include_symbols:
                lim = symbol_limit if symbol_limit > 0 else 0
                item["symbols"] = syms[:lim] if lim else []
                if lim:
                    item["symbols_truncated"] = len(syms) > lim
            items.append(item)
        return {"strategies": items, "strategy_count": len(items)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/trading/accounts/{account_id}/execution-strategies/{execution_strategy_id}")
def trading_toggle_account_strategy(
    account_id: int,
    execution_strategy_id: int,
    body: AccountStrategyToggle,
):
    try:
        from trading_service.db import set_account_strategy_enabled, get_accounts
        ids = {a["id"] for a in get_accounts(active_only=False)}
        if account_id not in ids:
            raise HTTPException(status_code=404, detail="Unknown account_id")
        ok = set_account_strategy_enabled(account_id, execution_strategy_id, body.is_enabled)
        if not ok:
            raise HTTPException(status_code=404, detail="Unknown execution_strategy_id")
        return {"ok": True, "account_id": account_id, "execution_strategy_id": execution_strategy_id, "is_enabled": body.is_enabled}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/trading/pnl")
def trading_pnl(
    account_id: Optional[int] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    limit: int = Query(90, ge=1, le=365),
):
    try:
        from trading_service.db import get_pnl_snapshots
        fd = date.fromisoformat(from_date) if from_date else None
        td = date.fromisoformat(to_date) if to_date else None
        snapshots = get_pnl_snapshots(account_id=account_id, from_date=fd, to_date=td, limit=limit)
        return {"snapshots": snapshots}
    except Exception as e:
        return {"snapshots": [], "error": str(e)}


# --- Admin / Config ---

@app.get("/admin/data-status")
def admin_data_status():
    try:
        from admin_manager import get_data_status
        return get_data_status()
    except Exception as e:
        return {"error": str(e)}


@app.post("/admin/sync-positions")
def admin_sync_positions(account_id: Optional[int] = Query(None)):
    try:
        from admin_manager import start_sync_positions
        return start_sync_positions(account_id=account_id)
    except Exception as e:
        return {"error": str(e)}


@app.post("/admin/sync-pnl")
def admin_sync_pnl():
    try:
        from admin_manager import start_sync_pnl
        return start_sync_pnl()
    except Exception as e:
        return {"error": str(e)}


class StrategyLaneSpec(BaseModel):
    """One backtest lane (isolated book). cash_fraction × initial_cash is starting capital."""

    strategy_key: str = "lane_a"
    cash_fraction: float = 1.0
    universe_type: str = "explicit"
    symbols: List[str] = Field(default_factory=list)
    sector: Optional[str] = None
    sector_match: str = "contains"
    all_eq_limit: int = 2000
    daily_universe: Optional[Dict[str, List[str]]] = None
    mode: str = "buy_and_hold"
    entry_cash_per_trade: float = 50_000.0
    max_positions: int = 15
    deploy_fraction: float = 0.98
    exit_if_not_in_universe: bool = False
    sizing_mode: Optional[str] = None
    sizing_value: float = 0.0


class BacktestReplayBody(BaseModel):
    """Replay on market DB ohlcv_1d. See trading_service/backtest_replay.py."""

    start_date: str
    end_date: str
    initial_cash: float = 1_000_000.0
    commission_pct: float = 0.0
    include_full_timeline: bool = True
    symbols: List[str] = Field(default_factory=list)
    daily_universe: Optional[Dict[str, List[str]]] = None
    mode: str = "buy_and_hold"
    universe_type: str = "explicit"
    sector: Optional[str] = None
    sector_match: str = "contains"
    all_eq_limit: int = 2000
    entry_cash_per_trade: float = 50_000.0
    max_positions: int = 15
    deploy_fraction: float = 0.98
    exit_if_not_in_universe: bool = False
    strategies: Optional[List[StrategyLaneSpec]] = None
    sizing_mode: Optional[str] = None
    sizing_value: float = 0.0
    compare_catalog_strategies: bool = False
    catalog_include_inactive: bool = False
    capital_per_lane: Literal["split", "isolated"] = "split"
    daily_universe_by_strategy: Optional[Dict[str, Dict[str, List[str]]]] = None
    financial_filters: Optional[Dict[str, Any]] = None
    stop_loss_pct: float = 0.0
    exit_rules: Optional[Dict[str, Any]] = None


@app.post("/admin/backtest-replay")
def admin_backtest_replay(body: BacktestReplayBody):
    """
    Dry-run on ohlcv_1d. Universes: explicit list, all_eq (cap), sector, and/or daily_universe (screener export).
    Modes: buy_and_hold, sequential_entries, daily_screen_rebalance (full turnover to that day's list).
    Response: timeline_compact (every day PnL), activity_days (entries/exits only), combined_* for multi-lane,
    comparison_leaderboard (sorted by pnl_pct) when multiple strategies complete.
    Set compare_catalog_strategies=true to run one lane per trading_execution_strategies row (same mode/sizing for all).
    capital_per_lane=split → one initial_cash pot divided across lanes; isolated → each lane gets full initial_cash.
    sizing_mode fixed_inr / fixed_qty / equal_weight / percent_equity applies uniformly (see backtest_replay).
    Set include_full_timeline=false for lighter JSON on large runs.
    """
    try:
        from trading_service.backtest_replay import run_replay

        strat = [s.model_dump() for s in body.strategies] if body.strategies else None
        out = run_replay(
            start_date=body.start_date,
            end_date=body.end_date,
            symbols=body.symbols,
            daily_universe=body.daily_universe,
            initial_cash=body.initial_cash,
            mode=body.mode,
            entry_cash_per_trade=body.entry_cash_per_trade,
            max_positions=body.max_positions,
            deploy_fraction=body.deploy_fraction,
            commission_pct=body.commission_pct,
            universe_type=body.universe_type,
            sector=body.sector,
            sector_match=body.sector_match,
            all_eq_limit=body.all_eq_limit,
            exit_if_not_in_universe=body.exit_if_not_in_universe,
            include_full_timeline=body.include_full_timeline,
            strategies=strat,
            sizing_mode=body.sizing_mode,
            sizing_value=body.sizing_value,
            compare_catalog_strategies=body.compare_catalog_strategies,
            catalog_include_inactive=body.catalog_include_inactive,
            capital_per_lane=body.capital_per_lane,
            daily_universe_by_strategy=body.daily_universe_by_strategy,
            financial_filters=body.financial_filters,
            stop_loss_pct=body.stop_loss_pct,
            exit_rules=body.exit_rules,
        )
        if out.get("error"):
            raise HTTPException(status_code=400, detail=out["error"])
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/entry-candidate-scan")
def admin_entry_candidate_scan(account_id: Optional[int] = Query(None)):
    """
    Consumer tick: for each account, each execution strategy enabled in the account map,
    resolve universe and list symbols not already open on that account. Does not place orders.
    Runs in a background thread; poll GET /admin/job/entry_candidate_scan for result when status=completed.
    """
    try:
        from admin_manager import start_entry_candidate_scan
        return start_entry_candidate_scan(account_id=account_id)
    except Exception as e:
        return {"error": str(e)}


@app.get("/admin/job/{job_id}")
def admin_job_status(job_id: str):
    from admin_manager import get_job_status
    return get_job_status(job_id)


@app.get("/admin/jobs")
def admin_all_jobs():
    from admin_manager import get_all_jobs
    return get_all_jobs()
