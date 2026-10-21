"""Broker sessions: stored logins beat the environment, a login is applied to the running terminal, the morning job reports."""
import pytest

from trading_service.terminal.sessions import BrokerLogins, SessionStore


def test_a_stored_login_is_laid_over_the_environment(db):
    store = SessionStore(db)
    assert store.env({"FLATTRADE_TOKEN": "old", "OTHER": "x"})["FLATTRADE_TOKEN"] == "old"
    store.save("flattrade", {"FLATTRADE_TOKEN": "new"}, "auto")
    env = store.env({"FLATTRADE_TOKEN": "old", "OTHER": "x"})
    assert env["FLATTRADE_TOKEN"] == "new" and env["OTHER"] == "x"
    assert store.get("flattrade")["source"] == "auto"


async def test_a_login_is_saved_and_handed_to_the_running_terminal(db):
    applied = []
    logins = BrokerLogins(db, apply=lambda b, t: applied.append((b, t)), login=lambda b, env, code: {"FLATTRADE_TOKEN": f"T-{code or 'auto'}"})
    res = await logins.login("flattrade")
    assert res["source"] == "auto" and applied == [("flattrade", {"FLATTRADE_TOKEN": "T-auto"})]
    await logins.login("flattrade", code="http://127.0.0.1:5000/?code=c1")
    assert SessionStore(db).get("flattrade")["source"] == "manual"


async def test_the_morning_job_logs_in_where_it_can_and_reports_failures(db, monkeypatch):
    for k in ("FLATTRADE_USER_ID", "FLATTRADE_PASSWORD", "FLATTRADE_TOTP_KEY", "FLATTRADE_API_KEY", "FLATTRADE_API_SECRET"):
        monkeypatch.setenv(k, "x")

    def boom(broker, env, code):
        raise RuntimeError("captcha")

    results = await BrokerLogins(db, login=boom).login_all()
    assert results["flattrade"] == {"ok": False, "detail": "captcha"}
    assert results["paytm"]["ok"] is None


async def test_an_unknown_broker_is_refused(db):
    with pytest.raises(ValueError):
        await BrokerLogins(db).login("zerodha")


def test_status_says_where_each_token_came_from(db, monkeypatch):
    monkeypatch.setenv("FLATTRADE_API_KEY", "key")
    SessionStore(db).save("flattrade", {"FLATTRADE_TOKEN": "T"}, "manual")
    rows = {r["broker"]: r for r in BrokerLogins(db, check=lambda env: {"flattrade": {"status": "valid", "detail": ""}}).status()}
    assert rows["flattrade"]["status"] == "valid" and rows["flattrade"]["source"] == "manual"
    assert rows["flattrade"]["login_url"].startswith("https://auth.flattrade.in/?app_key=key")
    assert rows["paytm"]["status"] == "unknown"


async def test_the_live_feed_takes_a_new_token_and_reconnects_at_once():
    from trading_service.terminal.feed.flattrade import FlattradeFeed

    class Ws:
        closed = False

        async def close(self):
            self.closed = True

    feed = FlattradeFeed("FT1", "FT1", "old", resolver=None)
    feed._ws = Ws()
    ws = feed._ws
    await feed.use_token("new")
    assert feed.token == "new" and ws.closed
