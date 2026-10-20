"""Small builders shared by the terminal tests."""
from __future__ import annotations

from datetime import datetime

from trading_service.terminal.candles import Candle
from trading_service.terminal.timeframes import IST, UTC


def ist(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    """A wall-clock IST time as an aware UTC datetime (what the system stores)."""
    return datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(UTC)


def bar(start: datetime, o: float, h: float, l: float, c: float, v: int = 100) -> Candle:
    return Candle(start, o, h, l, c, v)


def closes_to_candles(closes: list[float], start: datetime, step_seconds: int, spread: float = 1.0) -> list[Candle]:
    """Candles whose open is the previous close and whose range is close +/- spread."""
    from datetime import timedelta

    out: list[Candle] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(Candle(start + timedelta(seconds=i * step_seconds), o, max(o, c) + spread, min(o, c) - spread, c, 100))
        prev = c
    return out


def hourly_bars(closes: list[float], day: tuple[int, int, int] = (2026, 10, 19), spread: float = 0.5, volume: int = 1000) -> list[Candle]:
    """NSE 1H bars (09:15 ... 15:15, seven per day, weekdays only) with the given closes.

    Each bar opens at the previous close and extends `spread` beyond its open/close,
    so highs and lows are predictable in assertions.
    """
    from datetime import date, timedelta

    d = date(*day)
    slots = [(9, 15), (10, 15), (11, 15), (12, 15), (13, 15), (14, 15), (15, 15)]
    out: list[Candle] = []
    prev = closes[0]
    i = 0
    while i < len(closes):
        if d.weekday() < 5:
            for h, m in slots:
                if i >= len(closes):
                    break
                c = closes[i]
                o = prev
                out.append(Candle(ist(d.year, d.month, d.day, h, m), o, max(o, c) + spread, min(o, c) - spread, c, volume))
                prev = c
                i += 1
        d += timedelta(days=1)
    return out
