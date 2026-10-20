"""Flattrade adapter, tested against fakes of its wire protocol (no network, no broker).

REST goes through an injected `http_post`; the websocket feed talks to a tiny local
websocket server that behaves like Flattrade's (auth, subscribe, `tk` snapshot, `tf` deltas).
"""
import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from tests.terminal.helpers import ist
from trading_service.terminal.feed.base import BrokerError
from trading_service.terminal.feed.flattrade import (
    FlattradeApi,
    FlattradeFeed,
    FlattradeHistory,
    TokenResolver,
    config_from_env,
    parse_candle_time,
    parse_tpseries,
)

SBIN = "NSE:SBIN-EQ"


class FakeBroker:
    """Records REST calls and answers like PiConnect."""

    def __init__(self, scrips=None, series=None):
        self.calls = []
        self.scrips = scrips if scrips is not None else {"SBIN-EQ": "3045"}
        self.series = series or []

    def __call__(self, endpoint, jdata):
        self.calls.append((endpoint, jdata))
        if endpoint == "SearchScrip":
            values = [{"exch": jdata["exch"], "tsym": t, "token": tok} for t, tok in self.scrips.items() if jdata["stext"] in t]
            return {"stat": "Ok", "values": values} if values else {"stat": "Not_Ok", "emsg": "no data"}
        if endpoint == "TPSeries":
            return self.series
        raise AssertionError(endpoint)


def api(broker):
    return FlattradeApi("USER1", "tok", http_post=broker)


class TestRest:
    def test_every_call_carries_the_user_id(self):
        b = FakeBroker()
        api(b).search_scrip("SBIN-EQ", "NSE")
        assert b.calls[0] == ("SearchScrip", {"uid": "USER1", "stext": "SBIN-EQ", "exch": "NSE"})

    def test_not_ok_responses_become_errors(self):
        with pytest.raises(BrokerError, match="no data"):
            api(FakeBroker(scrips={})).search_scrip("ZZZ", "NSE")

    def test_candle_times_are_ist_wall_clock(self):
        assert parse_candle_time("19-10-2026 09:15:00") == ist(2026, 10, 19, 9, 15)
        assert parse_candle_time("19/10/2026 09:15:00") == ist(2026, 10, 19, 9, 15)

    def test_bad_candle_time_is_rejected(self):
        with pytest.raises(BrokerError):
            parse_candle_time("yesterday")

    def test_tpseries_rows_are_parsed_sorted_oldest_first_and_deduplicated(self):
        rows = [  # the broker returns newest first, as strings
            {"stat": "Ok", "time": "19-10-2026 09:16:00", "into": "101", "inth": "102", "intl": "100", "intc": "101.5", "intv": "50"},
            {"stat": "Ok", "time": "19-10-2026 09:15:00", "into": "100", "inth": "101", "intl": "99.5", "intc": "101", "intv": "75"},
            {"stat": "Ok", "time": "19-10-2026 09:15:00", "into": "100", "inth": "101", "intl": "99.5", "intc": "101", "intv": "75"},
            {"stat": "Not_Ok", "emsg": "no data"},
        ]
        bars = parse_tpseries(rows)
        assert [b.time for b in bars] == [ist(2026, 10, 19, 9, 15), ist(2026, 10, 19, 9, 16)]
        assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close, bars[0].volume) == (100, 101, 99.5, 101, 75)

    def test_config_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("FLATTRADE_USER_ID", "U")
        monkeypatch.setenv("FLATTRADE_TOKEN", "T")
        monkeypatch.delenv("FLATTRADE_ACCOUNT_ID", raising=False)
        assert config_from_env() == {"user_id": "U", "token": "T", "account_id": "U"}

    def test_missing_config_says_what_to_set(self, monkeypatch):
        monkeypatch.delenv("FLATTRADE_TOKEN", raising=False)
        with pytest.raises(BrokerError, match="FLATTRADE_TOKEN"):
            config_from_env()


