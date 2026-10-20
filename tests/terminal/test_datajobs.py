"""The Data screen's backend: how complete the data is, and starting the scrape jobs."""
import pytest
from tests.terminal.test_api import client, terminal  # noqa: F401  (fixtures)
from trading_service.terminal import datajobs
from trading_service.terminal.screener_sync import ScreenerClient, ScreenerUnavailable, SyncError


class FakeScreener:
    def __init__(self, jobs=None, post_result=None, down=False):
        self.jobs, self.post_result, self.down, self.posted = jobs or {}, post_result or {"job_id": "x", "status": "started"}, down, []

    def get(self, path, **params):
        if self.down:
            raise ScreenerUnavailable("strategy-screener is not reachable")
        return self.jobs

    def post(self, path, **params):
        if self.down:
            raise ScreenerUnavailable("strategy-screener is not reachable")
        self.posted.append((path, params))
        return self.post_result


def fill(db):
    for s, sector, cap in [("NSE:A-EQ", "Banks", 100.0), ("NSE:B-EQ", "", None), ("NSE:C-EQ", "IT", 0), ("NSE:NIFTY-INDEX", "", None)]:
        db.execute("INSERT INTO symbols (tradingsymbol, name, sector, market_cap) VALUES (:s, :s, :c, :m)", s=s, c=sector, m=cap)


class TestStatus:
    def test_counts_only_equities_and_how_many_have_a_sector_and_market_cap(self, db):
        fill(db)
        assert datajobs.data_status(db)["symbols"] == {"total": 3, "with_sector": 2, "with_market_cap": 1}

    def test_says_how_fresh_the_daily_candles_are(self, db):
        for s, d in [("NSE:A-EQ", "2026-10-15"), ("NSE:A-EQ", "2026-10-16"), ("NSE:B-EQ", "2026-10-16")]:
            db.execute("INSERT INTO ohlcv_1d (tradingsymbol, date, open, high, low, close, volume) VALUES (:s, :d, 1, 1, 1, 1, 1)", s=s, d=d)
        assert datajobs.data_status(db)["ohlcv_1d"] == {"symbols": 2, "latest": "2026-10-16", "oldest": "2026-10-15", "symbols_on_latest": 2}

    def test_reports_symbol_changes_without_touching_the_snapshot(self, db):
        fill(db)
        assert datajobs.data_status(db)["changes"] is None        # nothing to compare with yet
        db.execute("INSERT INTO terminal_symbol_snapshot (symbol) VALUES ('NSE:A-EQ'), ('NSE:OLD-EQ')")
        before = datajobs.data_status(db)["changes"]
        assert before["added"] == ["NSE:B-EQ", "NSE:C-EQ", "NSE:NIFTY-INDEX"] and before["removed"] == ["NSE:OLD-EQ"]
        assert datajobs.data_status(db)["changes"] == before      # asking twice changes nothing

    def test_empty_tables_are_fine(self, db):
        s = datajobs.data_status(db)
        assert s["symbols"]["total"] == 0 and s["ohlcv_1d"]["latest"] is None and s["financials"]["companies"] == 0


class TestRunning:
    def test_passes_only_allowed_parameters_to_the_job(self):
        fake = FakeScreener()
        datajobs.run_job(fake, "financials", {"job": "financials", "mode": "announcements_only", "limit": "50"})
        assert fake.posted == [("/admin/update-financials", {"mode": "announcements_only", "limit": 50})]

    @pytest.mark.parametrize("job,raw,fragment", [
        ("nope", {}, "unknown job"),
        ("financials", {"mode": "everything"}, "must be one of"),
        ("latest_results", {"days": 0}, "between 1 and 90"),
        ("latest_results", {"days": "many"}, "whole number"),
        ("sectors", {"mode": "full"}, "does not take mode"),
    ])
    def test_bad_requests_are_refused_before_anything_is_called(self, job, raw, fragment):
        fake = FakeScreener()
        with pytest.raises(SyncError, match=fragment):
            datajobs.run_job(fake, job, raw)
        assert fake.posted == []

    def test_a_job_that_fails_to_start_says_why(self):
        with pytest.raises(ScreenerUnavailable, match="Fyers token"):
            datajobs.run_job(FakeScreener(post_result={"error": "Fyers token expired"}), "ohlcv", {})

    def test_every_job_in_the_catalogue_can_be_started(self):
        fake = FakeScreener()
        for j in datajobs.catalog():
            datajobs.run_job(fake, j["id"], {})
        assert len(fake.posted) == len(datajobs.JOBS)


class TestClient:
    def test_post_goes_out_as_a_post(self):
        class Resp:
            status_code = 200
            def json(self): return {"ok": 1}
        class Session:
            calls = []
            def post(self, url, params, timeout): self.calls.append(("post", url, params)); return Resp()
            def get(self, url, params, timeout): self.calls.append(("get", url, params)); return Resp()
        s = Session()
        c = ScreenerClient("http://x", session=s)
        c.post("/admin/update-ohlcv", mode="full")
        c.get("/admin/jobs")
        assert s.calls == [("post", "http://x/admin/update-ohlcv", {"mode": "full"}), ("get", "http://x/admin/jobs", {})]


class TestApi:
    def test_status_includes_the_catalogue_and_running_jobs(self, client):
        client.app.state.screener_client = FakeScreener(jobs={"financials": {"status": "running", "progress": "[3/200] SBIN"}})
        body = client.get("/terminal/data/status").json()
        assert body["jobs"]["financials"]["progress"] == "[3/200] SBIN"
        assert {j["id"] for j in body["catalog"]} == set(datajobs.JOBS) and "symbols" in body["status"]

    def test_status_still_answers_when_the_screener_is_down(self, client):
        client.app.state.screener_client = FakeScreener(down=True)
        body = client.get("/terminal/data/status").json()
        assert body["jobs"] is None and "not reachable" in body["jobs_error"] and "symbols" in body["status"]

    def test_run_starts_a_job_and_reports_errors_readably(self, client):
        fake = client.app.state.screener_client = FakeScreener()
        r = client.post("/terminal/data/run", json={"job": "latest_results", "days": 7})
        assert r.status_code == 202 and r.json()["status"] == "started" and fake.posted == [("/admin/scrape-latest-results", {"days": 7})]
        assert client.post("/terminal/data/run", json={"job": "latest_results", "days": 500}).status_code == 400
        client.app.state.screener_client = FakeScreener(down=True)
        assert client.post("/terminal/data/run", json={"job": "sectors"}).status_code == 502
