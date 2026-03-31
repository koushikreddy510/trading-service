"""
Trading service DB: accounts, orders, positions, PnL snapshots.
Set DATABASE_URL; run migrations/trading_schema.sql first.
"""
import copy
import json
import os
from typing import Any, Dict, List, Optional, Tuple
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


def _run_migration_file(path: str):
    if not os.path.exists(path):
        return
    with open(path) as f:
        sql = f.read()
    with _conn() as c:
        c.execute(text(sql))
        c.commit()


def ensure_tables():
    """Run base schema then execution-strategy migrations (idempotent)."""
    root = os.path.join(os.path.dirname(__file__), "..", "migrations")
    _run_migration_file(os.path.join(root, "trading_schema.sql"))
    _run_migration_file(os.path.join(root, "trading_execution_strategies.sql"))
    _run_migration_file(os.path.join(root, "trading_universe_sources_extend.sql"))


# Keys in config_json that affect entry universe resolution (see universe_resolver).
_UNIVERSE_CONFIG_KEYS = frozenset({
    "universe_cap_filter",
    "universe_sector_filter",
    "union_screen",
    "universe_screen",
    "near_pct",
    "pct_from_high_min",
    "pct_from_high_max",
    "at_52w_high",
})


def _universe_fingerprint(
    universe_source: str,
    screener_strategy_id: Optional[int],
    screener_strategy_name: Optional[str],
    config: Any,
) -> str:
    cfg = config if isinstance(config, dict) else {}
    sub = {k: cfg[k] for k in sorted(_UNIVERSE_CONFIG_KEYS) if k in cfg}
    payload = {
        "screener_strategy_id": screener_strategy_id,
        "screener_strategy_name": screener_strategy_name or "",
        "u": sub,
        "universe_source": universe_source or "screener_filtered",
    }
    return json.dumps(payload, sort_keys=True, default=str)


def count_open_positions_for_execution_strategy(execution_strategy_id: int) -> int:
    with _conn() as conn:
        n = conn.execute(
            text("""
                SELECT COUNT(*) FROM trading_positions
                WHERE execution_strategy_id = :sid AND COALESCE(quantity, 0) <> 0
            """),
            {"sid": execution_strategy_id},
        ).scalar()
    return int(n or 0)


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
               p.quantity, p.avg_price, p.current_price, p.unrealized_pnl, p.last_synced_at,
               p.execution_strategy_id, es.slug, es.display_name
        FROM trading_positions p
        JOIN trading_accounts a ON a.id = p.account_id
        LEFT JOIN trading_execution_strategies es ON es.id = p.execution_strategy_id
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
            "execution_strategy_id": r[12],
            "execution_strategy_slug": r[13] or "",
            "execution_strategy_name": r[14] or "",
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


def get_execution_strategies(active_only: bool = True) -> List[dict]:
    q = """
        SELECT es.id, es.slug, es.display_name, es.profile, es.entry_style, es.universe_source,
               es.screener_strategy_id, es.screener_strategy_name, es.config_json, es.is_active,
               COALESCE((
                   SELECT COUNT(*) FROM trading_positions p
                   WHERE p.execution_strategy_id = es.id AND COALESCE(p.quantity, 0) <> 0
               ), 0)
        FROM trading_execution_strategies es
    """
    if active_only:
        q += " WHERE es.is_active = true"
    q += " ORDER BY es.slug"
    with _conn() as conn:
        rows = conn.execute(text(q)).fetchall()
    out = []
    for r in rows:
        cfg = r[8]
        if cfg is not None and hasattr(cfg, "keys"):
            cfg = dict(cfg)
        elif cfg is None:
            cfg = {}
        out.append({
            "id": r[0], "slug": r[1], "display_name": r[2], "profile": r[3], "entry_style": r[4],
            "universe_source": r[5], "screener_strategy_id": r[6], "screener_strategy_name": r[7] or "",
            "config": cfg, "is_active": r[9],
            "open_positions_count": int(r[10] or 0),
        })
    return out


def get_execution_strategy_by_slug(slug: str) -> Optional[dict]:
    with _conn() as conn:
        r = conn.execute(text("""
            SELECT es.id, es.slug, es.display_name, es.profile, es.entry_style, es.universe_source,
                   es.screener_strategy_id, es.screener_strategy_name, es.config_json, es.is_active,
                   COALESCE((
                       SELECT COUNT(*) FROM trading_positions p
                       WHERE p.execution_strategy_id = es.id AND COALESCE(p.quantity, 0) <> 0
                   ), 0)
            FROM trading_execution_strategies es WHERE es.slug = :slug
        """), {"slug": slug}).fetchone()
    if not r:
        return None
    cfg = r[8]
    if cfg is not None and hasattr(cfg, "keys"):
        cfg = dict(cfg)
    else:
        cfg = {}
    return {
        "id": r[0], "slug": r[1], "display_name": r[2], "profile": r[3], "entry_style": r[4],
        "universe_source": r[5], "screener_strategy_id": r[6], "screener_strategy_name": r[7] or "",
        "config": cfg, "is_active": r[9],
        "open_positions_count": int(r[10] or 0),
    }