class TestTokenResolver:
    def test_resolves_a_canonical_symbol_to_exchange_and_token(self):
        b = FakeBroker()
        assert TokenResolver(api(b)).resolve(SBIN) == ("NSE", "3045")

    def test_only_an_exact_trading_symbol_match_counts(self):
        b = FakeBroker(scrips={"SBIN-BE": "1", "SBIN-EQ": "3045"})
        assert TokenResolver(api(b)).resolve(SBIN) == ("NSE", "3045")

    def test_unknown_symbol_is_an_error(self):
        with pytest.raises(BrokerError):
            TokenResolver(api(FakeBroker(scrips={"OTHER": "9"}))).resolve(SBIN)

    def test_lookups_are_cached_in_memory(self):
        b = FakeBroker()
        r = TokenResolver(api(b))
        r.resolve(SBIN)
        r.resolve(SBIN)
        assert len(b.calls) == 1

    def test_lookups_survive_restarts_via_the_database(self, db):
        b = FakeBroker()
        TokenResolver(api(b), db).resolve(SBIN)

        b2 = FakeBroker()
        assert TokenResolver(api(b2), db).resolve(SBIN) == ("NSE", "3045")
        assert b2.calls == [], "second process read the token from terminal_broker_tokens"


class TestHistory:
    def row(self, hh, mm, price):
        return {"stat": "Ok", "time": f"19-10-2026 {hh:02d}:{mm:02d}:00", "into": str(price), "inth": str(price + 1),
                "intl": str(price - 1), "intc": str(price), "intv": "10"}

    def test_returns_only_bars_inside_the_requested_range(self):
        b = FakeBroker(series=[self.row(9, 17, 102), self.row(9, 16, 101), self.row(9, 15, 100)])
        h = FlattradeHistory(api(b), TokenResolver(api(b)))
        bars = h.fetch_1m(SBIN, ist(2026, 10, 19, 9, 16), ist(2026, 10, 19, 9, 17))
        assert [x.close for x in bars] == [101]

    def test_long_ranges_are_fetched_in_windows(self):
        b = FakeBroker(series=[])
        h = FlattradeHistory(api(b), TokenResolver(api(b)))
        h.fetch_1m(SBIN, ist(2026, 9, 1), ist(2026, 10, 19))
        windows = [c for c in b.calls if c[0] == "TPSeries"]
        assert len(windows) >= 3
        assert all(int(c[1]["et"]) - int(c[1]["st"]) <= 15 * 86400 for c in windows)
        assert windows[0][1]["intrv"] == "1" and windows[0][1]["token"] == "3045"


# ------------------------------------------------------------------- websocket
class FakeFlattradeServer:
    def __init__(self, auth_ok=True):
        self.auth_ok = auth_ok
        self.received = []
        self.connections = 0
        self.script = None   # async fn(ws) run after the subscribe message
        self._server = None

    async def handler(self, ws):
        self.connections += 1
        async for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg)
            if msg["t"] == "a":
                await ws.send(json.dumps({"t": "ck", "s": "OK"} if self.auth_ok else {"t": "ck", "s": "NOT_OK", "emsg": "bad token"}))
            elif msg["t"] == "t" and self.script:
                await self.script(ws)

    async def __aenter__(self):
        self._server = await serve(self.handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()


def make_feed(url, resolver=None, **kw):
    resolver = resolver or TokenResolver(api(FakeBroker()))
    statuses = []
    feed = FlattradeFeed("USER1", "ACC1", "tok", resolver, ws_url=url, on_status=statuses.append,
                         clock=lambda: ist(2026, 10, 19, 10, 0, 0), **kw)
    return feed, statuses


async def run_until(feed, ticks, n, timeout=3):
    task = asyncio.create_task(feed.run(ticks.append))
    try:
        for _ in range(int(timeout / 0.02)):
            if len(ticks) >= n:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"only {len(ticks)} ticks after {timeout}s")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_authenticates_subscribes_and_merges_snapshot_with_deltas():
    async with FakeFlattradeServer() as server:
        async def script(ws):
            await ws.send(json.dumps({"t": "tk", "e": "NSE", "tk": "3045", "ts": "SBIN-EQ", "lp": "800.50", "v": "1000", "c": "795.00", "o": "796", "h": "801", "l": "795"}))
            await ws.send(json.dumps({"t": "tf", "e": "NSE", "tk": "3045", "lp": "801.25", "v": "1200"}))   # only changes
            await ws.send(json.dumps({"t": "tf", "e": "NSE", "tk": "3045", "h": "802"}))                   # no price change

        server.script = script
        feed, statuses = make_feed(server.url)
        await feed.subscribe([SBIN])          # before connecting: remembered, sent on connect
        ticks = []
        await run_until(feed, ticks, 3)

    auth = server.received[0]
    assert auth == {"t": "a", "uid": "USER1", "actid": "ACC1", "accesstoken": "tok", "source": "API"}
    assert {"t": "t", "k": "NSE|3045"} in server.received

    first, second, third = ticks
    assert (first.symbol, first.price, first.volume, first.prev_close) == (SBIN, 800.5, 1000, 795.0)
    assert second.price == 801.25 and second.volume == 1200
    assert second.prev_close == 795.0, "fields missing from a delta are inherited from the snapshot"
    assert third.price == 801.25 and third.high == 802.0
    assert "connected" in statuses


