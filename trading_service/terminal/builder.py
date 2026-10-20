"""Turns ticks into 1-minute candles, and tells the rule engine when bars close.

Two deliberately separate jobs:

* `on_tick` is the hot path and does NO I/O: it only folds the tick into the
  forming 1-minute bar held in memory.
* `flush(now)` runs on a timer (about once a second). It (1) closes and persists
  every 1-minute bar whose minute has passed, then (2) walks every minute
  boundary since the last flush and emits a `CloseEvent` for each timeframe whose
  bar ends on that boundary.

Closing bars from the CLOCK rather than from the next tick means a quiet symbol
still closes its 5m/1H/1D bar on time, and the last bar of the session (15:29 ->
15:30) closes even though no tick ever arrives afterwards. Bars are persisted
before the events are emitted, so the rule engine always sees the finished bar.

Only 1-minute bars are stored. 1-second data is never persisted: ticks are
streamed to the browser and folded into the forming bar.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

from .candles import Candle
from .ticks import Tick
from .timeframes import TIMEFRAMES, bucket_end, bucket_start, exchange_of, in_session

MINUTE = timedelta(minutes=1)
MAX_CATCH_UP = timedelta(days=3)   # after a longer gap (e.g. process was asleep) don't replay boundaries


@dataclass(frozen=True)
class CloseEvent:
    """The bar of `timeframe` that started at `bar_start` has just finished."""

    symbol: str
    timeframe: str
    bar_start: datetime


class CandleBuilder:
    def __init__(
        self,
        persist: Callable[[str, Candle], None],
        tracked: Callable[[], Iterable[str]],
        allow_off_session: bool = False,
    ):
        self._persist = persist
        self._tracked = tracked
        self.allow_off_session = allow_off_session
        self._forming: dict[str, dict[datetime, Candle]] = {}
        self._last_volume: dict[str, int] = {}
        self._cursor: Optional[datetime] = None

    # ------------------------------------------------------------------- ticks
    def on_tick(self, tick: Tick) -> None:
        exchange = exchange_of(tick.symbol)
        if not self.allow_off_session and not in_session(tick.ts, exchange):
            return  # pre-open / post-close prints must not pollute session bars
        minute = bucket_start(tick.ts, "1m", exchange)
        delta = self._volume_delta(tick)
        bars = self._forming.setdefault(tick.symbol, {})
        bar = bars.get(minute)
        if bar is None:
            bars[minute] = Candle(minute, tick.price, tick.price, tick.price, tick.price, delta)
        else:
            bars[minute] = Candle(
                minute, bar.open, max(bar.high, tick.price), min(bar.low, tick.price), tick.price, bar.volume + delta
            )

    def _volume_delta(self, tick: Tick) -> int:
        if tick.volume is None:
            return 0
        last = self._last_volume.get(tick.symbol)
        self._last_volume[tick.symbol] = tick.volume
        if last is None:
            return 0                      # first sighting: we can't know how much traded this minute
        if tick.volume < last:
            return tick.volume            # the cumulative counter reset (new session)
        return tick.volume - last

    def forming_minutes(self, symbol: str) -> list[Candle]:
        """1-minute bars not yet persisted (the current minute), oldest first."""
        return [self._forming[symbol][m] for m in sorted(self._forming.get(symbol, {}))]

    # ------------------------------------------------------------------- clock
    def flush(self, now: datetime) -> list[CloseEvent]:
        self._close_finished_minutes(now)
        return self._bar_close_events(now)

    def _close_finished_minutes(self, now: datetime) -> None:
        for symbol in list(self._forming):
            bars = self._forming[symbol]
            for minute in sorted(bars):
                if minute + MINUTE <= now:
                    self._persist(symbol, bars.pop(minute))
            if not bars:
                del self._forming[symbol]

    def _bar_close_events(self, now: datetime) -> list[CloseEvent]:
        upto = now.replace(second=0, microsecond=0)
        if self._cursor is None or upto - self._cursor > MAX_CATCH_UP:
            self._cursor = upto
            return []
        events: list[CloseEvent] = []
        symbols = sorted(set(self._tracked()))
        boundary = self._cursor + MINUTE
        while boundary <= upto:
            last_minute = boundary - MINUTE
            for symbol in symbols:
                exchange = exchange_of(symbol)
                if not in_session(last_minute, exchange):
                    continue
                for tf in TIMEFRAMES:
                    start = bucket_start(last_minute, tf, exchange)
                    if bucket_end(start, tf, exchange) == boundary:
                        events.append(CloseEvent(symbol, tf, start))
            boundary += MINUTE
        self._cursor = upto
        return events
