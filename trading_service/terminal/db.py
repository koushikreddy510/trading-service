"""Tiny SQL layer for the terminal package.

Plain SQL through SQLAlchemy, no ORM: every query in this package is visible in
the module that runs it, which keeps the repositories easy to read and test.

Tests build a `Database` bound to a throwaway schema; the running app builds one
from DATABASE_URL, the same variable `trading_service.db` already uses.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def new_id() -> str:
    """Short random id used for watchlists, drawings, rules, signals, positions."""
    return uuid.uuid4().hex[:12]


def jdump(value: Any) -> str:
    """Serialise a value for a `CAST(:x AS JSONB)` parameter."""
    return json.dumps(value, separators=(",", ":"), default=str)


class Database:
    def __init__(self, engine: Engine):
        self.engine = engine

    @classmethod
    def from_url(cls, url: str) -> "Database":
        return cls(create_engine(url, pool_pre_ping=True))

    # ------------------------------------------------------------------ schema
    def ensure_schema(self) -> None:
        """Create the terminal tables.  Execute via the raw DBAPI cursor so the
        multi-statement DDL string is never paired with the empty immutabledict
        parameters object that some psycopg2/SQLAlchemy combinations reject."""
        sql = SCHEMA_FILE.read_text()
        with self.engine.begin() as conn:
            conn.connection.cursor().execute(sql)

    # ----------------------------------------------------------------- queries
    def rows(self, sql: str, **params: Any) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]

    def row(self, sql: str, **params: Any) -> Optional[dict]:
        found = self.rows(sql, **params)
        return found[0] if found else None

    def scalar(self, sql: str, **params: Any) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def write_rows(self, sql: str, **params: Any) -> list[dict]:
        """Run one write statement that returns rows (INSERT ... RETURNING) and COMMIT it.
        `rows` is read-only: it never commits, so writes must not go through it."""
        with self.engine.begin() as conn:
            return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]

    def execute(self, sql: str, **params: Any) -> int:
        """Run one write statement in its own transaction; returns rows affected."""
        with self.engine.begin() as conn:
            return conn.execute(text(sql), params).rowcount

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        """A transaction for multi-statement writes (commits on exit)."""
        with self.engine.begin() as conn:
            yield conn

    def table_exists(self, name: str) -> bool:
        return bool(self.scalar("SELECT to_regclass(:n) IS NOT NULL", n=name))


_default: Optional[Database] = None


def get_db() -> Database:
    """Process-wide database, created lazily from DATABASE_URL."""
    global _default
    if _default is None:
        url = os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        _default = Database.from_url(url)
    return _default


def set_db(db: Optional[Database]) -> None:
    """Install (or clear) the process-wide database. Used by tests and app setup."""
    global _default
    _default = db
