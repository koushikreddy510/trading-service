"""Symbol search over the data-store's `symbols` table (stocks) and `terminal_instruments` (MCX futures)."""
from __future__ import annotations

from . import instruments
from .db import Database


def search(db: Database, query: str, limit: int = 20) -> list[dict]:
    """Stocks and MCX contracts. A contract whose underlying is exactly the query ("silvermic") comes first, then
    stocks (exact/prefix matches first), then the other contracts that start with it."""
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(limit, 50))
    contracts = instruments.search(db, q, limit)
    exact = [c for c in contracts if c.pop("rank") == 0]
    rest = [c for c in contracts if c not in exact]
    return (exact + _stocks(db, q, limit) + rest)[:limit]


def _stocks(db: Database, q: str, limit: int) -> list[dict]:
    if not db.table_exists("symbols"):
        return []
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = db.rows(
        """
        SELECT tradingsymbol, name, sector
          FROM symbols
         WHERE tradingsymbol ILIKE :like OR name ILIKE :like
         ORDER BY
            CASE
              WHEN upper(tradingsymbol) = upper(:q) THEN 0
              WHEN upper(tradingsymbol) LIKE upper(:q) || '%' THEN 1
              WHEN upper(split_part(tradingsymbol, ':', 2)) LIKE upper(:q) || '%' THEN 1
              WHEN name ILIKE :q || '%' THEN 2
              ELSE 3
            END,
            length(tradingsymbol), tradingsymbol
         LIMIT :n
        """,
        like=like, q=q, n=limit,
    )
    return [
        {
            "symbol": r["tradingsymbol"],
            "exchange": r["tradingsymbol"].split(":", 1)[0] if ":" in r["tradingsymbol"] else "NSE",
            "name": r["name"] or "",
            "sector": r["sector"] or "",
        }
        for r in rows
    ]
