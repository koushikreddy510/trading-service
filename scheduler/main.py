"""
Central scheduler for trading pipeline.
Runs all cron-like jobs via HTTP (screener, trading-service) or subprocess (data-store).
Uses APScheduler with YAML config. Designed for single-EC2 Docker deployment.
"""
import os
import logging
import subprocess
from contextlib import asynccontextmanager

import requests
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.responses import JSONResponse

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


def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        logger.warning(f"Config not found: {CONFIG_PATH}, using defaults")
        return {"timezone": "Asia/Kolkata", "jobs": []}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _run_http_job(job_id: str, url: str, method: str = "POST"):
    """Trigger an HTTP endpoint. Log result."""
    try:
        fn = requests.post if method.upper() == "POST" else requests.get
        r = fn(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        JOB_LOG[job_id] = {"status": "ok", "status_code": r.status_code}
        logger.info(f"Job {job_id} OK: {url}")
    except Exception as e:
        JOB_LOG[job_id] = {"status": "error", "error": str(e)}
        logger.error(f"Job {job_id} failed: {e}")


def _run_subprocess_job(job_id: str, cmd: list, cwd: str = None):
    """Run a subprocess (e.g. data-store script). Log result."""
    cwd = cwd or DATA_STORE_DIR
    if not os.path.isdir(cwd):
        JOB_LOG[job_id] = {"status": "error", "error": f"Directory not found: {cwd}"}
        logger.error(f"Job {job_id}: DATA_STORE_DIR not found")
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
            JOB_LOG[job_id] = {"status": "ok", "returncode": 0}
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


scheduler = BackgroundScheduler(timezone="Asia/Kolkata")


def _register_jobs(config: dict):
    jobs = config.get("jobs", [])
    tz = config.get("timezone", "Asia/Kolkata")
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

        jtype = job.get("type", "http")
        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
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
    scheduler.start()
    logger.info("Scheduler started")
    yield
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


app = FastAPI(title="Trading Pipeline Scheduler", version="1.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "scheduler": scheduler.running}


@app.get("/jobs")
def list_jobs():
    jobs = []
    for j in scheduler.get_jobs():
        jobs.append({
            "id": j.id,
            "name": j.name,
            "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
            "trigger": str(j.trigger),
        })
    return {"jobs": jobs}


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
