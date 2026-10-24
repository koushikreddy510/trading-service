"""
Central scheduler for trading pipeline.
Runs all cron-like jobs via HTTP (screener, trading-service) or subprocess (data-store).
Uses APScheduler with YAML config. Designed for single-EC2 Docker deployment.
"""
import re
import html
import json
import os
import logging
import sqlite3
import subprocess
import time
import zoneinfo
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta

import requests
import yaml
from apscheduler.events import EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")

CONFIG_PATH = os.getenv("SCHEDULER_CONFIG", "/app/schedules.yaml")
SCREENER_URL = os.getenv("SCREENER_URL", "http://strategy-screener:8000")
TRADING_URL = os.getenv("TRADING_URL", "http://trading-service:8001")
DATA_STORE_URL = os.getenv("DATA_STORE_URL", "http://data-store:8003")
DATA_STORE_DIR = os.getenv("DATA_STORE_DIR", "/app/data-store")
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "600"))  # 10 min for long jobs
JOB_LOG = {}  # in-memory job log (last run per job_id)
SLACK_WEBHOOK_URL = os.getenv("SLACK_JOBS_WEBHOOK_URL") or os.getenv("SLACK_WEBHOOK_URL") or os.getenv("TRADING_SLACK_WEBHOOK_URL")
SLACK_JOBS_NOTIFY = os.getenv("SLACK_JOBS_NOTIFY", "all").lower()  # all | failures | off; a job's `slack:` overrides it
JOB_SLACK = {}  # job_id -> its own `slack:` setting from schedules.yaml
RUNS_DB = os.getenv("JOB_RUNS_DB", "/data/job_runs.db")  # run history; a docker volume keeps it across restarts and deploys
RUNS_KEEP = 2000                                         # newest rows kept; older ones are deleted on write



_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def cron_weekdays(field: str) -> str:
    """The weekday field of a normal cron line, written for APScheduler.

    In cron 0 is Sunday and 1 is Monday; APScheduler counts 0 as Monday, so a plain "1-5" would run Tuesday to
    Saturday (the Saturday 07:45 login that gave this away, and no Monday login). Digits become day names; a step
    such as */2 and names written out are left alone."""
    return re.sub(r"(?<![/\d])\d", lambda m: _DAYS[int(m.group()) % 7], field)

def _slack(text: str) -> None:
    """Best effort: a Slack outage must never affect a job."""
    if not SLACK_WEBHOOK_URL:
        return
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=5)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Slack notification failed: {e}")


def _brief(text: str, limit: int = 400) -> str:
    """A response body as one short line (compact JSON when it is JSON)."""
    try:
        text = json.dumps(json.loads(text), separators=(",", ":"), ensure_ascii=False)
    except ValueError:
        text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _runs_db():
    os.makedirs(os.path.dirname(RUNS_DB) or ".", exist_ok=True)
    con = sqlite3.connect(RUNS_DB, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS job_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, name TEXT, "
                "started_at TEXT NOT NULL, seconds REAL, status TEXT NOT NULL, detail TEXT)")
    return con


def _record_run(job_id: str, name: str, entry: dict, detail) -> None:
    """Keep the run for the history views. Best effort: a full disk or missing volume must never affect a job."""
    try:
        started = datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata")) - timedelta(seconds=entry.get("seconds") or 0)
        with closing(_runs_db()) as con, con:
            con.execute("INSERT INTO job_runs (job_id, name, started_at, seconds, status, detail) VALUES (?, ?, ?, ?, ?, ?)",
                        (job_id, name, started.isoformat(timespec="seconds"), entry.get("seconds"), entry["status"], _brief(str(detail or ""), 400)))
            con.execute("DELETE FROM job_runs WHERE id <= (SELECT max(id) FROM job_runs) - ?", (RUNS_KEEP,))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not record run of {job_id}: {e}")


def _runs(job_id: str = None, limit: int = 50) -> list[dict]:
    try:
        with closing(_runs_db()) as con:
            sql, args = "SELECT job_id, name, started_at, seconds, status, detail FROM job_runs", []
            if job_id:
                sql, args = sql + " WHERE job_id = ?", [job_id]
            return [dict(r) for r in con.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, max(1, min(limit, 500))])]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not read run history: {e}")
        return []


