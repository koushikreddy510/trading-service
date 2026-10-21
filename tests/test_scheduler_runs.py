"""The scheduler keeps a run history and shows it: what is scheduled, what ran, what failed.
Everything that existed before (Slack messages, /jobs, /jobs/<id>/log, manual runs) must behave exactly as it did."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("apscheduler")
pytest.importorskip("yaml")
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

SCHEDULER_DIR = Path(__file__).resolve().parent.parent / "scheduler"


def load(monkeypatch, db):
    spec = importlib.util.spec_from_file_location("scheduler_main", SCHEDULER_DIR / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "RUNS_DB", str(db))
    sent = []
    monkeypatch.setattr(mod, "SLACK_WEBHOOK_URL", "https://hooks.example/x")
    monkeypatch.setattr(mod.requests, "post", lambda url, json=None, timeout=None: sent.append(json["text"]) or SimpleNamespace(status_code=200))
    mod.sent = sent
    return mod


@pytest.fixture
def sched(monkeypatch, tmp_path):
    return load(monkeypatch, tmp_path / "runs.db")


def reply(text, status=200):
    class R:
        def raise_for_status(self):
            if status >= 400:
                raise RuntimeError(f"{status} Server Error")
    r = R()
    r.status_code, r.text = status, text
    return r


def ok(sched, monkeypatch, body='{"stored":5}'):
    monkeypatch.setattr(sched.requests, "get", lambda url, timeout=None: reply(body))


class TestHistory:
    def test_a_finished_job_is_recorded_with_its_result(self, sched, monkeypatch):
        sched.scheduler.add_job(lambda: None, id="scan", name="Terminal scan", trigger="interval", hours=1)
        ok(sched, monkeypatch)
        sched._run_http_job("scan", "http://x", "GET")
        (r,) = sched._runs()
        assert (r["job_id"], r["name"], r["status"], r["detail"]) == ("scan", "Terminal scan", "ok", '{"stored":5}')
        assert r["seconds"] is not None and r["started_at"].endswith("+05:30")

    def test_a_failed_job_is_recorded_with_the_reason(self, sched, monkeypatch):
        monkeypatch.setattr(sched.requests, "post", lambda url, json=None, timeout=None: reply("x", 500) if json is None else None)
        sched._run_http_job("ohlcv", "http://x/run")
        (r,) = sched._runs()
        assert r["status"] == "error" and "500 Server Error" in r["detail"] and r["name"] == "ohlcv"

    def test_a_subprocess_job_is_recorded(self, sched, tmp_path):
        sched._run_subprocess_job("script", [sys.executable, "-c", "print('hello')"], cwd=str(tmp_path))
        (r,) = sched._runs()
        assert r["status"] == "ok" and "hello" in r["detail"]

    def test_history_survives_a_restart(self, sched, monkeypatch, tmp_path):
        ok(sched, monkeypatch)
        sched._run_http_job("scan", "http://x", "GET")
        again = load(monkeypatch, tmp_path / "runs.db")      # a fresh process: JOB_LOG is empty, the file is not
        assert again.JOB_LOG == {} and [r["job_id"] for r in again._runs()] == ["scan"]

    def test_newest_first_and_filtered_by_job(self, sched, monkeypatch):
        ok(sched, monkeypatch)
        for j in ("a", "b", "a"):
            sched._run_http_job(j, "http://x", "GET")
        assert [r["job_id"] for r in sched._runs()] == ["a", "b", "a"]
        assert len(sched._runs("a")) == 2 and sched._runs("zzz") == []

    def test_only_the_newest_rows_are_kept(self, sched, monkeypatch):
        monkeypatch.setattr(sched, "RUNS_KEEP", 3)
        ok(sched, monkeypatch)
        for i in range(5):
            sched._run_http_job(f"j{i}", "http://x", "GET")
        assert [r["job_id"] for r in sched._runs()] == ["j4", "j3", "j2"]

    def test_the_limit_is_kept_sane(self, sched, monkeypatch):
        ok(sched, monkeypatch)
        for _ in range(3):
            sched._run_http_job("a", "http://x", "GET")
        assert len(sched._runs(limit=0)) == 1 and len(sched._runs(limit=10**6)) == 3

    @pytest.mark.parametrize("mode", ["off", "failures"])
    def test_a_quiet_slack_mode_still_records_the_run(self, sched, monkeypatch, mode):
        monkeypatch.setattr(sched, "SLACK_JOBS_NOTIFY", mode)
        ok(sched, monkeypatch)
        sched._run_http_job("scan", "http://x", "GET")
        assert sched.sent == [] and len(sched._runs()) == 1


class TestNothingBreaks:
    def test_a_history_file_that_cannot_be_written_never_affects_the_job_or_slack(self, sched, monkeypatch, tmp_path):
        (tmp_path / "file").write_text("not a directory")
        monkeypatch.setattr(sched, "RUNS_DB", str(tmp_path / "file" / "runs.db"))
        ok(sched, monkeypatch)
        sched._run_http_job("scan", "http://x", "GET")
        assert sched.JOB_LOG["scan"]["status"] == "ok" and len(sched.sent) == 1 and sched._runs() == []

    def test_the_job_log_entry_has_the_same_shape_as_before(self, sched, monkeypatch):
        ok(sched, monkeypatch)
        sched._run_http_job("scan", "http://x", "GET")
        assert set(sched.JOB_LOG["scan"]) == {"status", "status_code", "result", "seconds"}

    def test_the_slack_message_is_unchanged(self, sched, monkeypatch):
        sched.scheduler.add_job(lambda: None, id="scan", name="Terminal scan", trigger="interval", hours=1)
        ok(sched, monkeypatch)
        sched._run_http_job("scan", "http://x", "GET")
        (text,) = sched.sent
        assert text.startswith("✅ *Terminal scan* (`scan`) finished in ") and '{"stored":5}' in text

    def test_the_real_schedule_file_still_registers_every_job_in_ist(self, sched):
        import yaml
        cfg = yaml.safe_load((SCHEDULER_DIR / "schedules.yaml").read_text())
        sched._register_jobs(cfg)
        ids = {j.id for j in sched.scheduler.get_jobs()}
        assert ids == {j["id"] for j in cfg["jobs"]} and {"fyers_token_refresh", "broker_tokens_check"} <= ids
        assert str(sched.scheduler.timezone) == "Asia/Kolkata"


class TestEndpoints:
    @pytest.fixture
    def client(self, sched, monkeypatch):
        monkeypatch.setattr(sched, "CONFIG_PATH", str(SCHEDULER_DIR / "schedules.yaml"))
        with TestClient(sched.app) as c:
            c.sched = sched
            yield c

    def test_the_old_endpoints_answer_as_before(self, client):
        assert client.get("/health").json() == {"status": "ok", "scheduler": True}
        assert client.get("/jobs/scan/log").json() == {"status": "never_run"}
        assert client.post("/jobs/nope/run").status_code == 404
        assert client.get("/").json()["service"] == "trading-pipeline-scheduler"
        job = client.get("/jobs").json()["jobs"][0]
        assert {"id", "name", "next_run", "trigger"} <= set(job)

    def test_jobs_show_their_last_run_or_none(self, client, monkeypatch):
        monkeypatch.setattr(client.sched.requests, "post", lambda url, json=None, timeout=None: reply("{}") if json is None else None)
        client.sched._run_http_job("ohlcv_stocks", "http://x")
        jobs = {j["id"]: j for j in client.get("/jobs").json()["jobs"]}
        assert jobs["ohlcv_stocks"]["last_run"]["status"] == "ok" and jobs["enrich_sectors"]["last_run"] is None

    def test_runs_endpoint_filters_and_limits(self, client, monkeypatch):
        ok(client.sched, monkeypatch)
        for j in ("a", "b", "a"):
            client.sched._run_http_job(j, "http://x", "GET")
        assert [r["job_id"] for r in client.get("/runs").json()["runs"]] == ["a", "b", "a"]
        assert len(client.get("/runs", params={"job": "a"}).json()["runs"]) == 2
        assert len(client.get("/runs", params={"limit": 1}).json()["runs"]) == 1

    def test_a_manual_run_is_recorded_too(self, client, monkeypatch):
        monkeypatch.setattr(client.sched.requests, "post", lambda url, json=None, timeout=None: reply('{"done":1}') if json is None else None)
        assert client.post("/jobs/ohlcv_stocks/run").json()["status"] == "triggered"
        assert client.get("/runs", params={"job": "ohlcv_stocks"}).json()["runs"][0]["detail"] == '{"done":1}'

    def test_the_page_lists_jobs_and_runs_and_escapes_what_jobs_return(self, client, monkeypatch):
        monkeypatch.setattr(client.sched.requests, "post", lambda url, json=None, timeout=None: reply("<script>alert(1)</script>") if json is None else None)
        client.sched._run_http_job("ohlcv_stocks", "http://x")
        page = client.get("/ui")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        assert "OHLCV stocks refresh" in page.text and "never run" in page.text and "Recent runs" in page.text
        assert "<script>alert(1)</script>" not in page.text and "&lt;script&gt;" in page.text
