"""Symbol search over the data-store's `symbols` table."""
from __future__ import annotations

from .db import Database


def search(db: Database, query: str, limit: int = 20) -> list[dict]:
    """Find symbols by trading symbol or company name. Exact/prefix matches rank first.
    Returns [] when the data-store table is not present."""
    q = (query or "").strip()
    if not q or not db.table_exists("symbols"):
        return []
    limit = max(1, min(limit, 50))
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