def _report(job_id: str, started: float) -> None:
    """Record how long the job took and tell Slack how it went (what it returned, or why it failed)."""
    entry = JOB_LOG[job_id]
    entry["seconds"] = round(time.monotonic() - started, 1)
    ok = entry["status"] == "ok"
    job = scheduler.get_job(job_id)
    detail = entry.get("result") if ok else (entry.get("error") or entry.get("stderr") or f"exit {entry.get('returncode')}")
    _record_run(job_id, job.name if job else job_id, entry, detail)
    notify = JOB_SLACK.get(job_id, SLACK_JOBS_NOTIFY)
    if notify == "off" or (ok and notify == "failures"):
        return
    _slack(f"{'✅' if ok else '❌'} *{job.name if job else job_id}* (`{job_id}`) {'finished' if ok else 'FAILED'} in {entry['seconds']}s"
           + (f"\n```{detail}```" if detail else ""))


def _on_missed(event) -> None:
    job = scheduler.get_job(event.job_id)
    _slack(f"⏰ *{job.name if job else event.job_id}* (`{event.job_id}`) did not run at its scheduled time ({event.scheduled_run_time:%Y-%m-%d %H:%M}).")


def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        logger.warning(f"Config not found: {CONFIG_PATH}, using defaults")
        return {"timezone": "Asia/Kolkata", "jobs": []}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _run_http_job(job_id: str, url: str, method: str = "POST"):
    """Trigger an HTTP endpoint. Log result."""
    started = time.monotonic()
    try:
        fn = requests.post if method.upper() == "POST" else requests.get
        r = fn(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        JOB_LOG[job_id] = {"status": "ok", "status_code": r.status_code, "result": _brief(r.text)}
        logger.info(f"Job {job_id} OK: {url}")
    except Exception as e:
        JOB_LOG[job_id] = {"status": "error", "error": str(e)}
        logger.error(f"Job {job_id} failed: {e}")
    _report(job_id, started)


def _run_subprocess_job(job_id: str, cmd: list, cwd: str = None):
    """Run a subprocess (e.g. data-store script). Log result."""
    cwd = cwd or DATA_STORE_DIR
    started = time.monotonic()
    if not os.path.isdir(cwd):
        JOB_LOG[job_id] = {"status": "error", "error": f"Directory not found: {cwd}"}
        logger.error(f"Job {job_id}: DATA_STORE_DIR not found")
        _report(job_id, started)
        return
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3600,
            env=os.environ.copy(),
        )
        if result.returncode == 0:
            JOB_LOG[job_id] = {"status": "ok", "returncode": 0, "result": _brief((result.stdout or "")[-400:])}
            logger.info(f"Job {job_id} OK: {' '.join(cmd)}")
        else:
            JOB_LOG[job_id] = {
                "status": "error",
                "returncode": result.returncode,
                "stderr": (result.stderr or "")[:500],
            }
            logger.error(f"Job {job_id} failed (exit {result.returncode}): {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        JOB_LOG[job_id] = {"status": "error", "error": "Timeout (3600s)"}
        logger.error(f"Job {job_id} timed out")
    except Exception as e:
        JOB_LOG[job_id] = {"status": "error", "error": str(e)}
        logger.error(f"Job {job_id} failed: {e}")
    _report(job_id, started)


scheduler = BackgroundScheduler(timezone=zoneinfo.ZoneInfo("Asia/Kolkata"))


def _register_jobs(config: dict):
    jobs = config.get("jobs", [])
    tz = zoneinfo.ZoneInfo(config.get("timezone", "Asia/Kolkata"))
    scheduler.timezone = tz

    for job in jobs:
        jid = job.get("id")
        if not jid:
            continue
        cron = job.get("schedule")
        if not cron:
            continue

        parts = cron.strip().split()
        if len(parts) != 5:
            logger.warning(f"Job {jid}: invalid cron '{cron}', skip")
            continue

        if job.get("slack"):
            JOB_SLACK[jid] = str(job["slack"]).lower()
        jtype = job.get("type", "http")
        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=cron_weekdays(parts[4]),
            timezone=tz,
        )

        if jtype == "http":
            url = job.get("url")
            if not url:
                logger.warning(f"Job {jid}: missing url, skip")
                continue
            # Resolve URL base for relative paths
            if url.startswith("/"):
                svc = job.get("service", "screener")
                base = (
                    SCREENER_URL
                    if svc == "screener"
                    else (DATA_STORE_URL if svc == "data_store" else TRADING_URL)
                )
                url = f"{base.rstrip('/')}{url}"
            method = job.get("method", "POST")
            scheduler.add_job(
                _run_http_job,
                trigger=trigger,
                id=jid,
                name=job.get("name", jid),
                replace_existing=True,
                args=[jid, url],
                kwargs={"method": method},
            )
        elif jtype == "subprocess":
            cmd = job.get("cmd")
            if not cmd:
                logger.warning(f"Job {jid}: missing cmd, skip")
                continue
            cwd = job.get("cwd") or DATA_STORE_DIR
            scheduler.add_job(
                _run_subprocess_job,
                trigger=trigger,
                id=jid,
                name=job.get("name", jid),
                replace_existing=True,
                args=[jid, cmd],
                kwargs={"cwd": cwd},
            )
        else:
            logger.warning(f"Job {jid}: unknown type '{jtype}', skip")

        logger.info(f"Registered job: {jid} at {cron} ({jtype})")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = _load_config()
    _register_jobs(config)
    scheduler.add_listener(_on_missed, EVENT_JOB_MISSED)
    scheduler.start()
    logger.info("Scheduler started")
    _slack(f"🟢 *Scheduler started* with {len(scheduler.get_jobs())} jobs.")
    yield
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


