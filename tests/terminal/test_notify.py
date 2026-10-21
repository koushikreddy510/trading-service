"""Slack messages: what is said, when, and that Slack being unreachable never matters."""
import pytest

from tests.terminal.test_engine import SYMBOL, Harness
from trading_service.terminal import notify
from trading_service.terminal.hub import Hub
from trading_service.terminal.notify import Notifier, data_digest, digest_text
from trading_service.terminal.ticks import QuoteBoard
from trading_service.terminal.hub import Subscriptions


class Slack:
    def __init__(self):
        self.sent = []
        self.channels = []

    def __call__(self, text, account="default", channel="orders"):
        self.sent.append((account, text))
        self.channels.append(channel)

    @property
    def texts(self):
        return [t for _, t in self.sent]


@pytest.fixture
def slack():
    return Slack()


@pytest.fixture
def notifier(slack):
    return Notifier(sender=slack, background=False)


class TestTradeMessages:
    def test_an_entry_is_announced_once_with_its_stop_target_and_reason(self, db, notifier, slack):
        for e in Harness(db).close_bar():
            notifier.on_event(e)
        (text,) = slack.texts
        assert text.startswith("🟢 *Paper ENTRY* BUY 10 SBIN @ 101.00")
        assert "stop 99.50" in text and "target 104.00" in text and "close crossed above line" in text

    def test_a_trail_move_is_not_an_entry(self, db, notifier, slack):
        h = Harness(db)
        (_, pos_event) = h.close_bar()
        slack.sent.clear()
        notifier.on_event(pos_event)              # the same position again, now with no signal in front of it
        assert slack.sent == []

    def test_an_exit_shows_the_reason_and_the_profit(self, db, notifier, slack):
        h = Harness(db)
        h.close_bar()
        (p,) = h.book.open_positions()
        notifier.on_event({"type": "position", "position": h.book.close(p, 104.0, "target").to_json()})
        assert slack.texts == ["✅ *Paper EXIT* (target) BUY 10 SBIN 101.00 → 104.00 · P&L +30.00 (+2.97%)"]

    def test_a_loss_is_marked_and_an_exit_rule_reads_naturally(self, db, notifier, slack):
        h = Harness(db)
        h.close_bar()
        (p,) = h.book.open_positions()
        notifier.on_event({"type": "position", "position": h.book.close(p, 99.0, "exit_rule").to_json()})
        assert slack.texts[0].startswith("🔻 *Paper EXIT* (exit rule)") and "-20.00" in slack.texts[0]

    def test_a_skipped_signal_stays_out_of_slack(self, notifier, slack):
        notifier.on_event({"type": "signal", "signal": {"id": "s", "status": "rejected", "side": "BUY", "symbol": SYMBOL, "timeframe": "1H", "reason": "stop is above entry"}})
        assert slack.sent == []

    def test_trades_go_to_the_orders_channel(self, db, notifier, slack):
        for e in Harness(db).close_bar():
            notifier.on_event(e)
        assert slack.channels == ["orders"]

    def test_a_partial_exit_is_announced_once_and_the_final_exit_after_it(self, db, notifier, slack):
        h = Harness(db)
        h.close_bar()
        (p,) = h.book.open_positions()
        slack.sent.clear()
        half = h.book.partial_close(p, 104.0, "exit_rule_1").to_json()
        notifier.on_event({"type": "position", "position": half})
        notifier.on_event({"type": "position", "position": half})          # e.g. a trail move afterwards: nothing new
        assert slack.texts == ["✅ *Paper PARTIAL EXIT* (exit rule 1) BUY 5 SBIN 101.00 → 104.00 · P&L +15.00 · 5 left for exit rule 2"]
        notifier.on_event({"type": "position", "position": h.book.close(p, 105.0, "exit_rule_2").to_json()})
        assert len(slack.texts) == 2 and slack.texts[1].startswith("✅ *Paper EXIT* (exit rule 2) BUY 10 SBIN")

    def test_bars_and_quotes_say_nothing(self, notifier, slack):
        notifier.on_event({"type": "bar_closed"})
        assert slack.sent == []

    def test_demo_runs_are_tagged_and_live_feed_runs_are_not(self, slack):
        assert Notifier(tag="demo", sender=slack, background=False).tag == "[demo] "
        assert Notifier(tag="flattrade", sender=slack, background=False).tag == ""

    def test_an_account_picks_its_own_webhook(self, monkeypatch):
        from trading_service.slack_notify import webhook_for
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks/shared")
        monkeypatch.setenv("SLACK_WEBHOOK_URL_ZERODHA", "https://hooks/zerodha")
        assert webhook_for() == "https://hooks/shared" and webhook_for("zerodha") == "https://hooks/zerodha" and webhook_for("other") == "https://hooks/shared"

    def test_jobs_and_data_have_their_own_webhooks_and_fall_back_to_the_shared_one(self, monkeypatch):
        from trading_service.slack_notify import webhook_for
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks/orders")
        monkeypatch.delenv("SLACK_DATA_WEBHOOK_URL", raising=False)
        monkeypatch.setenv("SLACK_JOBS_WEBHOOK_URL", "https://hooks/jobs")
        assert webhook_for(channel="jobs") == "https://hooks/jobs"
        assert webhook_for(channel="data") == "https://hooks/orders"
        assert webhook_for(channel="orders") == "https://hooks/orders"


