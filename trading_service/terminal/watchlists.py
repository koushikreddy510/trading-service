"""Watchlists: named, ordered lists of symbols."""
from __future__ import annotations

import re
from typing import Iterable, Optional

from .db import Database, new_id
from .drawings import LOCAL_USER

SYMBOL_RE = re.compile(r"^[A-Z]{2,5}:[A-Z0-9&_.\-]{1,40}$")
DEFAULT_NAME = "Watchlist"
MAX_NAME = 60
MAX_ITEMS = 500


class WatchlistError(ValueError):
    pass


def normalise_symbol(raw: object) -> str:
    """'nse:sbin-eq ' -> 'NSE:SBIN-EQ'; anything not shaped EXCHANGE:SYMBOL is rejected."""
    symbol = str(raw or "").strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise WatchlistError(f"{raw!r} is not a symbol like NSE:SBIN-EQ")
    return symbol


def _clean_name(raw: object) -> str:
    name = str(raw or "").strip()
    if not name:
        raise WatchlistError("name is required")
    if len(name) > MAX_NAME:
        raise WatchlistError(f"name is longer than {MAX_NAME} characters")
    return name


class WatchlistRepo:
    def __init__(self, db: Database, user: str = LOCAL_USER):
        self.db = db
        self.user = user

    # ------------------------------------------------------------------ reads
    def _items(self, ids: list[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {i: [] for i in ids}
        if not ids:
            return out
        rows = self.db.rows(
            "SELECT watchlist_id, symbol FROM terminal_watchlist_items WHERE watchlist_id = ANY(:ids) "
            "ORDER BY watchlist_id, position, added_at, symbol",
            ids=ids,
        )
        for r in rows:
            out[r["watchlist_id"]].append(r["symbol"])
        return out

    def list(self) -> list[dict]:
        lists = self.db.rows(
            "SELECT id, name FROM terminal_watchlists WHERE user_id = :u ORDER BY position, created_at, name", u=self.user
        )
        items = self._items([w["id"] for w in lists])
        return [{"id": w["id"], "name": w["name"], "symbols": items[w["id"]]} for w in lists]

    def get(self, watchlist_id: str) -> Optional[dict]:
        row = self.db.row("SELECT id, name FROM terminal_watchlists WHERE id = :i AND user_id = :u", i=watchlist_id, u=self.user)
        if not row:
            return None
        return {"id": row["id"], "name": row["name"], "symbols": self._items([row["id"]])[row["id"]]}

    def all_symbols(self) -> set[str]:
        rows = self.db.rows(
            "SELECT DISTINCT i.symbol FROM terminal_watchlist_items i JOIN terminal_watchlists w ON w.id = i.watchlist_id "
            "WHERE w.user_id = :u",
            u=self.user,
        )
        return {r["symbol"] for r in rows}

    # ----------------------------------------------------------------- writes
    def ensure_default(self, starter_symbols: Iterable[object] = ()) -> dict:
        """The first watchlist, created on first use. `starter_symbols` fill it only when it is created,
        so symbols the user later removes do not come back on restart."""
        existing = self.list()
        return existing[0] if existing else self.create(DEFAULT_NAME, starter_symbols)

    def create(self, name: object, symbols: Iterable[object] = ()) -> dict:
        name = _clean_name(name)
        if self.db.row("SELECT 1 AS x FROM terminal_watchlists WHERE user_id = :u AND lower(name) = lower(:n)", u=self.user, n=name):
            raise WatchlistError(f"a watchlist named {name!r} already exists")
        wid = new_id()
        position = (self.db.scalar("SELECT coalesce(max(position), -1) + 1 FROM terminal_watchlists WHERE user_id = :u", u=self.user)) or 0
        self.db.execute(
            "INSERT INTO terminal_watchlists (id, user_id, name, position) VALUES (:i, :u, :n, :p)",
            i=wid, u=self.user, n=name, p=position,
        )
        if symbols:
            self.add(wid, symbols)
        return self.get(wid)  # type: ignore[return-value]

    def rename(self, watchlist_id: str, name: object) -> bool:
        name = _clean_name(name)
        clash = self.db.row(
            "SELECT 1 AS x FROM terminal_watchlists WHERE user_id = :u AND lower(name) = lower(:n) AND id <> :i",
            u=self.user, n=name, i=watchlist_id,
        )
        if clash:
            raise WatchlistError(f"a watchlist named {name!r} already exists")
        return self.db.execute(
            "UPDATE terminal_watchlists SET name = :n WHERE id = :i AND user_id = :u", n=name, i=watchlist_id, u=self.user
        ) > 0

    def delete(self, watchlist_id: str) -> bool:
        return self.db.execute("DELETE FROM terminal_watchlists WHERE id = :i AND user_id = :u", i=watchlist_id, u=self.user) > 0

    def add(self, watchlist_id: str, symbols: Iterable[object]) -> list[str]:
        """Append symbols (skipping ones already present). Returns the symbols actually added."""
        wl = self.get(watchlist_id)
        if wl is None:
            raise WatchlistError("watchlist not found")
        wanted = list(dict.fromkeys(normalise_symbol(s) for s in symbols))
        new = [s for s in wanted if s not in wl["symbols"]]
        if len(wl["symbols"]) + len(new) > MAX_ITEMS:
            raise WatchlistError(f"a watchlist holds at most {MAX_ITEMS} symbols")
        base = len(wl["symbols"])
        for i, s in enumerate(new):
            self.db.execute(
                "INSERT INTO terminal_watchlist_items (watchlist_id, symbol, position) VALUES (:w, :s, :p) ON CONFLICT DO NOTHING",
                w=watchlist_id, s=s, p=base + i,
            )
        return new

    def remove(self, watchlist_id: str, symbol: object) -> bool:
        if self.get(watchlist_id) is None:
            return False
        return self.db.execute(
            "DELETE FROM terminal_watchlist_items WHERE watchlist_id = :w AND symbol = :s", w=watchlist_id, s=normalise_symbol(symbol)
        ) > 0

    def reorder(self, watchlist_id: str, symbols: Iterable[object]) -> list[str]:
        """Set the order. Symbols not mentioned keep their relative order after the listed ones."""
        wl = self.get(watchlist_id)
        if wl is None:
            raise WatchlistError("watchlist not found")
        given = [s for s in dict.fromkeys(normalise_symbol(s) for s in symbols) if s in wl["symbols"]]
        order = given + [s for s in wl["symbols"] if s not in given]
        for i, s in enumerate(order):
            self.db.execute(
                "UPDATE terminal_watchlist_items SET position = :p WHERE watchlist_id = :w AND symbol = :s", p=i, w=watchlist_id, s=s
            )
        return order
