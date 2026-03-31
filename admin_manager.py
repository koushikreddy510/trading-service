"""
Admin / config for trading-service: data status, sync jobs (positions, PnL).
Similar pattern to strategy-screener's data_manager.py.
"""
import os
import threading
from datetime import date, datetime
from typing import Dict, Any, Optional

from trading_service.db import (
    get_accounts,
    get_admin_status,
    replace_positions,
    upsert_pnl_snapshot,
)

_running_jobs: Dict[str, Dict[str, Any]] = {}
_job_lock = threading.Lock()


def _job_status(job_id: str) -> dict:
    with _job_lock:
        j = _running_jobs.get(job_id)
        if not j:
            return {"job_id": job_id, "status": "not_found"}
        out = {
            "job_id": job_id,
            "status": j["status"],
            "started_at": j.get("started_at"),
            "finished_at": j.get("finished_at"),
            "progress": j.get("progress", ""),
            "error": j.get("error"),
        }
        if "result" in j:
            out["result"] = j["result"]
        return out


def _run_in_thread(job_id: str, fn, *args, **kwargs):
    def wrapper():
        with _job_lock:
            _running_jobs[job_id]["status"] = "running"
        try:
            ret = fn(*args, **kwargs)
            with _job_lock:
                _running_jobs[job_id]["status"] = "completed"
                _running_jobs[job_id]["finished_at"] = datetime.now().isoformat()
                if ret is not None:
                    _running_jobs[job_id]["result"] = ret
        except Exception as e:
            with _job_lock:
                _running_jobs[job_id]["status"] = "failed"
                _running_jobs[job_id]["error"] = str(e)
                _running_jobs[job_id]["finished_at"] = datetime.now().isoformat()

    with _job_lock:
        if job_id in _running_jobs and _running_jobs[job_id]["status"] == "running":
            return {"job_id": job_id, "status": "already_running"}
        _running_jobs[job_id] = {
            "status": "starting",
            "started_at": datetime.now().isoformat(),
            "finished_at": None,
            "progress": "",
            "error": None,
            "result": None,
        }

    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return {"job_id": job_id, "status": "started"}


def get_data_status() -> dict:
    """Overview: accounts, positions, PnL snapshots, last sync times."""
    try:
        status = get_admin_status()
    except Exception as e:
        return {"error": str(e), "accounts": [], "positions": {}, "pnl": {}, "orders": {}, "config": {}}

    accounts = status["accounts"]
    status["config"] = {
        "brokers_configured": sorted(set(a["broker"] for a in accounts)),
        "env_database": "set" if os.getenv("DATABASE_URL") else "not set",
        "env_slack": "set" if os.getenv("SLACK_WEBHOOK_URL") or os.getenv("TRADING_SLACK_WEBHOOK_URL") else "not set",
    }
    return status


def _sync_positions_job(account_id: Optional[int] = None):
    """Fetch positions from broker APIs and write to DB."""
    from trading_service.adapters import get_adapter

    accounts = get_admin_status()["accounts"]
    if account_id is not None:
        accounts = [a for a in accounts if a["id"] == account_id]
    if not accounts:
        with _job_lock:
            if "sync_positions" in _running_jobs:
                _running_jobs["sync_positions"]["progress"] = "No active accounts to sync"
        return

    total_synced = 0
    errors = []

    for i, acc in enumerate(accounts):
        aid, broker, acc_id, display_name = acc["id"], acc["broker"], acc["account_id"], acc.get("display_name", "")
        cred_ref = acc.get("credentials_ref", "") or "default"
        with _job_lock:
            if "sync_positions" in _running_jobs:
                _running_jobs["sync_positions"]["progress"] = f"[{i+1}/{len(accounts)}] {display_name or acc_id} ({broker})"

        try:
            AdapterClass = get_adapter(broker)
            adapter = AdapterClass()
            positions_raw = adapter.get_positions(acc_id, cred_ref)
        except Exception as e:
            errors.append(f"{display_name or acc_id}: {e}")
            continue

        positions = [
            {
                "symbol": p.symbol,
                "exchange": p.exchange,
                "side": p.side,
                "quantity": p.quantity,
                "avg_price": p.avg_price,
                "current_price": p.current_price,
                "unrealized_pnl": p.unrealized_pnl,
            }
            for p in positions_raw
        ]
        try:
            cnt = replace_positions(aid, positions)
            total_synced += cnt
        except Exception as e:
            errors.append(f"{display_name or acc_id} DB: {e}")

    with _job_lock:
        if "sync_positions" in _running_jobs:
            msg = f"Done: {total_synced} positions across {len(accounts)} accounts"
            if errors:
                msg += f". Errors: {'; '.join(errors[:3])}"
            _running_jobs["sync_positions"]["progress"] = msg


def start_sync_positions(account_id: Optional[int] = None) -> dict:
    return _run_in_thread("sync_positions", _sync_positions_job, account_id)


def _sync_pnl_job():
    """Create today's PnL snapshot from current positions (unrealized) and optional realized from orders."""
    today = date.today()
    accounts = get_accounts(active_only=True)

    with _job_lock:
        if "sync_pnl" in _running_jobs:
            _running_jobs["sync_pnl"]["progress"] = f"Computing PnL for {len(accounts)} accounts..."

    from trading_service.db import _conn
    from sqlalchemy import text

    with _conn() as conn:
        for acc in accounts:
            aid = acc["id"]
            row = conn.execute(text(
                "SELECT COALESCE(SUM(unrealized_pnl), 0) FROM trading_positions WHERE account_id = :aid"
            ), {"aid": aid}).fetchone()
            unrealized = float(row[0]) if row else 0
            realized = 0  # Placeholder; can be enhanced with broker API or trade history
            total = realized + unrealized
            upsert_pnl_snapshot(aid, today, realized, unrealized, total)

    with _job_lock:
        if "sync_pnl" in _running_jobs:
            _running_jobs["sync_pnl"]["progress"] = f"Done: PnL snapshots for {len(accounts)} accounts ({today})"


def start_sync_pnl() -> dict:
    return _run_in_thread("sync_pnl", _sync_pnl_job)


def _entry_candidate_scan_job(account_id: Optional[int] = None):
    from trading_service.strategy_consumer import run_entry_candidate_scan

    result = run_entry_candidate_scan(account_id=account_id)
    n = sum(r.get("candidate_symbol_count", 0) for r in result.get("rows", []))
    with _job_lock:
        if "entry_candidate_scan" in _running_jobs:
            _running_jobs["entry_candidate_scan"]["progress"] = (
                f"{result.get('strategy_runs', 0)} enabled strategy runs, {n} total candidates (not held)"
            )
    return result


def start_entry_candidate_scan(account_id: Optional[int] = None) -> dict:
    """Async job: enabled strategies × accounts → universe minus open symbols (no orders)."""
    return _run_in_thread("entry_candidate_scan", _entry_candidate_scan_job, account_id)


def get_job_status(job_id: str) -> dict:
    return _job_status(job_id)


def get_all_jobs() -> dict:
    with _job_lock:
        return {
            k: {
                "status": v["status"],
                "started_at": v.get("started_at"),
                "finished_at": v.get("finished_at"),
                "progress": v.get("progress", ""),
                "error": v.get("error"),
            }
            for k, v in _running_jobs.items()
        }