async def test_ticks_for_unsubscribed_instruments_are_ignored():
    async with FakeFlattradeServer() as server:
        async def script(ws):
            await ws.send(json.dumps({"t": "tk", "e": "NSE", "tk": "9999", "lp": "5"}))
            await ws.send(json.dumps({"t": "tk", "e": "NSE", "tk": "3045", "lp": "800"}))

        server.script = script
        feed, _ = make_feed(server.url)
        await feed.subscribe([SBIN])
        ticks = []
        await run_until(feed, ticks, 1)
    assert [t.price for t in ticks] == [800.0]


async def test_subscribing_while_connected_sends_only_the_new_instrument():
    async with FakeFlattradeServer() as server:
        feed, statuses = make_feed(server.url, TokenResolver(api(FakeBroker(scrips={"SBIN-EQ": "3045", "TCS-EQ": "11536"}))))
        await feed.subscribe([SBIN])
        task = asyncio.create_task(feed.run(lambda t: None))
        for _ in range(100):
            if "connected" in statuses:
                break
            await asyncio.sleep(0.02)
        await feed.subscribe(["NSE:TCS-EQ"])
        await feed.unsubscribe([SBIN])
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert {"t": "t", "k": "NSE|3045"} in server.received
    assert {"t": "t", "k": "NSE|11536"} in server.received
    assert {"t": "u", "k": "NSE|3045"} in server.received


async def test_a_rejected_token_is_reported_and_retried_with_backoff():
    async with FakeFlattradeServer(auth_ok=False) as server:
        feed, statuses = make_feed(server.url, max_backoff=0.05)
        task = asyncio.create_task(feed.run(lambda t: None))
        for _ in range(150):
            if server.connections >= 2:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert server.connections >= 2
    assert "connected" not in statuses
    assert "reconnecting" in statuses


async def test_after_the_connection_drops_it_reconnects_and_resubscribes():
    async with FakeFlattradeServer() as server:
        async def script(ws):
            if server.connections == 1:
                await ws.close()           # first connection dies right after subscribing
            else:
                await ws.send(json.dumps({"t": "tk", "e": "NSE", "tk": "3045", "lp": "810"}))

        server.script = script
        feed, statuses = make_feed(server.url, max_backoff=0.05)
        await feed.subscribe([SBIN])
        ticks = []
        await run_until(feed, ticks, 1)

    assert server.connections >= 2
    subscribes = [m for m in server.received if m["t"] == "t"]
    assert len(subscribes) >= 2, "the watch list was re-sent on the new connection"
    assert ticks[0].price == 810.0


async def test_a_symbol_the_broker_does_not_know_is_skipped_not_fatal():
    async with FakeFlattradeServer() as server:
        feed, _ = make_feed(server.url, TokenResolver(api(FakeBroker(scrips={"SBIN-EQ": "3045"}))))
        await feed.subscribe(["NSE:NOPE-EQ", SBIN])
        assert feed._wanted == {"NSE:NOPE-EQ", SBIN}
        assert list(feed._by_key.values()) == [SBIN]


async def test_heartbeats_are_sent_to_keep_the_socket_alive():
    async with FakeFlattradeServer() as server:
        feed, statuses = make_feed(server.url, heartbeat=0.05)
        task = asyncio.create_task(feed.run(lambda t: None))
        await asyncio.sleep(0.4)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert sum(1 for m in server.received if m["t"] == "h") >= 2
