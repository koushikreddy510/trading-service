"""Schema setup."""
from trading_service.terminal.db import new_id


def test_schema_is_idempotent_and_creates_all_terminal_tables(db):
    db.ensure_schema()  # second run must not fail
    for table in (
        "terminal_watchlists", "terminal_watchlist_items", "terminal_drawings", "terminal_rules",
        "terminal_signals", "terminal_paper_positions", "terminal_ohlcv_1m",
        "terminal_broker_tokens", "terminal_screener_metrics",
    ):
        assert db.table_exists(table), table


def test_ids_are_short_and_unique():
    ids = {new_id() for _ in range(500)}
    assert len(ids) == 500 and all(len(i) == 12 for i in ids)
