"""Shared pytest fixtures.

The terminal tests run against a REAL Postgres so SQL, JSONB and constraints are
exercised exactly as in production. A database is found in this order:

1. ``TEST_DATABASE_URL`` - e.g. the data-store docker-compose database
   (``postgresql://market_user:market_pass@localhost:5432/market``). Every test gets
   its own throwaway *schema* inside it, so no real table is ever touched.
2. A throwaway cluster started from the Postgres binaries found on this machine
   (``initdb``/``pg_ctl`` on PATH, or /usr/lib/postgresql/*/bin, or Homebrew).
3. Otherwise DB tests are skipped with a message saying how to enable them.
"""
from __future__ import annotations

import getpass
import glob
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "terminal" / "fixtures"


def _find_pg_bin() -> str | None:
    on_path = shutil.which("initdb")
    if on_path:
        return str(Path(on_path).parent)
    patterns = [
        "/usr/lib/postgresql/*/bin",
        "/opt/homebrew/opt/postgresql*/bin",
        "/usr/local/opt/postgresql*/bin",
        "/Applications/Postgres.app/Contents/Versions/*/bin",
    ]
    for pattern in patterns:
        found = sorted(glob.glob(pattern))
        if found and Path(found[-1], "initdb").exists():
            return found[-1]
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def no_real_slack(monkeypatch):
    """A developer's webhook in the environment must never receive messages from a test run."""
    import os

    for name in [n for n in os.environ if n.startswith("SLACK_") and n.endswith("WEBHOOK_URL") or n.startswith("SLACK_WEBHOOK_URL")] + ["TRADING_SLACK_WEBHOOK_URL"]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def pg_server_url():
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        yield url
        return
    pg_bin = _find_pg_bin()
    if not pg_bin:
        pytest.skip("No Postgres available: set TEST_DATABASE_URL or install PostgreSQL (initdb/pg_ctl)")
    base = tempfile.mkdtemp(prefix="terminal-pg-")
    data = os.path.join(base, "data")
    port = _free_port()
    as_root = hasattr(os, "geteuid") and os.geteuid() == 0
    prefix: list[str] = []
    if as_root:  # postgres refuses to run as root
        import pwd

        pwd.getpwnam("postgres")
        shutil.chown(base, "postgres", "postgres")
        os.chmod(base, 0o755)
        prefix = ["runuser", "-u", "postgres", "--"]
    user = "postgres" if as_root else getpass.getuser()
    subprocess.run([*prefix, f"{pg_bin}/initdb", "-D", data, "-A", "trust", "-U", user], check=True, capture_output=True)
    subprocess.run(
        [*prefix, f"{pg_bin}/pg_ctl", "-D", data, "-o", f"-p {port} -k {base} -c listen_addresses=127.0.0.1", "-l", os.path.join(base, "log"), "-w", "start"],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql+psycopg2://{user}@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run([*prefix, f"{pg_bin}/pg_ctl", "-D", data, "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture()
def db(pg_server_url):
    """A `Database` bound to a fresh schema containing the data-store tables
    plus the terminal schema. Dropped after the test."""
    from trading_service.terminal.db import Database

    url = pg_server_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    schema = "t_" + uuid.uuid4().hex[:10]
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-c search_path={schema}"})
    database = Database(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql((FIXTURES / "market_schema.sql").read_text())
    database.ensure_schema()
    try:
        yield database
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
