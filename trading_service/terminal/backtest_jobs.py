"""Running backtests in the background and keeping their reports.

A backtest can take minutes, so `start` validates the request, stores a row with status 'running' and returns its id
at once; a daemon thread fills in the report. Clients poll `get`. A process restart abandons running backtests, which
`fail_stale` marks as failed at start-up so nothing stays 'running' forever.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Mapping, Optional

from .backtest import BacktestError, parse_params, run_params
from .db import Database, jdump, new_id
from .drawings import LOCAL_USER

log = logging.getLogger(__name__)

MAX_RUNNING = 2


class BacktestStore:
    def __init__(self, db: Database, user: str = LOCAL_USER):
        self.db, self.user = db, user

    def fail_stale(self) -> int:
        return self.db.execute("UPDATE terminal_backtests SET status = 'failed', error = 'interrupted by a restart', finished_at = now() WHERE status = 'running'")

    def list(self, limit: int = 50) -> list[dict]:
        """Newest first, without the (large) report: summary only."""
        rows = self.db.rows(
            "SELECT id, account, status, progress, error, params, result -> 'summary' AS summary, created_at, finished_at "
            "FROM terminal_backtests WHERE user_id = :u ORDER BY created_at DESC LIMIT :n", u=self.user, n=max(1, min(limit, 200)))
        return [_json(r) for r in rows]

    def get(self, backtest_id: str) -> Optional[dict]:
        r = self.db.row("SELECT id, account, status, progress, error, params, result, created_at, finished_at FROM terminal_backtests WHERE id = :i AND user_id = :u", i=backtest_id, u=self.user)
        return _json(r) if r else None

    def delete(self, backtest_id: str) -> bool:
        return self.db.execute("DELETE FROM terminal_backtests WHERE id = :i AND user_id = :u", i=backtest_id, u=self.user) > 0

    def start(self, raw: Mapping[str, Any], on_done=None) -> str:
        """Validate, record and launch. Raises BacktestError for a bad request or when too many are running."""
        params = parse_params(self.db, raw)
        if self.db.scalar("SELECT count(*) FROM terminal_backtests WHERE status = 'running'") >= MAX_RUNNING:
            raise BacktestError(f"{MAX_RUNNING} backtests are already running; wait for one to finish")
        backtest_id = new_id()
        self.db.execute(
            "INSERT INTO terminal_backtests (id, user_id, account, params) VALUES (:i, :u, :a, CAST(:p AS JSONB))",
            i=backtest_id, u=self.user, a=params.account, p=jdump(params.to_json()))

        def work() -> None:
            try:
                result = run_params(self.db, params, lambda day: self.db.execute("UPDATE terminal_backtests SET progress = :d WHERE id = :i", d=day, i=backtest_id))
                self.db.execute("UPDATE terminal_backtests SET status = 'done', result = CAST(:r AS JSONB), finished_at = now() WHERE id = :i", r=jdump(result), i=backtest_id)
            except Exception as e:                     # noqa: BLE001 - whatever went wrong is the user's answer
                log.exception("backtest %s failed", backtest_id)
                self.db.execute("UPDATE terminal_backtests SET status = 'failed', error = :e, finished_at = now() WHERE id = :i", e=str(e)[:500], i=backtest_id)
            if on_done:
                on_done(self.get(backtest_id))

        threading.Thread(target=work, name=f"backtest-{backtest_id}", daemon=True).start()
        return backtest_id


def _json(r: dict) -> dict:
    for k in ("created_at", "finished_at"):
        r[k] = r[k].isoformat() if r.get(k) else None
    return r
