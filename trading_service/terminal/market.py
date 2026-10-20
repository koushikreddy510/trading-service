"""Candle storage and the one place that answers "give me N candles of timeframe X".

Where each timeframe comes from:

* 1m                     stored closed bars (`terminal_ohlcv_1m`) + the minute still forming
* 5m ... 4H              resampled from those 1m bars on read (never stored separately)
* 1D                     the data-store's `ohlcv_1d` (the once-a-day job), plus today's
                         bar built from live 1m bars while the day is in progress
* 1W                     resampled from the daily bars

Because every timeframe derives from the same 1-minute rows, a 1H chart, a 15m rule
and the screener can never disagree about what a candle looked like.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from .builder import CandleBuilder
from .candles import Candle, resample
from .db import Database
from .feed.base import BrokerError, History
from .timeframes import (
    IST,
    INTRADAY_MINUTES,
    exchange_of,
    session_of,
    session_open,
    validate_timeframe,
)

log = logging.getLogger(__name__)

MAX_LOOKBACK_DAYS = 120          # how much 1-minute history one request may scan
DEFAULT_BACKFILL_DAYS = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MinuteStore:
    """Closed 1-minute bars in Postgres."""

    def __init__(self, db: Database):
        self.db = db

    def save(self, symbol: str, bars: list[Candle]) -> None:
        if not bars:
            return
        with self.db.tx() as conn:
            from sqlalchemy import text

            conn.execute(
                text(
                    """
                    INSERT INTO terminal_ohlcv_1m (symbol, ts, open, high, low, close, volume)
                    VALUES (:s, :t, :o, :h, :l, :c, :v)
                    ON CONFLICT (symbol, ts) DO UPDATE SET
                        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                        close = EXCLUDED.close, volume = EXCLUDED.volume
                    """
                ),
                [{"s": symbol, "t": b.time, "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume} for b in bars],
            )

    def load(self, symbol: str, start: datetime, end: Optional[datetime] = None) -> list[Candle]:
        sql = "SELECT ts, open, high, low, close, volume FROM terminal_ohlcv_1m WHERE symbol = :s AND ts >= :a"
        params: dict = {"s": symbol, "a": start}
        if end is not None:
            sql += " AND ts < :b"
            params["b"] = end
        sql += " ORDER BY ts"
        return [Candle(r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in self.db.rows(sql, **params)]

    def latest(self, symbol: str) -> Optional[datetime]:
        return self.db.scalar("SELECT max(ts) FROM terminal_ohlcv_1m WHERE symbol = :s", s=symbol)


class DailyStore:
    """Daily bars from the data-store's `ohlcv_1d` (written once a day by its Fyers job)."""

    def __init__(self, db: Database):
        self.db = db

    def load(self, symbol: str, limit: int) -> list[Candle]:
        if not self.db.table_exists("ohlcv_1d"):
            return []
        exchange = exchange_of(symbol)
        rows = self.db.rows(
            "SELECT date, open, high, low, close, volume FROM ohlcv_1d WHERE tradingsymbol = :s ORDER BY date DESC LIMIT :n",
            s=symbol, n=limit,
        )
        return [
            Candle(session_open(r["date"], exchange), r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in reversed(rows)
        ]


class CandleService:
    def __init__(
        self,
        db: Database,
        history: Optional[History] = None,
        builder: Optional[CandleBuilder] = None,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self.db = db
        self.history = history
        self.builder = builder
        self.clock = clock
        self.minutes = MinuteStore(db)
        self.daily = DailyStore(db)

    # ------------------------------------------------------------------ writes
    def store_minute(self, symbol: str, bar: Candle) -> None:
        """CandleBuilder's persist callback."""
        self.minutes.save(symbol, [bar])

    def backfill(self, symbol: str, days: int = DEFAULT_BACKFILL_DAYS) -> int:
        """Fetch closed 1-minute bars from the broker for the gap between what we have
        and now. Safe to call repeatedly. Returns the number of bars stored."""
        if self.history is None:
            return 0
        now = self.clock().replace(second=0, microsecond=0)
        earliest = now - timedelta(days=min(days, MAX_LOOKBACK_DAYS))
        last = self.minutes.latest(symbol)
        start = max(earliest, last + timedelta(minutes=1)) if last else earliest
        if start >= now:
            return 0
        return self._fetch_and_store(symbol, start, now)

    def repair(self, symbol: str, since: datetime) -> int:
        """Re-fetch closed 1-minute bars from `since` to now and OVERWRITE what is stored.

        `backfill` only continues after the newest stored bar, so it can never fix a hole in the middle. After the
        live feed was down, the minutes inside the outage are missing and the minute it dropped in was built from a
        partial set of ticks; the broker's own bars are the complete version of both."""
        if self.history is None:
            return 0
        now = self.clock().replace(second=0, microsecond=0)
        start = max(since.replace(second=0, microsecond=0), now - timedelta(days=MAX_LOOKBACK_DAYS))
        if start >= now:
            return 0
        return self._fetch_and_store(symbol, start, now)

    def _fetch_and_store(self, symbol: str, start: datetime, now: datetime) -> int:
        try:
            bars = self.history.fetch_1m(symbol, start, now)   # `now` is exclusive: the forming minute is never stored
        except BrokerError as e:
            log.warning("backfill %s failed: %s", symbol, e)
            return 0
        self.minutes.save(symbol, bars)
        return len(bars)

    # ------------------------------------------------------------------- reads
    def _forming(self, symbol: str) -> list[Candle]:
        return self.builder.forming_minutes(symbol) if self.builder else []

    def _lookback(self, symbol: str, tf: str, limit: int) -> timedelta:
        s = session_of(exchange_of(symbol))
        per_day = (s.close.hour * 60 + s.close.minute) - (s.open.hour * 60 + s.open.minute)
        bars_per_day = max(per_day / INTRADAY_MINUTES[tf], 1)
        trading_days = math.ceil(limit / bars_per_day)
        calendar_days = math.ceil(trading_days * 7 / 5) + 4       # weekends + a holiday buffer
        return timedelta(days=min(calendar_days, MAX_LOOKBACK_DAYS))

    def candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        """Up to `limit` newest candles, oldest first. The last one may still be forming;
        callers that must not act on it use `candles.completed` (the rule engine does)."""
        validate_timeframe(timeframe)
        limit = max(1, min(limit, 5000))
        exchange = exchange_of(symbol)
        now = self.clock()
        if timeframe in INTRADAY_MINUTES:
            start = now - self._lookback(symbol, timeframe, limit)
            minutes = self._merge(self.minutes.load(symbol, start), self._forming(symbol))
            bars = minutes if timeframe == "1m" else resample(minutes, timeframe, exchange)
            return bars[-limit:]
        daily = self._daily_with_today(symbol, limit if timeframe == "1D" else limit * 5 + 5, now)
        bars = daily if timeframe == "1D" else resample(daily, "1W", exchange)
        return bars[-limit:]

    @staticmethod
    def _merge(stored: list[Candle], forming: list[Candle]) -> list[Candle]:
        by_time = {b.time: b for b in stored}
        by_time.update({b.time: b for b in forming})
        return [by_time[t] for t in sorted(by_time)]

    def _daily_with_today(self, symbol: str, limit: int, now: datetime) -> list[Candle]:
        exchange = exchange_of(symbol)
        bars = {b.time: b for b in self.daily.load(symbol, limit)}
        today = now.astimezone(IST).date()
        today_open = session_open(today, exchange)
        minutes = self._merge(self.minutes.load(symbol, today_open), self._forming(symbol))
        minutes = [m for m in minutes if m.time >= today_open]
        if minutes:
            (today_bar,) = resample(minutes, "1D", exchange)
            bars[today_bar.time] = today_bar     # live minutes win over a stale/partial daily row
        return [bars[t] for t in sorted(bars)]