class TestHubIntegration:
    def test_every_broadcast_reaches_the_listener_and_a_broken_listener_breaks_nothing(self, db, notifier, slack):
        h = Harness(db)
        h.close_bar()
        (p,) = h.book.open_positions()
        hub = Hub(Subscriptions(), QuoteBoard())
        hub.listeners.append(lambda e: 1 / 0)
        hub.listeners.append(notifier.on_event)
        hub.broadcast({"type": "position", "position": h.book.close(p, 104.0, "target").to_json()})
        assert len(slack.sent) == 1

    def test_a_terminal_wires_its_notifier_to_its_hub(self, db):
        from trading_service.terminal.feed.mock import MockFeed, MockHistory, SimClock
        from trading_service.terminal.runtime import Terminal
        from tests.terminal.helpers import ist
        clock = SimClock(ist(2026, 10, 19, 10, 0, 30), speed=0)
        n = Notifier(sender=Slack(), background=False)
        t = Terminal(db, MockFeed(clock), MockHistory(), clock.now, mode="test", hub_interval=0, notifier=n)
        assert n.on_event in t.hub.listeners and t.notifier is n


class TestOtherMessages:
    def test_a_finished_backtest(self, notifier, slack):
        notifier.backtest_done({"status": "done", "account": "default", "params": {"start": "2025-10-01", "end": "2026-10-19"},
                                "result": {"summary": {"return_pct": 12.5, "final_equity": 1125000, "initial_capital": 1000000, "trades": 40,
                                                       "win_rate_pct": 45.0, "open_positions": 3, "max_drawdown_pct": 6.2}}})
        assert slack.texts == ["📊 *Backtest done* 2025-10-01 → 2026-10-19: +12.50% (equity 1,125,000 from 1,000,000) · 40 closed trades, win rate 45.0% · 3 still open · max drawdown 6.2%"]

    def test_a_failed_backtest(self, notifier, slack):
        notifier.backtest_done({"status": "failed", "error": "boom"})
        assert slack.texts == ["⚠️ Backtest failed: boom"] and slack.channels == ["jobs"]

    def test_a_sync_lists_what_changed(self, notifier, slack):
        notifier.sync_result({"watchlist": {"name": "Screen: Momentum"}, "created": False, "matched": 12, "near": 3,
                              "added": ["NSE:TCS-EQ", "NSE:INFY-EQ"], "removed": ["NSE:SBIN-EQ"], "kept_for_trading": ["NSE:ITC-EQ"]})
        assert slack.texts == ["🔄 *Screener sync* “Screen: Momentum”: 12 match, 3 near\n  added: TCS, INFY\n  removed: SBIN\n  kept (open position or rule): ITC"]
        assert slack.channels == ["data"]

    def test_a_sync_without_changes_says_so(self, notifier, slack):
        notifier.sync_result({"watchlist": {"name": "W"}, "created": True, "matched": 0, "near": 0, "added": [], "removed": []})
        assert slack.texts == ["🔄 *Screener sync* “W” (created): 0 match, 0 near - no change"]

    def test_the_daily_refresh_reports_each_strategy_and_its_failures(self, notifier, slack):
        notifier.sync_refresh([{"strategy_id": 4, "ok": True, "added": 2, "removed": 1, "matched": 9, "near": 2}, {"strategy_id": 7, "ok": False, "error": "screener down"}])
        assert slack.texts == ["🔄 *Screener sync (daily)*\n  strategy 4: +2 −1 · 9 match, 2 near\n  strategy 7: ⚠️ screener down"]
        assert slack.channels == ["data"]

    def test_a_long_list_is_cut_short(self):
        assert notify.names([f"NSE:S{i}-EQ" for i in range(20)]).endswith("and 5 more")

    def test_a_slack_outage_is_swallowed(self):
        def down(text, account="default", channel="orders"):
            raise ConnectionError("no route")
        Notifier(sender=down, background=False).send("hello")


