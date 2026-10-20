"""Our `NSE:SBIN-EQ` ids -> what Paytm Money's order API wants: exchange, segment and its numeric security_id.

Paytm publishes the mapping as a public CSV (its "scrip master"). Column names follow Paytm's own SDK and OpenAlgo's
reader of that file (exchange, instrument_type, security_id, symbol); this was not run against the live file.

Fyers needs no resolver: our canonical ids are Fyers tickers. Flattrade's is `terminal/feed/flattrade.py` (TokenResolver).
shortcut: cash equities and ETFs only (no F&O, indices, MCX), and the master is loaded once per process;
call reload() or restart to pick up new listings. Extend parse_master for derivatives when orders need them.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Callable, Optional

MASTER_URL = "https://developer.paytmmoney.com/data/v1/scrips/security_master.csv"
EQUITY_TYPES = ("ES", "ETF")          # Paytm's instrument_type for shares / ETFs


@dataclass(frozen=True)
class PaytmInstrument:
    exchange: str      # NSE | BSE
    segment: str       # "E" = equity cash ("D" = derivatives, not handled)
    security_id: str
    symbol: str        # Paytm's own symbol, e.g. SBIN


def parse_master(text: str) -> dict[tuple[str, str], PaytmInstrument]:
    out: dict[tuple[str, str], PaytmInstrument] = {}
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if row.get("instrument_type") not in EQUITY_TYPES or row.get("exchange") not in ("NSE", "BSE"):
            continue
        # the same symbol can appear in several series; the first row wins
        out.setdefault((row["exchange"], row["symbol"]), PaytmInstrument(row["exchange"], "E", row["security_id"], row["symbol"]))
    return out


def download_master() -> str:
    import requests

    r = requests.get(MASTER_URL, timeout=60)
    r.raise_for_status()
    return r.text


class PaytmInstruments:
    def __init__(self, loader: Callable[[], str] = download_master):
        self._loader = loader
        self._by_key: Optional[dict[tuple[str, str], PaytmInstrument]] = None

    def reload(self) -> None:
        self._by_key = parse_master(self._loader())

    def resolve(self, symbol: str) -> PaytmInstrument:
        exchange, _, name = symbol.partition(":")
        if not name:
            exchange, name = "NSE", symbol
        if exchange not in ("NSE", "BSE"):
            raise ValueError(f"{symbol}: Paytm Money has no {exchange} equities here (NSE and BSE only)")
        if not name.endswith("-EQ"):
            raise ValueError(f"{symbol}: only plain equity symbols like NSE:SBIN-EQ are supported")
        if self._by_key is None:
            self.reload()
        found = self._by_key.get((exchange, name[:-3]))  # type: ignore[union-attr]
        if found is None:
            raise ValueError(f"{symbol}: not in Paytm Money's security master")
        return found
