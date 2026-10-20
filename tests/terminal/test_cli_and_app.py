"""The scheduled scan command and the way the terminal plugs into the existing service."""
import importlib
import json
import sys
from datetime import date, timedelta

import pytest

from trading_service.terminal import __main__ as cli
from trading_service.terminal import db as dbmod


def test_the_scan_command_runs_the_daily_scan_and_prints_a_summary(db, capsys, monkeypatch):
    for i in range(30):
        db.execute(
            "INSERT INTO ohlcv_1d (tradingsymbol, date, open, high, low, close, volume) VALUES ('NSE:AAA-EQ', :d, 1, 1, 1, :c, 10)",
            d=date(2026, 9, 1) + timedelta(days=i), c=100 + i,
        )
    monkeypatch.setattr(cli, "get_db", lambda: db)
    assert cli.main(["scan"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["stored"] == 1 and summary["scanned"] == 1


def test_the_existing_service_still_starts_and_serves_terminal_routes(monkeypatch):
    """main.py gains the terminal without losing its own routes. With no DATABASE_URL the
    terminal cannot start, and the service must carry on (terminal answers 503)."""
    from fastapi.testclient import TestClient

    monkeypatch.delenv("DATABASE_URL", raising=False)
    dbmod.set_db(None)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    with TestClient(main.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/terminal/status").status_code == 503
    paths = set(main.app.openapi()["paths"])
    assert "/trading/accounts" in paths and "/terminal/candles" in paths
