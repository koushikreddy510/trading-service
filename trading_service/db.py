"""
Trading service DB: accounts, orders, positions, PnL snapshots.
Set DATABASE_URL; run migrations/trading_schema.sql first.
"""
import os
from typing import List, Optional
from datetime import date, timedelta
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    _engine: Optional[Engine] = None
else:
    _engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def _conn():
    if not _engine:
        raise RuntimeError("DATABASE_URL not set")
    return _engine.connect()


def ensure_tables():
    """Run trading_schema.sql if tables don't exist."""
    path = os.path.join(os.path.dirname(__file__), "..", "migrations", "trading_schema.sql")
    if not os.path.exists(path):
        return
    with open(path) as f:
        sql = f.read()
    with _conn() as c:
        c.execute(text(sql))
        c.commit()


def get_accounts(active_only: bool = True) -> List[dict]:
    with _conn() as conn:
        q = "SELECT id, broker, account_id, display_name, is_active FROM trading_accounts"
        if active_only:
            q += " WHERE is_active = true"
        q += " ORDER BY broker, account_id"
        rows = conn.execute(text(q)).fetchall()
    return [
        {"id": r[0], "broker": r[1], "account_id": r[2], "display_name": r[3] or "", "is_active": r[4]}
        for r in rows
    ]


def get_orders(
    account_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[dict]:
    q = """
        SELECT o.id, o.account_id, a.broker, a.display_name, o.strategy_name, o.symbol, o.exchange,
               o.side, o.quantity, o.order_type, o.status, o.broker_order_id, o.filled_qty, o.filled_price,
               o.trigger_price, o.placed_at, o.filled_at, o.created_at, o.timeframe
        FROM trading_orders o
        JOIN trading_accounts a ON a.id = o.account_id
        WHERE 1=1
    """
    params = {"limit": limit, "offset": offset}
    if account_id is not None:
        q += " AND o.account_id = :account_id"
        params["account_id"] = account_id
    if status:
        q += " AND o.status = :status"
        params["status"] = status
    q += " ORDER BY o.created_at DESC LIMIT :limit OFFSET :offset"
    with _conn() as conn:
        rows = conn.execute(text(q), params).fetchall()
    return [
        {
            "id": r[0], "account_id": r[1], "broker": r[2], "display_name": r[3], "strategy_name": r[4],
            "symbol": r[5], "exchange": r[6], "side": r[7], "quantity": r[8], "order_type": r[9],
            "status": r[10], "broker_order_id": r[11], "filled_qty": r[12], "filled_price": float(r[13]) if r[13] else None,
            "trigger_price": float(r[14]) if r[14] else None, "placed_at": str(r[15]) if r[15] else None,
            "filled_at": str(r[16]) if r[16] else None, "created_at": str(r[17]) if r[17] else None,
            "timeframe": r[18],
        }
        for r in rows
    ]


def get_positions(account_id: Optional[int] = None) -> List[dict]:
    q = """
        SELECT p.id, p.account_id, a.broker, a.display_name, p.symbol, p.exchange, p.side,
               p.quantity, p.avg_price, p.current_price, p.unrealized_pnl, p.last_synced_at
        FROM trading_positions p
        JOIN trading_accounts a ON a.id = p.account_id
        WHERE 1=1
    """
    params = {}
    if account_id is not None:
        q += " AND p.account_id = :account_id"
        params["account_id"] = account_id
    q += " ORDER BY p.account_id, p.symbol"
    with _conn() as conn:
        rows = conn.execute(text(q), params).fetchall()
    return [
        {
            "id": r[0], "account_id": r[1], "broker": r[2], "display_name": r[3], "symbol": r[4],
            "exchange": r[5], "side": r[6], "quantity": r[7], "avg_price": float(r[8]),
            "current_price": float(r[9]) if r[9] else None, "unrealized_pnl": float(r[10]) if r[10] else None,
            "last_synced_at": str(r[11]) if r[11] else None,
        }
        for r in rows
    ]


def get_pnl_snapshots(
    account_id: Optional[int] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    limit: int = 90,
) -> List[dict]:
    if to_date is None:
        to_date = date.today()
    if from_date is None:
        from_date = to_date - timedelta(days=limit)
    q = """
        SELECT s.id, s.account_id, a.broker, a.display_name, s.snapshot_date,
               s.realized_pnl, s.unrealized_pnl, s.total_pnl, s.snapshot_at
        FROM trading_pnl_snapshots s
        JOIN trading_accounts a ON a.id = s.account_id
        WHERE s.snapshot_date >= :from_date AND s.snapshot_date <= :to_date
    """
    params = {"from_date": from_date, "to_date": to_date}
    if account_id is not None:
        q += " AND s.account_id = :account_id"
        params["account_id"] = account_id
    q += " ORDER BY s.snapshot_date DESC"
    with _conn() as conn:
        rows = conn.execute(text(q), params).fetchall()
    return [
        {
            "id": r[0], "account_id": r[1], "broker": r[2], "display_name": r[3], "snapshot_date": str(r[4]),
            "realized_pnl": float(r[5]) if r[5] else 0, "unrealized_pnl": float(r[6]) if r[6] else 0,
            "total_pnl": float(r[7]) if r[7] else 0, "snapshot_at": str(r[8]) if r[8] else None,
        }
        for r in rows
    ]


def insert_order(account_id: int, symbol: str, exchange: str, side: str, quantity: int, order_type: str,
                 status: str = "PENDING", broker_order_id: Optional[str] = None, strategy_id: Optional[int] = None,
                 strategy_name: Optional[str] = None, timeframe: Optional[str] = None,
                 trigger_price: Optional[float] = None) -> int:
    with _conn() as conn:
        r = conn.execute(text("""
            INSERT INTO trading_orders (account_id, symbol, exchange, side, quantity, order_type, status,
                broker_order_id, strategy_id, strategy_name, timeframe, trigger_price, placed_at)
            VALUES (:account_id, :symbol, :exchange, :side, :quantity, :order_type, :status,
                :broker_order_id, :strategy_id, :strategy_name, :timeframe, :trigger_price,
                CASE WHEN :status = 'PLACED' THEN now() ELSE NULL END)
            RETURNING id
        """), {
            "account_id": account_id, "symbol": symbol, "exchange": exchange, "side": side, "quantity": quantity,
            "order_type": order_type, "status": status, "broker_order_id": broker_order_id,
            "strategy_id": strategy_id, "strategy_name": strategy_name, "timeframe": timeframe,
            "trigger_price": trigger_price,
        }).fetchone()
        conn.commit()
        return r[0]


def update_order_status(order_id: int, status: str, filled_qty: Optional[int] = None, filled_price: Optional[float] = None):
    with _conn() as conn:
        conn.execute(text("""
            UPDATE trading_orders SET status = :status, updated_at = now(),
                filled_qty = COALESCE(:filled_qty, filled_qty), filled_price = COALESCE(:filled_price, filled_price),
                filled_at = CASE WHEN :status = 'FILLED' THEN now() ELSE filled_at END,
                cancelled_at = CASE WHEN :status = 'CANCELLED' THEN now() ELSE cancelled_at END
            WHERE id = :order_id
        """), {"order_id": order_id, "status": status, "filled_qty": filled_qty, "filled_price": filled_price})
        conn.commit()
