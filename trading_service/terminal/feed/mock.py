"""Demo-mode market: a deterministic synthetic market, so the whole terminal works
with no broker, no token and no market hours.

`MockMarket` is a pure function of (symbol, time): the same minute always has the
same OHLC, so the history the chart loads and the live ticks that extend it agree
with each other and tests are reproducible. `SimClock` lets demo mode run a
trading session faster than real time.
"""
from __future__ import annotations

import asyncio
import time as _time
import zlib
from datetime import date, datetime, timedelta
from typing import Callable, Optional, Sequence

import numpy as np

from ..candles import Candle
from ..ticks import Tick
from ..timeframes import IST, exchange_of, in_session, session_open, session_of
from .base import TickHandler

FIRST_DAY = date(2025, 6, 2)     # a Monday; the synthetic market starts here
DAYS = 700                        # weekdays of synthetic history available
TICK_SIZE = 0.05


class SimClock:
    """`now()` for demo mode. `speed=60` runs one simulated minute per real second;
    `speed=0` freezes time so tests drive it with `advance`."""

    def __init__(self, start: datetime, speed: float = 1.0, monotonic: Callable[[], float] = _time.monotonic):
        self._start = start
        self._speed = speed
        self._mono = monotonic
        self._t0 = monotonic()
        self._offset = 0.0

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._offset + (self._mono() - self._t0) * self._speed)

    def advance(self, seconds: float) -> None:
        self._offset += seconds


def _round_tick(price: float) -> float:
    return round(round(price / TICK_SIZE) * TICK_SIZE, 2)


class MockMarket:
    def __init__(self, seed: int = 7):
        self.seed = seed
        self._cache: dict[tuple[str, str], dict[str, np.ndarray]] = {}

    # ------------------------------------------------------------- series
    def _minutes_per_day(self, exchange: str) -> int:
        s = session_of(exchange)
        return (s.close.hour * 60 + s.close.minute) - (s.open.hour * 60 + s.open.minute)

    def base_price(self, symbol: str) -> float:
        return 80 + (zlib.crc32(symbol.encode()) % 4000)

    def _series(self, symbol: str) -> dict[str, np.ndarray]:
        exchange = exchange_of(symbol)
        key = (symbol, exchange)
        if key not in self._cache:
            n = self._minutes_per_day(exchange) * DAYS
            rng = np.random.default_rng(zlib.crc32(symbol.encode()) ^ self.seed)
            drift = rng.normal(0, 0.00012, DAYS).repeat(self._minutes_per_day(exchange))   # slow day-level trends
            ret = rng.normal(0, 0.00045, n) + drift
            close = self.base_price(symbol) * np.exp(np.cumsum(ret))
            open_ = np.concatenate(([close[0]], close[:-1]))
            wick_hi = np.abs(rng.normal(0, 0.00025, n)) * close
            wick_lo = np.abs(rng.normal(0, 0.00025, n)) * close
            volume = rng.integers(800, 6000, n)
            self._cache[key] = {
                "open": open_, "close": close,
                "high": np.maximum(open_, close) + wick_hi,
                "low": np.minimum(open_, close) - wick_lo,
                "volume": volume,
            }
        return self._cache[key]

    def _index(self, symbol: str, minute_start: datetime) -> Optional[int]:
        exchange = exchange_of(symbol)
        if not in_session(minute_start, exchange):
            return None
        local_day = minute_start.astimezone(IST).date()
        day_idx = int(np.busday_count(FIRST_DAY, local_day))
        if day_idx < 0 or day_idx >= DAYS:
            return None
        minute_of_day = int((minute_start - session_open(local_day, exchange)).total_seconds() // 60)
        return day_idx * self._minutes_per_day(exchange) + minute_of_day

    # --------------------------------------------------------------- bars
    def minute_bar(self, symbol: str, minute_start: datetime) -> Optional[Candle]:
        i = self._index(symbol, minute_start)
        if i is None:
            return None
        s = self._series(symbol)
        return Candle(
            minute_start, _round_tick(s["open"][i]), _round_tick(s["high"][i]),
            _round_tick(s["low"][i]), _round_tick(s["close"][i]), int(s["volume"][i]),
        )

    def bars(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        """Closed 1-minute bars whose start lies in [start, end)."""
        out: list[Candle] = []
        t = start.replace(second=0, microsecond=0)
        if t < start:
            t += timedelta(minutes=1)
        while t < end:
            bar = self.minute_bar(symbol, t)
            if bar:
                out.append(bar)
            t += timedelta(minutes=1)
        return out

    def tick_at(self, symbol: str, ts: datetime) -> Optional[Tick]:
        """The price at an instant: moves from this minute's open to its close, staying inside its range."""
        minute = ts.replace(second=0, microsecond=0)
        i = self._index(symbol, minute)
        if i is None:
            return None
        s = self._series(symbol)
        frac = (ts - minute).total_seconds() / 60.0
        wobble = np.sin(frac * 37 + i) * (s["high"][i] - s["low"][i]) * 0.15
        price = s["open"][i] + (s["close"][i] - s["open"][i]) * frac + wobble
        price = min(max(price, s["low"][i]), s["high"][i])
        day_start = i - int(i % self._minutes_per_day(exchange_of(symbol)))
        cum_before = float(s["volume"][day_start:i].sum())
        cum = int(cum_before + s["volume"][i] * frac)
        prev_close = float(s["close"][day_start - 1]) if day_start > 0 else float(s["open"][0])
        return Tick(symbol, _round_tick(price), ts, volume=cum, prev_close=_round_tick(prev_close))


class MockHistory:
    def __init__(self, market: Optional[MockMarket] = None):
        self.market = market or MockMarket()

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        return self.market.bars(symbol, start, end)


class MockFeed:
    """Emits a tick per subscribed symbol every `interval` seconds, priced from the
    synthetic market at the clock's current time. Silent while the market is closed."""

    def __init__(self, clock: SimClock, market: Optional[MockMarket] = None, interval: float = 0.25):
        self.clock = clock
        self.market = market or MockMarket()
        self.interval = interval
        self._symbols: set[str] = set()
        self.status = "disconnected"

    async def subscribe(self, symbols: Sequence[str]) -> None:
        self._symbols.update(symbols)

    async def unsubscribe(self, symbols: Sequence[str]) -> None:
        self._symbols.difference_update(symbols)

    def emit(self, on_tick: TickHandler) -> int:
        """One round of ticks (also called directly by tests). Returns how many were sent."""
        now = self.clock.now()
        sent = 0
        for symbol in sorted(self._symbols):
            tick = self.market.tick_at(symbol, now)
            if tick:
                on_tick(tick)
                sent += 1
        return sent

    async def run(self, on_tick: TickHandler) -> None:
        self.status = "connected"
        try:
            while True:
                self.emit(on_tick)
                await asyncio.sleep(self.interval)
        finally:
            self.status = "disconnected"