def get_execution_strategy_by_id(strategy_id: int) -> Optional[dict]:
    with _conn() as conn:
        r = conn.execute(text("""
            SELECT es.id, es.slug, es.display_name, es.profile, es.entry_style, es.universe_source,
                   es.screener_strategy_id, es.screener_strategy_name, es.config_json, es.is_active,
                   COALESCE((
                       SELECT COUNT(*) FROM trading_positions p
                       WHERE p.execution_strategy_id = es.id AND COALESCE(p.quantity, 0) <> 0
                   ), 0)
            FROM trading_execution_strategies es WHERE es.id = :id
        """), {"id": strategy_id}).fetchone()
    if not r:
        return None
    cfg = r[8]
    if cfg is not None and hasattr(cfg, "keys"):
        cfg = dict(cfg)
    else:
        cfg = {}
    return {
        "id": r[0], "slug": r[1], "display_name": r[2], "profile": r[3], "entry_style": r[4],
        "universe_source": r[5], "screener_strategy_id": r[6], "screener_strategy_name": r[7] or "",
        "config": cfg, "is_active": r[9],
        "open_positions_count": int(r[10] or 0),
    }


def get_account_execution_strategies(account_id: int) -> List[dict]:
    """All execution strategies with this account's enable flag and overrides."""
    with _conn() as conn:
        rows = conn.execute(text("""
            SELECT es.id, es.slug, es.display_name, es.profile, es.entry_style, es.universe_source,
                   es.screener_strategy_id, es.screener_strategy_name, es.config_json, es.is_active,
                   COALESCE(m.is_enabled, false) AS account_enabled,
                   m.overrides_json,
                   COALESCE((
                       SELECT COUNT(*) FROM trading_positions p
                       WHERE p.execution_strategy_id = es.id AND COALESCE(p.quantity, 0) <> 0
                   ), 0)
            FROM trading_execution_strategies es
            LEFT JOIN trading_strategy_account_map m
                ON m.execution_strategy_id = es.id AND m.account_id = :aid
            WHERE es.is_active = true
            ORDER BY es.slug
        """), {"aid": account_id}).fetchall()
    out = []
    for r in rows:
        cfg = r[8]
        if cfg is not None and hasattr(cfg, "keys"):
            cfg = dict(cfg)
        else:
            cfg = {}
        ovr = r[11]
        if ovr is not None and hasattr(ovr, "keys"):
            ovr = dict(ovr)
        else:
            ovr = {}
        out.append({
            "id": r[0], "slug": r[1], "display_name": r[2], "profile": r[3], "entry_style": r[4],
            "universe_source": r[5], "screener_strategy_id": r[6], "screener_strategy_name": r[7] or "",
            "config": cfg, "strategy_active": r[9], "enabled_for_account": r[10], "overrides": ovr,
            "open_positions_count": int(r[12] or 0),
        })
    return out


def patch_execution_strategy(strategy_id: int, patch: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[dict]]:
    """
    Partial update. With open positions for this strategy, universe_source, screener ids, and
    universe-related config (see _UNIVERSE_CONFIG_KEYS) cannot change; is_active may still change.
    """
    allowed = {"universe_source", "screener_strategy_id", "screener_strategy_name", "config", "is_active"}
    extra = set(patch.keys()) - allowed
    if extra:
        return False, f"unknown_fields:{','.join(sorted(extra))}", None

    with _conn() as conn:
        r = conn.execute(
            text("""
                SELECT universe_source, screener_strategy_id, screener_strategy_name, config_json, is_active
                FROM trading_execution_strategies WHERE id = :id
            """),
            {"id": strategy_id},
        ).fetchone()
    if not r:
        return False, "unknown_strategy_id", None

    cur_source, cur_sid, cur_sname, cur_cfg_raw, cur_active = r[0], r[1], r[2], r[3], r[4]
    cur_cfg = dict(cur_cfg_raw) if cur_cfg_raw and hasattr(cur_cfg_raw, "keys") else {}

    new_source = patch["universe_source"] if "universe_source" in patch else cur_source
    new_sid = patch["screener_strategy_id"] if "screener_strategy_id" in patch else cur_sid
    new_sname = patch["screener_strategy_name"] if "screener_strategy_name" in patch else cur_sname
    new_cfg = copy.deepcopy(cur_cfg)
    if "config" in patch:
        pcfg = patch["config"]
        new_cfg = dict(pcfg) if isinstance(pcfg, dict) else {}
    new_active = patch["is_active"] if "is_active" in patch else cur_active

    fp_old = _universe_fingerprint(cur_source, cur_sid, cur_sname, cur_cfg)
    fp_new = _universe_fingerprint(new_source, new_sid, new_sname, new_cfg)

    n_open = count_open_positions_for_execution_strategy(strategy_id)
    if n_open > 0 and fp_old != fp_new:
        return False, "universe_locked_open_positions", None

    with _conn() as conn:
        conn.execute(
            text("""
                UPDATE trading_execution_strategies SET
                    universe_source = :universe_source,
                    screener_strategy_id = :screener_strategy_id,
                    screener_strategy_name = :screener_strategy_name,
                    config_json = CAST(:config_json AS jsonb),
                    is_active = :is_active,
                    updated_at = now()
                WHERE id = :id
            """),
            {
                "id": strategy_id,
                "universe_source": new_source,
                "screener_strategy_id": new_sid,
                "screener_strategy_name": new_sname or None,
                "config_json": json.dumps(new_cfg),
                "is_active": new_active,
            },
        )
        conn.commit()

    updated = get_execution_strategy_by_id(strategy_id)
    return True, None, updated


