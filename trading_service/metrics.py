"""
Prometheus metrics for PnL, positions, and orders.
Exposed at /metrics for Prometheus scraping.
"""
from prometheus_client import (
    REGISTRY,
    generate_latest,
    CONTENT_TYPE_LATEST,
    Gauge,
)

# PnL gauges (latest snapshot per account)
gauge_pnl_total = Gauge(
    "trading_pnl_total",
    "Total PnL (realized + unrealized) in INR",
    ["account_id", "display_name", "broker"],
)
gauge_pnl_realized = Gauge(
    "trading_pnl_realized",
    "Realized PnL in INR",
    ["account_id", "display_name", "broker"],
)
gauge_pnl_unrealized = Gauge(
    "trading_pnl_unrealized",
    "Unrealized PnL in INR",
    ["account_id", "display_name", "broker"],
)

# Position count per account
gauge_positions_count = Gauge(
    "trading_positions_count",
    "Number of open positions",
    ["account_id", "display_name", "broker"],
)

# Per-position unrealized PnL (for drill-down)
gauge_position_pnl = Gauge(
    "trading_position_unrealized_pnl",
    "Unrealized PnL per position in INR",
    ["account_id", "display_name", "symbol", "side"],
)

# Order counts (optional)
gauge_orders_pending = Gauge(
    "trading_orders_pending_count",
    "Count of pending/ open orders",
    ["account_id", "display_name"],
)


def refresh_metrics():
    """Query DB and update all gauges. Call before /metrics scrape."""
    try:
        from trading_service.db import _conn
        from sqlalchemy import text
    except Exception:
        return

    try:
        with _conn() as conn:
            # Latest PnL snapshot per account (use most recent snapshot_date)
            rows = conn.execute(text("""
                SELECT DISTINCT ON (s.account_id)
                    s.account_id, a.display_name, a.broker,
                    s.realized_pnl, s.unrealized_pnl, s.total_pnl
                FROM trading_pnl_snapshots s
                JOIN trading_accounts a ON a.id = s.account_id
                ORDER BY s.account_id, s.snapshot_date DESC
            """)).fetchall()
            for r in rows:
                aid, name, broker = str(r[0]), r[1] or "", r[2] or ""
                gauge_pnl_total.labels(account_id=aid, display_name=name, broker=broker).set(float(r[4]) or 0)
                gauge_pnl_realized.labels(account_id=aid, display_name=name, broker=broker).set(float(r[2]) or 0)
                gauge_pnl_unrealized.labels(account_id=aid, display_name=name, broker=broker).set(float(r[3]) or 0)

            # Position count per account
            pos_counts = conn.execute(text("""
                SELECT p.account_id, a.display_name, a.broker, COUNT(*) as cnt
                FROM trading_positions p
                JOIN trading_accounts a ON a.id = p.account_id
                GROUP BY p.account_id, a.display_name, a.broker
            """)).fetchall()
            for r in pos_counts:
                aid, name, broker = str(r[0]), r[1] or "", r[2] or ""
                gauge_positions_count.labels(account_id=aid, display_name=name, broker=broker).set(r[3])

            # Per-position unrealized PnL
            positions = conn.execute(text("""
                SELECT p.account_id, a.display_name, p.symbol, p.side, p.unrealized_pnl
                FROM trading_positions p
                JOIN trading_accounts a ON a.id = p.account_id
            """)).fetchall()
            for r in positions:
                aid, name, sym, side = str(r[0]), r[1] or "", r[2] or "", r[3] or ""
                gauge_position_pnl.labels(account_id=aid, display_name=name, symbol=sym, side=side).set(float(r[4]) or 0)

            # Pending orders count per account
            order_counts = conn.execute(text("""
                SELECT o.account_id, a.display_name, COUNT(*) as cnt
                FROM trading_orders o
                JOIN trading_accounts a ON a.id = o.account_id
                WHERE o.status IN ('PENDING', 'PLACED', 'OPEN')
                GROUP BY o.account_id, a.display_name
            """)).fetchall()
            for r in order_counts:
                aid, name = str(r[0]), r[1] or ""
                gauge_orders_pending.labels(account_id=aid, display_name=name).set(r[2])
    except Exception:
        pass  # Don't fail scrape if DB is unavailable


def get_metrics_output():
    """Refresh and return Prometheus exposition format."""
    refresh_metrics()
    return generate_latest(REGISTRY)
