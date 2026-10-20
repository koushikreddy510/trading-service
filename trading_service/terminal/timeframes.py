"""Timeframes, exchange sessions, and candle bucket boundaries.

Everything is stored and transported in UTC. Bars are cut on the exchange's
session clock in IST, because that is how brokers and TradingView cut them:

* an NSE 1H bar runs 09:15-10:15 (not 09:00-10:00);
* the last bar of a session is shorter (NSE 4H: 09:15-13:15 and 13:15-15:30);
* a daily bar opens at the session open, a weekly bar on Monday's open.

A candle is identified by its *bucket start*; it is COMPLETE once its bucket end
has passed. `is_complete` is the single definition of "completed candle" used by
the rule engine, so entries can never be generated from a bar still forming.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


@dataclass(frozen=True)
class Session:
    open: time
    close: time


_EQUITY = Session(time(9, 15), time(15, 30))
SESSIONS: dict[str, Session] = {
    "NSE": _EQUITY,
    "BSE": _EQUITY,
    "NFO": _EQUITY,
    "BFO": _EQUITY,
    "MCX": Session(time(9, 0), time(23, 30)),
}

INTRADAY_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1H": 60,
    "2H": 120,
    "4H": 240,
}
TIMEFRAMES: list[str] = [*INTRADAY_MINUTES, "1D", "1W"]


def validate_timeframe(tf: str) -> str:
    if tf not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe {tf!r}; expected one of {', '.join(TIMEFRAMES)}")
    return tf


def is_intraday(tf: str) -> bool:
    return tf in INTRADAY_MINUTES


def tf_seconds(tf: str) -> int:
    """Nominal bar length. Weekly/daily are calendar lengths (used only to
    extrapolate a line past the last bar, never to cut bars)."""
    validate_timeframe(tf)
    if tf == "1D":
        return 86400
    if tf == "1W":
        return 7 * 86400
    return INTRADAY_MINUTES[tf] * 60


def exchange_of(symbol: str) -> str:
    """'NSE:SBIN-EQ' -> 'NSE'. Bare symbols are assumed to be NSE."""
    return symbol.split(":", 1)[0].upper() if ":" in symbol else "NSE"


def session_of(exchange: str) -> Session:
    return SESSIONS.get(exchange.upper(), _EQUITY)


def _at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=IST).astimezone(UTC)


def session_open(day: date, exchange: str = "NSE") -> datetime:
    return _at(day, session_of(exchange).open)


def session_close(day: date, exchange: str = "NSE") -> datetime:
    return _at(day, session_of(exchange).close)


def in_session(ts: datetime, exchange: str = "NSE") -> bool:
    """True for Mon-Fri between session open (inclusive) and close (exclusive).
    Exchange holidays are not modelled; a holiday simply produces no ticks."""
    local = ts.astimezone(IST)
    if local.weekday() >= 5:
        return False
    return session_open(local.date(), exchange) <= ts < session_close(local.date(), exchange)


def bucket_start(ts: datetime, tf: str, exchange: str = "NSE") -> datetime:
    """Start (UTC) of the bar that contains `ts`."""
    validate_timeframe(tf)
    local_day = ts.astimezone(IST).date()
    if tf == "1W":
        monday = local_day - timedelta(days=local_day.weekday())
        return session_open(monday, exchange)
    day_open = session_open(local_day, exchange)
    if tf == "1D":
        return day_open
    step = INTRADAY_MINUTES[tf]
    elapsed_min = max(0, int((ts - day_open).total_seconds() // 60))
    return day_open + timedelta(minutes=(elapsed_min // step) * step)


def bucket_end(start: datetime, tf: str, exchange: str = "NSE") -> datetime:
    """Exclusive end of the bar that starts at `start`."""
    validate_timeframe(tf)
    local_day = start.astimezone(IST).date()
    if tf == "1W":
        friday = local_day + timedelta(days=4 - local_day.weekday())
        return session_close(friday, exchange)
    day_close = session_close(local_day, exchange)
    if tf == "1D":
        return day_close
    return min(start + timedelta(minutes=INTRADAY_MINUTES[tf]), day_close)


def is_complete(start: datetime, tf: str, now: datetime, exchange: str = "NSE") -> bool:
    """A bar is complete once its bucket end has passed."""
    return bucket_end(start, tf, exchange) <= now


def to_epoch(ts: datetime) -> int:
    return int(ts.timestamp())


def from_epoch(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)