def set_account_strategy_enabled(account_id: int, execution_strategy_id: int, is_enabled: bool) -> bool:
    """Upsert map row. Returns True if account and strategy exist."""
    with _conn() as conn:
        ac = conn.execute(text("SELECT 1 FROM trading_accounts WHERE id = :id"), {"id": account_id}).scalar()
        st = conn.execute(text("SELECT 1 FROM trading_execution_strategies WHERE id = :id"), {"id": execution_strategy_id}).scalar()
        if not ac or not st:
            return False
        conn.execute(text("""
            INSERT INTO trading_strategy_account_map (execution_strategy_id, account_id, is_enabled, updated_at)
            VALUES (:sid, :aid, :en, now())
            ON CONFLICT (execution_strategy_id, account_id) DO UPDATE SET
                is_enabled = EXCLUDED.is_enabled, updated_at = now()
        """), {"sid": execution_strategy_id, "aid": account_id, "en": is_enabled})
        conn.commit()
    return True


def replace_positions(account_id: int, positions: list) -> int:
    """
    Replace all positions for an account with the given list.
    Broker returns only open positions; closed ones are implicitly removed.
    Returns count written.
    """
    with _conn() as conn:
        conn.execute(text("DELETE FROM trading_positions WHERE account_id = :aid"), {"aid": account_id})
        for p in positions:
            conn.execute(text("""
                INSERT INTO trading_positions (account_id, symbol, exchange, side, quantity, avg_price, current_price,
                    unrealized_pnl, execution_strategy_id, last_synced_at, updated_at)
                VALUES (:account_id, :symbol, :exchange, :side, :quantity, :avg_price, :current_price,
                    :unrealized_pnl, :execution_strategy_id, now(), now())
            """), {
                "account_id": account_id, "symbol": p.get("symbol", ""), "exchange": p.get("exchange", "NSE"),
                "side": p.get("side", "BUY"), "quantity": p.get("quantity", 0), "avg_price": float(p.get("avg_price", 0)),
                "current_price": float(p["current_price"]) if p.get("current_price") is not None else None,
                "unrealized_pnl": float(p["unrealized_pnl"]) if p.get("unrealized_pnl") is not None else None,
                "execution_strategy_id": p.get("execution_strategy_id"),
            })
        conn.commit()
    return len(positions)


def get_admin_status() -> dict:
    """Stats for admin data-status."""
    with _conn() as conn:
        accounts = conn.execute(text(
            "SELECT id, broker, account_id, display_name, credentials_ref FROM trading_accounts WHERE is_active = true"
        )).fetchall()
        pos_count = conn.execute(text("SELECT COUNT(*) FROM trading_positions")).scalar()
        pos_latest = conn.execute(text("SELECT MAX(last_synced_at) FROM trading_positions")).scalar()
        pnl_count = conn.execute(text("SELECT COUNT(*) FROM trading_pnl_snapshots")).scalar()
        pnl_latest = conn.execute(text("SELECT MAX(snapshot_date) FROM trading_pnl_snapshots")).scalar()
        order_count = conn.execute(text("SELECT COUNT(*) FROM trading_orders")).scalar()
    return {
        "accounts": [
            {"id": r[0], "broker": r[1], "account_id": r[2], "display_name": r[3] or "", "credentials_ref": r[4] or ""}
            for r in accounts
        ],
        "positions": {"total": pos_count or 0, "last_synced_at": str(pos_latest) if pos_latest else None},
        "pnl": {"snapshots_count": pnl_count or 0, "latest_date": str(pnl_latest) if pnl_latest else None},
        "orders": {"total": order_count or 0},
    }


def upsert_pnl_snapshot(account_id: int, snapshot_date: date, realized_pnl: float, unrealized_pnl: float, total_pnl: float):
    """Upsert daily PnL snapshot for an account."""
    with _conn() as conn:
        conn.execute(text("""
            INSERT INTO trading_pnl_snapshots (account_id, snapshot_date, realized_pnl, unrealized_pnl, total_pnl, snapshot_at)
            VALUES (:account_id, :snapshot_date, :realized_pnl, :unrealized_pnl, :total_pnl, now())
            ON CONFLICT (account_id, snapshot_date) DO UPDATE SET
                realized_pnl = EXCLUDED.realized_pnl, unrealized_pnl = EXCLUDED.unrealized_pnl,
                total_pnl = EXCLUDED.total_pnl, snapshot_at = now()
        """), {
            "account_id": account_id, "snapshot_date": snapshot_date,
            "realized_pnl": realized_pnl, "unrealized_pnl": unrealized_pnl, "total_pnl": total_pnl,
        })
        conn.commit()
