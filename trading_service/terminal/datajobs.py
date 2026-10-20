"""Data health and the data jobs, for the terminal's Data screen.

The scrapers themselves live in the strategy-screener / data-store repos (screener.in financials, NSE results and sectors,
the Fyers symbol master and daily candles). This module neither scrapes nor copies them: it READS the market tables to
say how fresh and how complete the data is, and it STARTS the existing jobs over the strategy-screener's admin API.
"""
from __future__ import annotations

from typing import Any, Optional

from .db import Database
from .screener_sync import ScreenerUnavailable, SyncError

CHANGES_SHOWN = 100

# id -> what it does, the screener admin endpoint that runs it, and the parameters a caller may pass (with their allowed values)
JOBS: dict[str, dict[str, Any]] = {
    "symbols": {"label": "Sync symbol master", "path": "/admin/sync-symbols", "params": {"mode": ["full", "check"]},
                "help": "Downloads the broker's symbol list. 'check' only reports what would change."},
    "ohlcv": {"label": "Daily candles (1D)", "path": "/admin/update-ohlcv", "params": {"mode": ["incremental", "full"]},
              "help": "Fetches 1D candles for every stock. Needs a valid Fyers token in the strategy-screener."},
    "financials": {"label": "Financials (screener.in)", "path": "/admin/update-financials",
                   "params": {"mode": ["incremental", "announcements_only", "full"], "limit": (0, 5000)},
                   "help": "Quarterly and yearly results, about one request a second. 'announcements_only' is the quick one."},
    "latest_results": {"label": "Latest results (NSE)", "path": "/admin/scrape-latest-results", "params": {"days": (1, 90)},
                       "help": "Which companies announced results in the last N days."},
    "screener_latest": {"label": "Latest results (screener.in)", "path": "/admin/scrape-screener-latest", "params": {},
                        "help": "The latest-results page of screener.in."},
    "sectors": {"label": "Sectors and market cap (NSE)", "path": "/admin/enrich-sectors", "params": {},
                "help": "Fills in the industry and market cap of stocks that have none, which sector analysis depends on."},
}


def catalog() -> list[dict]:
    return [{"id": k, "label": v["label"], "help": v["help"], "params": v["params"]} for k, v in JOBS.items()]


def data_status(db: Database) -> dict:
    """How much data there is and how fresh. Read-only (the Slack digest is the one that updates the symbol snapshot)."""
    out: dict[str, Any] = {}
    if db.table_exists("symbols"):
        eq = "tradingsymbol LIKE '%-EQ'"
        total = db.scalar(f"SELECT count(*) FROM symbols WHERE {eq}")
        out["symbols"] = {
            "total": total,
            "with_sector": db.scalar(f"SELECT count(*) FROM symbols WHERE {eq} AND COALESCE(TRIM(sector), '') <> ''"),
            "with_market_cap": db.scalar(f"SELECT count(*) FROM symbols WHERE {eq} AND COALESCE(market_cap, 0) > 0"),
        }
        now = {r["tradingsymbol"] for r in db.rows("SELECT tradingsymbol FROM symbols")}
        before = {r["symbol"] for r in db.rows("SELECT symbol FROM terminal_symbol_snapshot")} if db.table_exists("terminal_symbol_snapshot") else set()
        out["changes"] = None if not before else {"added": sorted(now - before)[:CHANGES_SHOWN], "removed": sorted(before - now)[:CHANGES_SHOWN],
                                                   "added_total": len(now - before), "removed_total": len(before - now)}
    if db.table_exists("ohlcv_1d"):
        latest = db.scalar("SELECT max(date) FROM ohlcv_1d")
        out["ohlcv_1d"] = {
            "symbols": db.scalar("SELECT count(DISTINCT tradingsymbol) FROM ohlcv_1d"), "latest": _iso(latest), "oldest": _iso(db.scalar("SELECT min(date) FROM ohlcv_1d")),
            "symbols_on_latest": db.scalar("SELECT count(*) FROM ohlcv_1d WHERE date = :d", d=latest) if latest else 0,
        }
    if db.table_exists("stock_financials"):
        out["financials"] = {"companies": db.scalar("SELECT count(DISTINCT tradingsymbol) FROM stock_financials"), "latest_scraped": _iso(db.scalar("SELECT max(scraped_at) FROM stock_financials"))}
    if db.table_exists("result_announcements"):
        out["results"] = {"total": db.scalar("SELECT count(*) FROM result_announcements"), "latest": _iso(db.scalar("SELECT max(result_date) FROM result_announcements")),
                          "last_7_days": db.scalar("SELECT count(*) FROM result_announcements WHERE result_date >= current_date - 7")}
    return out


def _iso(d: Any) -> Optional[str]:
    return str(d)[:19] if d else None


def clean_params(job: str, raw: dict) -> dict:
    """Only the parameters this job takes, each checked against what it allows. Anything else is an error, not ignored."""
    spec = JOBS.get(job)
    if spec is None:
        raise SyncError(f"unknown job '{job}'")
    out: dict[str, Any] = {}
    for name, allowed in spec["params"].items():
        if raw.get(name) in (None, ""):
            continue
        value = raw[name]
        if isinstance(allowed, tuple):
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise SyncError(f"{name} must be a whole number") from None
            if not allowed[0] <= value <= allowed[1]:
                raise SyncError(f"{name} must be between {allowed[0]} and {allowed[1]}")
        elif value not in allowed:
            raise SyncError(f"{name} must be one of {', '.join(allowed)}")
        out[name] = value
    extra = set(raw) - set(spec["params"]) - {"job"}
    if extra:
        raise SyncError(f"{job} does not take {', '.join(sorted(extra))}")
    return out


def run_job(client: Any, job: str, raw: dict) -> dict:
    """Start a job in the strategy-screener. Its answer ({job_id, status}) is returned as it is."""
    params = clean_params(job, raw)
    result = client.post(JOBS[job]["path"], **params)
    if isinstance(result, dict) and result.get("error"):
        raise ScreenerUnavailable(f"{JOBS[job]['label']} failed to start: {result['error']}")
    return result


def running_jobs(client: Any) -> dict:
    """The screener's job table: name -> {status, started_at, finished_at, progress, error}."""
    return client.get("/admin/jobs")
