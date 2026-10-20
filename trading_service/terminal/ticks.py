"""Ticks and the latest-quote board."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .timeframes import to_epoch


@dataclass(frozen=True)
class Tick:
    """One price update. `volume` is the CUMULATIVE traded volume for the day (as
    brokers send it); the candle builder turns it into per-bar volume."""

    symbol: str
    price: float
    ts: datetime
    volume: Optional[int] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    prev_close: Optional[float] = None


@dataclass
class Quote:
    symbol: str
    price: float
    ts: datetime
    prev_close: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    volume: Optional[int] = None

    @property
    def change(self) -> Optional[float]:
        return None if self.prev_close is None else self.price - self.prev_close

    @property
    def change_pct(self) -> Optional[float]:
        if self.prev_close in (None, 0):
            return None
        return (self.price - self.prev_close) / self.prev_close * 100.0

    def to_json(self) -> dict:
        return {
            "symbol": self.symbol, "price": self.price, "ts": to_epoch(self.ts), "prev_close": self.prev_close,
            "open": self.open, "high": self.high, "low": self.low, "volume": self.volume,
            "change": self.change, "change_pct": self.change_pct,
        }


class QuoteBoard:
    """Latest quote per symbol. Optional fields (open/high/low/prev close/volume) are
    kept from earlier ticks when a later tick does not carry them."""

    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}

    def update(self, tick: Tick) -> Quote:
        prev = self._quotes.get(tick.symbol)
        pick = lambda new, old: new if new is not None else old  # noqa: E731
        quote = Quote(
            symbol=tick.symbol,
            price=tick.price,
            ts=tick.ts,
            prev_close=pick(tick.prev_close, prev.prev_close if prev else None),
            open=pick(tick.open, prev.open if prev else None),
            high=pick(tick.high, prev.high if prev else None),
            low=pick(tick.low, prev.low if prev else None),
            volume=pick(tick.volume, prev.volume if prev else None),
        )
        self._quotes[tick.symbol] = quote
        return quote

    def get(self, symbol: str) -> Optional[Quote]:
        return self._quotes.get(symbol)

    def all(self) -> list[Quote]:
        return list(self._quotes.values())
