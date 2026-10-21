"""Flattrade (Noren PiConnect) market data: REST history/search and the live websocket.

Mirrors the Go client in trade/plugins/broker/flattrade so both talk to the broker
the same way:

* REST: POST  <base>/<Endpoint>  with form body  jData=<json>&jKey=<session token>
* WS:   send {"t":"a","uid","actid","accesstoken","source":"API"}; wait for
        {"t":"ck"|"ak","s":"OK"}; subscribe with {"t":"t","k":"NSE|11536#NSE|2885"};
        heartbeat {"t":"h"} every 5s. `tk` messages are full touchline snapshots and
        `tf` messages carry only changed fields, so they are merged per instrument.

This module only reads market data; it never places orders.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Sequence

from ..candles import Candle
from ..db import Database
from ..ticks import Tick
from ..timeframes import IST, UTC, exchange_of
from .base import BrokerError, StatusHandler, TickHandler

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://piconnect.flattrade.in/PiConnectAPI"
DEFAULT_WS_URL = "wss://piconnect.flattrade.in/PiConnectWSAPI/"
HEARTBEAT_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 30.0
TPSERIES_WINDOW = timedelta(days=15)   # keep each history request small


class NotConfigured(BrokerError):
    pass


def config_from_env() -> dict[str, str]:
    """FLATTRADE_USER_ID / FLATTRADE_TOKEN (the daily session token, 'jKey') / FLATTRADE_ACCOUNT_ID."""
    user = os.getenv("FLATTRADE_USER_ID", "")
    token = os.getenv("FLATTRADE_TOKEN", "")
    if not user or not token:
        raise NotConfigured("set FLATTRADE_USER_ID and FLATTRADE_TOKEN (today's session token)")
    return {"user_id": user, "token": token, "account_id": os.getenv("FLATTRADE_ACCOUNT_ID", user)}


# ------------------------------------------------------------------------ REST
HttpPost = Callable[[str, dict], Any]   # (endpoint, jData) -> parsed JSON


class FlattradeApi:
    def __init__(self, user_id: str, token: str, base_url: str = DEFAULT_BASE_URL, http_post: Optional[HttpPost] = None):
        self.user_id = user_id
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._post = http_post or self._requests_post

    def _requests_post(self, endpoint: str, jdata: dict) -> Any:
        import requests

        # Send as raw string — Flattrade rejects URL-encoded JSON ({ becomes %7B etc.)
        raw = f"jData={json.dumps(jdata, separators=(',', ':'))}&jKey={self.token}"
        resp = requests.post(
            f"{self.base_url}/{endpoint}",
            data=raw.encode(),
            timeout=30,
        )
        try:
            return resp.json()
        except ValueError:
            raise BrokerError(f"{endpoint}: HTTP {resp.status_code}, not JSON") from None

    def call(self, endpoint: str, payload: dict) -> Any:
        body = self._post(endpoint, {"uid": self.user_id, **payload})
        if isinstance(body, dict) and str(body.get("stat", "")).lower() == "not_ok":
            raise BrokerError(f"{endpoint}: {body.get('emsg') or 'Not_Ok'}")
        return body

    def search_scrip(self, text: str, exchange: str) -> list[dict]:
        body = self.call("SearchScrip", {"stext": text, "exch": exchange})
        return list(body.get("values") or []) if isinstance(body, dict) else []

    def tpseries(self, exchange: str, token: str, start: datetime, end: datetime, interval: str = "1") -> list[dict]:
        body = self.call(
            "TPSeries",
            {"exch": exchange, "token": token, "st": str(int(start.timestamp())), "et": str(int(end.timestamp())), "intrv": interval},
        )
        if isinstance(body, dict):   # an error object without stat=Not_Ok
            raise BrokerError(f"TPSeries: {body.get('emsg') or body}")
        return list(body or [])


def parse_candle_time(text: str) -> datetime:
    """Flattrade bar timestamps are IST wall-clock: 'DD-MM-YYYY HH:MM:SS' (or with '/')."""
    text = text.strip()
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST).astimezone(UTC)
        except ValueError:
            continue
    raise BrokerError(f"unparseable candle time {text!r}")


def parse_tpseries(rows: Sequence[dict]) -> list[Candle]:
    """TPSeries returns newest-first rows of strings; entries with stat != Ok are skipped."""
    out: dict[datetime, Candle] = {}
    for r in rows:
        if str(r.get("stat", "Ok")).lower() != "ok":
            continue
        t = parse_candle_time(r["time"])
        out[t] = Candle(t, float(r["into"]), float(r["inth"]), float(r["intl"]), float(r["intc"]), int(float(r.get("intv") or 0)))
    return [out[t] for t in sorted(out)]


# ------------------------------------------------------------- symbol -> token
class TokenResolver:
    """Maps our `NSE:SBIN-EQ` ids to Flattrade tokens via SearchScrip, caching in
    the database so each symbol is looked up once."""

    def __init__(self, api: FlattradeApi, db: Optional[Database] = None):
        self.api = api
        self.db = db
        self._memory: dict[str, tuple[str, str]] = {}   # symbol -> (exchange, token)

    def resolve(self, symbol: str) -> tuple[str, str]:
        if symbol in self._memory:
            return self._memory[symbol]
        exchange = exchange_of(symbol)
        tsym = symbol.split(":", 1)[1] if ":" in symbol else symbol
        if self.db is not None:
            row = self.db.row("SELECT exchange, token FROM terminal_broker_tokens WHERE symbol = :s", s=symbol)
            if row:
                self._memory[symbol] = (row["exchange"], row["token"])
                return self._memory[symbol]
        matches = [m for m in self.api.search_scrip(tsym, exchange) if m.get("tsym") == tsym and m.get("exch", exchange) == exchange]
        if not matches:
            raise BrokerError(f"{symbol}: no instrument named {tsym} on {exchange}")
        token = str(matches[0]["token"])
        self._memory[symbol] = (exchange, token)
        if self.db is not None:
            self.db.execute(
                """
                INSERT INTO terminal_broker_tokens (symbol, exchange, token, tsym) VALUES (:s, :e, :t, :ts)
                ON CONFLICT (symbol) DO UPDATE SET token = EXCLUDED.token, tsym = EXCLUDED.tsym, resolved_at = now()
                """,
                s=symbol, e=exchange, t=token, ts=tsym,
            )
        return self._memory[symbol]


class FlattradeHistory:
    def __init__(self, api: FlattradeApi, resolver: TokenResolver):
        self.api = api
        self.resolver = resolver

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        exchange, token = self.resolver.resolve(symbol)
        bars: dict[datetime, Candle] = {}
        cursor = start
        while cursor < end:
            stop = min(cursor + TPSERIES_WINDOW, end)
            for bar in parse_tpseries(self.api.tpseries(exchange, token, cursor, stop, "1")):
                if start <= bar.time < end:
                    bars[bar.time] = bar
            cursor = stop
        return [bars[t] for t in sorted(bars)]


# ----------------------------------------------------------------- websocket
class FlattradeFeed:
    """Live ticks over the Flattrade websocket, with reconnect + resubscribe."""

    def __init__(
        self,
        user_id: str,
        account_id: str,
        token: str,
        resolver: TokenResolver,
        ws_url: str = DEFAULT_WS_URL,
        on_status: Optional[StatusHandler] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        heartbeat: float = HEARTBEAT_SECONDS,
        max_backoff: float = MAX_BACKOFF_SECONDS,
    ):
        self.user_id, self.account_id, self.token = user_id, account_id, token
        self.resolver = resolver
        self.ws_url = ws_url
        self.on_status = on_status
        self.clock = clock
        self.heartbeat = heartbeat
        self.max_backoff = max_backoff
        self.status = "disconnected"
        self._wanted: set[str] = set()
        self._by_key: dict[str, str] = {}          # "NSE|2885" -> "NSE:RELIANCE-EQ"
        self._merged: dict[str, dict[str, str]] = {}
        self._ws = None
        self._authenticated = False

    # ---------------------------------------------------------- subscriptions
    async def subscribe(self, symbols: Sequence[str]) -> None:
        keys = []
        for s in symbols:
            self._wanted.add(s)
            try:
                exchange, token = await asyncio.to_thread(self.resolver.resolve, s)
            except BrokerError as e:
                log.warning("cannot subscribe %s: %s", s, e)
                continue
            self._by_key[f"{exchange}|{token}"] = s
            keys.append(f"{exchange}|{token}")
        await self._send_keys("t", keys)

    async def unsubscribe(self, symbols: Sequence[str]) -> None:
        keys = []
        for s in symbols:
            self._wanted.discard(s)
            for key, sym in list(self._by_key.items()):
                if sym == s:
                    del self._by_key[key]
                    self._merged.pop(key, None)
                    keys.append(key)
        await self._send_keys("u", keys)

    async def _send_keys(self, kind: str, keys: list[str]) -> None:
        if keys and self._ws is not None and self._authenticated:
            await self._send({"t": kind, "k": "#".join(keys)})

    async def _send(self, payload: dict) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))

    def _set_status(self, status: str) -> None:
        self.status = status
        if self.on_status:
            self.on_status(status)

    # -------------------------------------------------------------------- run
    async def run(self, on_tick: TickHandler) -> None:
        backoff = 1.0
        first = True
        while True:
            self._set_status("connecting" if first else "reconnecting")
            first = False
            try:
                await self._run_once(on_tick)
                backoff = 1.0
            except asyncio.CancelledError:
                self._set_status("disconnected")
                raise
            except Exception as e:   # network errors, auth rejection, bad frames
                log.warning("flattrade feed: %s", e)
            self._ws, self._authenticated = None, False
            self._set_status("disconnected")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    async def _run_once(self, on_tick: TickHandler) -> None:
        from websockets.asyncio.client import connect

        async with connect(self.ws_url) as ws:
            self._ws, self._authenticated = ws, False
            await self._send({"t": "a", "uid": self.user_id, "actid": self.account_id, "accesstoken": self.token, "source": "API"})
            beat = asyncio.create_task(self._heartbeat())
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    kind = msg.get("t")
                    if not self._authenticated and kind in ("ck", "ak"):
                        if str(msg.get("s", "")).upper() != "OK":
                            raise BrokerError(f"websocket authentication rejected: {msg.get('emsg')}")
                        self._authenticated = True
                        self._set_status("connected")
                        await self._resubscribe()
                    elif kind in ("tk", "tf"):
                        tick = self._handle(msg)
                        if tick:
                            on_tick(tick)
            finally:
                beat.cancel()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat)
            try:
                await self._send({"t": "h"})
            except Exception:
                return

    async def _resubscribe(self) -> None:
        keys = [k for k, s in self._by_key.items() if s in self._wanted]
        if keys:
            await self._send({"t": "t", "k": "#".join(keys)})

    # ---------------------------------------------------------------- decoding
    def _handle(self, msg: dict) -> Optional[Tick]:
        key = f"{msg.get('e', '')}|{msg.get('tk', '')}"
        symbol = self._by_key.get(key)
        if symbol is None:
            return None
        merged = self._merged.setdefault(key, {})
        merged.update({k: str(v) for k, v in msg.items()})
        try:
            price = float(merged["lp"])
        except (KeyError, ValueError):
            return None           # a delta that arrives before any price is known
        if price <= 0:
            return None

        def num(field: str) -> Optional[float]:
            try:
                return float(merged[field])
            except (KeyError, ValueError):
                return None

        volume = num("v")
        return Tick(
            symbol=symbol, price=price, ts=self.clock(),
            volume=int(volume) if volume is not None else None,
            open=num("o"), high=num("h"), low=num("l"), prev_close=num("c"),
        )
