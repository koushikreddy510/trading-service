"""Candle value type and resampling."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from .timeframes import bucket_start, exchange_of, is_complete, to_epoch


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar. `time` is the bucket start in UTC."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def to_json(self) -> dict:
        return {
            "time": to_epoch(self.time),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def resample(bars: Iterable[Candle], tf: str, exchange: str = "NSE") -> list[Candle]:
    """Aggregate finer bars into `tf` bars (1m -> 15m, 1D -> 1W, ...).

    Open is the first bar's open, close the last bar's close, high/low the
    extremes, volume the sum. Input must be sorted by time. Buckets are cut on
    the exchange session clock (see `timeframes`). Output includes the trailing
    bucket even if it is still forming; use `completed` to drop it.
    """
    out: list[Candle] = []
    current: Candle | None = None
    current_start: datetime | None = None
    for bar in bars:
        start = bucket_start(bar.time, tf, exchange)
        if current is not None and start == current_start:
            current = Candle(
                time=start,
                open=current.open,
                high=max(current.high, bar.high),
                low=min(current.low, bar.low),
                close=bar.close,
                volume=current.volume + bar.volume,
            )
        else:
            if current is not None:
                out.append(current)
            current = Candle(start, bar.open, bar.high, bar.low, bar.close, bar.volume)
            current_start = start
    if current is not None:
        out.append(current)
    return out


def completed(candles: Sequence[Candle], tf: str, now: datetime, symbol: str = "NSE:") -> list[Candle]:
    """Only the bars whose bucket has fully elapsed at `now`.

    This is the guard the rule engine relies on: a forming bar is removed so a
    condition is only ever evaluated against finished candles.
    """
    exchange = exchange_of(symbol)
    return [c for c in candles if is_complete(c.time, tf, now, exchange)]