class TestDataDigest:
    def _symbol(self, db, s):
        db.execute("INSERT INTO symbols (tradingsymbol, name) VALUES (:s, :s)", s=s)

    def test_the_first_digest_only_records_the_list_then_changes_are_reported(self, db):
        for s in ("NSE:A-EQ", "NSE:B-EQ"):
            self._symbol(db, s)
        first = data_digest(db)
        assert first["symbols"] == {"count": 2, "added": None, "removed": None}
        db.execute("DELETE FROM symbols WHERE tradingsymbol = 'NSE:A-EQ'")
        self._symbol(db, "NSE:C-EQ")
        second = data_digest(db)
        assert second["symbols"] == {"count": 2, "added": ["NSE:C-EQ"], "removed": ["NSE:A-EQ"]}
        assert data_digest(db)["symbols"]["added"] == []

    def test_freshness_of_each_feed(self, db):
        db.execute("INSERT INTO ohlcv_1d (tradingsymbol, date, open, high, low, close, volume) VALUES ('NSE:A-EQ', '2026-10-16', 1, 1, 1, 1, 1), ('NSE:B-EQ', '2026-10-16', 1, 1, 1, 1, 1)")
        d = data_digest(db)
        assert d["ohlcv_1d"] == {"latest": "2026-10-16", "symbols_on_latest": 2}
        assert d["screener"] == {"run_date": None, "stocks": 0}
        text = digest_text(d)
        assert "latest 2026-10-16 · 2 symbols on that day" in text and "screener scan: never run" in text

    def test_the_text_lists_added_and_removed_symbols(self):
        text = digest_text({"symbols": {"count": 5, "added": ["NSE:C-EQ"], "removed": ["NSE:A-EQ"]}, "screener": {"run_date": "2026-10-19", "stocks": 1800}})
        assert "(+1 added, −1 removed)" in text and "added: C" in text and "removed: A" in text and "2026-10-19 · 1,800 stocks" in text

    def test_the_endpoint_posts_to_slack_and_returns_the_digest(self, db):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from tests.terminal.helpers import ist
        from trading_service.terminal import api
        from trading_service.terminal.feed.mock import MockFeed, MockHistory, SimClock
        from trading_service.terminal.runtime import Terminal
        slack = Slack()
        clock = SimClock(ist(2026, 10, 19, 10, 0, 30), speed=0)
        app = FastAPI()
        api.mount(app)
        app.state.terminal = Terminal(db, MockFeed(clock), MockHistory(), clock.now, mode="flattrade", hub_interval=0, notifier=Notifier(sender=slack, background=False))
        with TestClient(app) as c:
            body = c.post("/terminal/notify/data-digest").json()
        assert "screener" in body and slack.texts[0].startswith("🗂 *Daily data digest*") and slack.channels == ["data"]


class TestBrokerTokenCheck:
    def make_client(self, db, slack):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from tests.terminal.helpers import ist
        from trading_service.terminal import api
        from trading_service.terminal.feed.mock import MockFeed, MockHistory, SimClock
        from trading_service.terminal.runtime import Terminal
        clock = SimClock(ist(2026, 10, 19, 10, 0, 30), speed=0)
        app = FastAPI()
        api.mount(app)
        app.state.terminal = Terminal(db, MockFeed(clock), MockHistory(), clock.now, mode="flattrade", hub_interval=0, notifier=Notifier(sender=slack, background=False))
        return TestClient(app)

    def test_a_rejected_login_goes_to_slack_with_the_command_and_a_good_one_stays_quiet(self, db, monkeypatch):
        from broker_auth import check
        monkeypatch.setenv("FLATTRADE_TOKEN", "T")
        monkeypatch.delenv("PAYTM_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("PAYTM_READ_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("STRATEGY_SCREENER_URL", raising=False)
        slack = Slack()
        with self.make_client(db, slack) as c:
            monkeypatch.setattr(check, "_http", lambda m, u, **kw: (200, {"stat": "Not_Ok", "emsg": "Session Expired"}))
            assert c.post("/terminal/notify/broker-tokens").json()["flattrade"]["status"] == "rejected"
            monkeypatch.setattr(check, "_http", lambda m, u, **kw: (200, {"stat": "Ok"}))
            assert c.post("/terminal/notify/broker-tokens").json()["flattrade"]["status"] == "valid"
        assert len(slack.texts) == 1 and "Broker logins" in slack.texts[0] and "Session Expired" in slack.texts[0]
