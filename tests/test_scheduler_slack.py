"""The scheduler tells Slack how every job went. (The scheduler is its own container, so it is loaded by path.)"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("apscheduler")
pytest.importorskip("yaml")


@pytest.fixture
def sched(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("scheduler_main", Path(__file__).resolve().parent.parent / "scheduler" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sent = []
    monkeypatch.setattr(mod, "RUNS_DB", str(tmp_path / "runs.db"))   # never write a real history file from a test
    monkeypatch.setattr(mod, "SLACK_WEBHOOK_URL", "https://hooks.example/x")
    monkeypatch.setattr(mod.requests, "post", lambda url, json=None, timeout=None: sent.append((url, json["text"])) or SimpleNamespace(status_code=200))
    mod.sent = sent
    return mod


def reply(text, status=200):
    class R:
        status_code = status
        def raise_for_status(self):
            if status >= 400:
                raise RuntimeError(f"{status} Server Error")
    r = R()
    r.text = text
    return r


def test_a_finished_job_reports_what_it_returned(sched, monkeypatch):
    sched.scheduler.add_job(lambda: None, id="scan", name="Terminal screener scan", trigger="interval", hours=1)
    monkeypatch.setattr(sched.requests, "get", lambda url, timeout=None: reply('{"run_date": "2026-10-19", "stored": 1800}'))
    sched._run_http_job("scan", "http://x/scan", "GET")
    ((url, text),) = sched.sent
    assert url == "https://hooks.example/x"
    assert text.startswith("✅ *Terminal screener scan* (`scan`) finished in ") and '{"run_date":"2026-10-19","stored":1800}' in text
    assert sched.JOB_LOG["scan"]["status"] == "ok" and "seconds" in sched.JOB_LOG["scan"]


def test_a_failed_job_says_why(sched, monkeypatch):
    monkeypatch.setattr(sched.requests, "post", lambda url, json=None, timeout=None: reply("oops", 500) if json is None else sched.sent.append((url, json["text"])))
    sched._run_http_job("ohlcv", "http://x/run")
    ((_, text),) = sched.sent
    assert text.startswith("❌ *ohlcv* (`ohlcv`) FAILED") and "500 Server Error" in text


def test_only_failures_mode_stays_quiet_on_success(sched, monkeypatch):
    monkeypatch.setattr(sched, "SLACK_JOBS_NOTIFY", "failures")
    monkeypatch.setattr(sched.requests, "get", lambda url, timeout=None: reply("{}"))
    sched._run_http_job("quiet", "http://x", "GET")
    assert sched.sent == []


def test_a_job_can_ask_for_failures_only(sched, monkeypatch):
    monkeypatch.setitem(sched.JOB_SLACK, "sync", "failures")
    monkeypatch.setattr(sched.requests, "get", lambda url, timeout=None: reply("{}"))
    sched._run_http_job("sync", "http://x", "GET")
    assert sched.sent == []
    monkeypatch.setattr(sched.requests, "get", lambda url, timeout=None: reply("down", 503))
    sched._run_http_job("sync", "http://x", "GET")
    assert sched.sent[0][1].startswith("❌ *sync*")


def test_frequent_and_self_reporting_jobs_only_report_failures():
    import yaml
    jobs = {j["id"]: j for j in yaml.safe_load((Path(__file__).resolve().parent.parent / "scheduler" / "schedules.yaml").read_text())["jobs"]}
    for jid in ("sync_positions", "broker_tokens_check", "terminal_screener_sync", "terminal_data_digest"):
        assert jobs[jid]["slack"] == "failures"


def test_a_missed_job_is_reported(sched):
    from datetime import datetime
    sched._on_missed(SimpleNamespace(job_id="digest", scheduled_run_time=datetime(2026, 10, 19, 21, 0)))
    assert "did not run at its scheduled time (2026-10-19 21:00)" in sched.sent[0][1]


def test_without_a_webhook_nothing_is_sent_and_the_job_still_runs(sched, monkeypatch):
    monkeypatch.setattr(sched, "SLACK_WEBHOOK_URL", None)
    monkeypatch.setattr(sched.requests, "get", lambda url, timeout=None: reply("{}"))
    sched._run_http_job("x", "http://x", "GET")
    assert sched.sent == [] and sched.JOB_LOG["x"]["status"] == "ok"


def test_the_terminal_jobs_are_scheduled():
    import yaml
    jobs = {j["id"]: j for j in yaml.safe_load((Path(__file__).resolve().parent.parent / "scheduler" / "schedules.yaml").read_text())["jobs"]}
    assert jobs["terminal_screener_scan"]["url"] == "/terminal/screener/scan"
    assert jobs["terminal_screener_sync"]["url"] == "/terminal/screener-sync/refresh"
    assert jobs["terminal_data_digest"]["url"] == "/terminal/notify/data-digest" and jobs["terminal_data_digest"]["service"] == "trading"