app = FastAPI(title="Trading Pipeline Scheduler", version="1.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "scheduler": scheduler.running}


@app.get("/jobs")
def list_jobs():
    last = {}
    for r in reversed(_runs(limit=500)):          # oldest first, so the newest run of each job wins
        last[r["job_id"]] = r
    jobs = []
    for j in scheduler.get_jobs():
        jobs.append({
            "id": j.id,
            "name": j.name,
            "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
            "trigger": str(j.trigger),
            "last_run": last.get(j.id),
        })
    return {"jobs": jobs}


@app.get("/runs")
def list_runs(job: str = None, limit: int = 50):
    """Run history, newest first (kept across restarts)."""
    return {"runs": _runs(job, limit)}


@app.get("/ui", response_class=HTMLResponse)
def ui():
    """One page: every job with its next run and last result, then the latest runs."""
    e = lambda v: html.escape("" if v is None else str(v))  # noqa: E731
    jobs = sorted(list_jobs()["jobs"], key=lambda j: j["next_run"] or "9")
    rows = "".join(
        f"<tr><td>{e(j['name'])}<br><small>{e(j['id'])}</small></td><td>{e((j['next_run'] or '-').replace('T', ' ')[:16])}</td>"
        f"<td class='{e(j['last_run']['status']) if j['last_run'] else ''}'>{e(j['last_run']['status']) if j['last_run'] else 'never run'}</td>"
        f"<td>{e(j['last_run']['started_at'].replace('T', ' ')) if j['last_run'] else ''}</td>"
        f"<td>{e(j['last_run']['seconds']) if j['last_run'] else ''}</td></tr>" for j in jobs)
    hist = "".join(
        f"<tr><td>{e(r['started_at'].replace('T', ' '))}</td><td>{e(r['name'])}</td><td class='{e(r['status'])}'>{e(r['status'])}</td>"
        f"<td>{e(r['seconds'])}</td><td><small>{e(r['detail'])}</small></td></tr>" for r in _runs(limit=50))
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Scheduler</title>"
        "<style>body{font:14px system-ui;margin:16px;max-width:1000px}table{border-collapse:collapse;width:100%;margin-bottom:24px}"
        "td,th{border-bottom:1px solid #8884;padding:6px 8px;text-align:left;vertical-align:top}.ok{color:#18a058}.error{color:#d03050;font-weight:600}</style>"
        "<h2>Scheduled jobs (IST)</h2><table><tr><th>Job<th>Next run<th>Last status<th>Last started<th>Seconds</tr>" + rows + "</table>"
        "<h2>Recent runs</h2><table><tr><th>Started<th>Job<th>Status<th>Seconds<th>Result</tr>" + hist + "</table>"
    )


@app.get("/jobs/{job_id}/log")
def job_log(job_id: str):
    return JOB_LOG.get(job_id, {"status": "never_run"})


@app.post("/jobs/{job_id}/run")
def run_job_now(job_id: str):
    """Manually trigger a job once."""
    job = scheduler.get_job(job_id)
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    try:
        job.func(*job.args, **job.kwargs)
        return {"status": "triggered", "log": JOB_LOG.get(job_id, {})}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/")
def root():
    return {
        "service": "trading-pipeline-scheduler",
        "screener_url": SCREENER_URL,
        "trading_url": TRADING_URL,
        "docs": "/docs",
    }
