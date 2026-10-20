"""The two things the terminal needs from a broker, as small protocols.

* `Feed`    - a websocket-style stream of `Tick`s for the symbols it is told to watch.
* `History` - closed 1-minute bars for a time range (used to backfill the chart and
              to fill any gap left while the app was not running).

Implementations: `MockFeed`/`MockHistory` (demo mode, no broker needed) and
`FlattradeFeed`/`FlattradeHistory`. Nothing else in the package knows which one is in use.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional, Protocol, Sequence

from ..candles import Candle
from ..ticks import Tick

TickHandler = Callable[[Tick], None]
StatusHandler = Callable[[str], None]   # 'connecting' | 'connected' | 'reconnecting' | 'disconnected'


class Feed(Protocol):
    async def run(self, on_tick: TickHandler) -> None:
        """Stream ticks to `on_tick` until cancelled (reconnecting on failure)."""

    async def subscribe(self, symbols: Sequence[str]) -> None: ...

    async def unsubscribe(self, symbols: Sequence[str]) -> None: ...


class History(Protocol):
    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        """Closed 1-minute bars with start in [start, end), oldest first. Blocking."""


class BrokerError(RuntimeError):
    """The broker rejected a request or could not be reached."""
