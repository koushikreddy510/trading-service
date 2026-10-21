"""Contracts that are not in the data-store's stock list: MCX commodity futures (and MCX index futures).

Source: Flattrade's public scrip master (no login needed), refreshed by a daily job and on first start. Each row is
one monthly contract, and its id is Flattrade's own trading symbol, so the feed and history need no mapping:

    MCX:SILVERMIC30NOV26   underlying SILVERMIC, expiry 2026-11-30, lot size 1

Expired contracts are deleted on refresh. The token is also written to `terminal_broker_tokens`, so the live feed
subscribes without a SearchScrip call.

Quantities for these contracts are in LOTS. `point_value` is the rupees one lot makes or loses when the price moves
by 1.00: lot size x Flattrade's price factor (`prcftr`). GOLDM is quoted per 10 g but traded in grams (100 x 0.1 = 10),
ALUMINI is one tonne quoted per kg (1 x 1000), SILVERM is 5 kg quoted per kg (5 x 1). The price factor is not in the
public file; it is filled in from GetSecurityInfo the first time a contract is used (see FlattradeHistory.ensure_spec).
Stocks have no row here and a point value of 1.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from typing import Callable, Optional

from .db import Database
from .timeframes import IST, exchange_of

log = logging.getLogger(__name__)

MCX_SCRIP_MASTER = "https://flattrade.s3.ap-south-1.amazonaws.com/scripmaster/Commodity.csv"
FUTURES = {"FUTCOM", "FUTIDX"}      # options (OPTFUT / OPTIDX, ~16,000 rows) are left out for now
SECTOR = {"FUTCOM": "Commodity future", "FUTIDX": "Index future"}


def _download(url: str) -> str:
    import requests

    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.text


def parse_scrip_master(text: str, today: date) -> list[dict]:
    """Futures rows of Flattrade's Commodity.csv that have not expired:
    Exchange,Token,Lotsize,Symbol,Tradingsymbol,Instrument,Expiry,Strike,Optiontype"""
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        if (r.get("Instrument") or "").strip() not in FUTURES:
            continue
        try:
            expiry = datetime.strptime(r["Expiry"].strip(), "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        if expiry < today:
            continue
        exchange, tsym = r["Exchange"].strip().upper(), r["Tradingsymbol"].strip().upper()
        out.append({
            "symbol": f"{exchange}:{tsym}", "exchange": exchange, "token": r["Token"].strip(), "tsym": tsym,
            "underlying": r["Symbol"].strip().upper(), "instrument": r["Instrument"].strip(), "expiry": expiry,
            "lot_size": int(float(r.get("Lotsize") or 1)),
        })
    return out


def refresh(db: Database, fetch: Callable[[str], str] = _download, today: Optional[date] = None) -> dict:
    """Load the current MCX futures; drop expired ones. Returns counts for the job log."""
    today = today or datetime.now(IST).date()
    rows = parse_scrip_master(fetch(MCX_SCRIP_MASTER), today)
    if not rows:
        raise RuntimeError("the MCX scrip master had no futures rows; keeping the current list")
    from sqlalchemy import text

    with db.tx() as conn:
        conn.execute(text(
            """
            INSERT INTO terminal_instruments (symbol, exchange, token, tsym, underlying, instrument, expiry, lot_size)
            VALUES (:symbol, :exchange, :token, :tsym, :underlying, :instrument, :expiry, :lot_size)
            ON CONFLICT (symbol) DO UPDATE SET token = EXCLUDED.token, underlying = EXCLUDED.underlying,
                instrument = EXCLUDED.instrument, expiry = EXCLUDED.expiry, lot_size = EXCLUDED.lot_size, updated_at = now()
            """
        ), rows)
        conn.execute(text(
            """
            INSERT INTO terminal_broker_tokens (symbol, exchange, token, tsym) VALUES (:symbol, :exchange, :token, :tsym)
            ON CONFLICT (symbol) DO UPDATE SET token = EXCLUDED.token, tsym = EXCLUDED.tsym, resolved_at = now()
            """
        ), rows)
        removed = conn.execute(text("DELETE FROM terminal_instruments WHERE expiry < :d"), {"d": today}).rowcount
    return {"contracts": len(rows), "underlyings": len({r["underlying"] for r in rows}), "expired_removed": removed}


def search(db: Database, query: str, limit: int) -> list[dict]:
    """Contracts whose underlying or trading symbol starts with the query; the exact underlying first, then the
    nearest expiry (the contract people actually trade)."""
    q = (query or "").strip().upper()
    if not q or not db.table_exists("terminal_instruments"):
        return []
    like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = db.rows(
        """
        SELECT symbol, exchange, underlying, instrument, expiry, lot_size
          FROM terminal_instruments
         WHERE underlying LIKE :like OR tsym LIKE :like
         ORDER BY (underlying = :q) DESC, length(underlying), underlying, expiry
         LIMIT :n
        """,
        like=like, q=q, n=limit,
    )
    return [
        {
            "symbol": r["symbol"], "exchange": r["exchange"],
            "name": f"{r['underlying']} {r['expiry']:%d %b %Y} · lot {r['lot_size']}",
            "sector": SECTOR.get(r["instrument"], r["instrument"]),
            "rank": 0 if r["underlying"] == q else 1,
        }
        for r in rows
    ]


class Instruments:
    """Point value (rupees per 1.00 move per unit of quantity) for the paper book. Cached once the price factor is known."""

    def __init__(self, db: Database):
        self.db = db
        self._known: dict[str, float] = {}

    def spec(self, symbol: str) -> Optional[dict]:
        if exchange_of(symbol) in ("NSE", "BSE") or not self.db.table_exists("terminal_instruments"):
            return None
        return self.db.row("SELECT symbol, exchange, token, lot_size, price_factor FROM terminal_instruments WHERE symbol = :s", s=symbol)

    def needs_price_factor(self, symbol: str) -> bool:
        row = self.spec(symbol)
        return row is not None and row["price_factor"] is None

    def set_price_factor(self, symbol: str, factor: float) -> None:
        self.db.execute("UPDATE terminal_instruments SET price_factor = :f WHERE symbol = :s", f=factor, s=symbol)
        self._known.pop(symbol, None)

    def point_value(self, symbol: str) -> float:
        if symbol in self._known:
            return self._known[symbol]
        row = self.spec(symbol)
        if row is None:
            return 1.0
        if row["price_factor"] is None:
            log.warning("%s: price factor not known yet; P&L assumes 1 per point per unit of lot size", symbol)
            return float(row["lot_size"])
        self._known[symbol] = float(row["lot_size"]) * float(row["price_factor"])
        return self._known[symbol]
