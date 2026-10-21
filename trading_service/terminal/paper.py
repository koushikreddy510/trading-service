"""Paper positions: what a rule's signal would have done.

A paper position opens at the signal candle's close and is then watched tick by
tick. If the price touches the stop or the target it is closed at that tick's
price (so a gap through the stop fills at the gap price, like a real stop order
would, rather than at an optimistic round number). Nothing here talks to a broker.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from .db import Database, jdump, new_id
from .rules import Signal
from .timeframes import to_epoch


@dataclass
class PaperPosition:
    id: str
    symbol: str
    side: str
    quantity: int
    entry_price: float
    stop: Optional[float]
    target: Optional[float]
    status: str
    opened_at: datetime
    signal_id: Optional[str] = None
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    realized_pnl: Optional[float] = None
    # Exit plan, copied from the rule when the position opened. `initial_stop` is the stop at entry, so the chart can
    # show how far a trailing stop has moved; `trail` / `exit_condition` carry their own timeframes.
    initial_stop: Optional[float] = None
    trail: Optional[dict] = None
    exit_condition: Optional[dict] = None
    exit_timeframe: Optional[str] = None
    # Two-level exits: level 1 closes 50%, level 2 closes the rest.
    exit_condition_2: Optional[dict] = None
    exit_timeframe_2: Optional[str] = None
    exit_level: int = 1            # 1 = waiting for first exit; 2 = first done, waiting for second
    exit_qty_1: Optional[int] = None          # quantity closed at exit level 1
    exit_price_1: Optional[float] = None      # price at which first half exited
    exit_pnl_1: Optional[float] = None        # realised P&L on the first half
    multiplier: float = 1.0        # rupees per 1.00 move per unit of quantity: 1 for shares, the point value of a futures lot

    def unrealized(self, price: float) -> float:
        direction = 1 if self.side == "BUY" else -1
        return (price - self.entry_price) * direction * self.quantity * self.multiplier

    def to_json(self, mark: Optional[float] = None) -> dict:
        out = {
            "id": self.id, "source": "paper", "signal_id": self.signal_id, "symbol": self.symbol, "side": self.side,
            "quantity": self.quantity, "entry_price": self.entry_price, "stop": self.stop, "target": self.target,
            "status": self.status, "opened_at": to_epoch(self.opened_at),
            "closed_at": to_epoch(self.closed_at) if self.closed_at else None,
            "exit_price": self.exit_price, "exit_reason": self.exit_reason, "realized_pnl": self.realized_pnl,
            "initial_stop": self.initial_stop, "trail": self.trail, "exit_condition": self.exit_condition,
            "exit_timeframe": self.exit_timeframe, "exit_condition_2": self.exit_condition_2,
            "exit_timeframe_2": self.exit_timeframe_2, "exit_level": self.exit_level,
            "exit_qty_1": self.exit_qty_1, "exit_price_1": self.exit_price_1, "exit_pnl_1": self.exit_pnl_1,
            "multiplier": self.multiplier,
        }
        if self.status == "open" and mark is not None:
            out["last_price"] = mark
            out["unrealized_pnl"] = self.unrealized(mark)
        return out


def _from_row(r: dict) -> PaperPosition:
    return PaperPosition(
        id=r["id"], signal_id=r["signal_id"], symbol=r["symbol"], side=r["side"], quantity=r["quantity"],
        entry_price=r["entry_price"], stop=r["stop"], target=r["target"], status=r["status"],
        opened_at=r["opened_at"], closed_at=r["closed_at"], exit_price=r["exit_price"],
        exit_reason=r["exit_reason"], realized_pnl=r["realized_pnl"], initial_stop=r["initial_stop"],
        trail=r["trail"], exit_condition=r["exit_condition"], exit_timeframe=r["exit_timeframe"],
        exit_condition_2=r.get("exit_condition_2"), exit_timeframe_2=r.get("exit_timeframe_2"),
        exit_level=r.get("exit_level") or 1, exit_qty_1=r.get("exit_qty_1"),
        exit_price_1=r.get("exit_price_1"), exit_pnl_1=r.get("exit_pnl_1"), multiplier=r.get("multiplier") or 1.0,
    )


class PaperBook:
    """Open paper positions, kept in memory for the tick path and persisted on every change."""

    def __init__(self, db: Database, point_value: Optional[Callable[[str], float]] = None):
        """`point_value(symbol)`: rupees per 1.00 move per unit of quantity (instruments.Instruments.point_value)."""
        self.db = db
        self.point_value = point_value or (lambda symbol: 1.0)
        self._lock = threading.Lock()
        self._open: dict[str, list[PaperPosition]] = {}
        self.reload()

    def reload(self) -> None:
        rows = self.db.rows("SELECT * FROM terminal_paper_positions WHERE status = 'open' ORDER BY opened_at, id")
        with self._lock:
            self._open = {}
            for r in rows:
                p = _from_row(r)
                self._open.setdefault(p.symbol, []).append(p)

    # ---------------------------------------------------------------- opening
    def open_from_signal(
        self, signal: Signal, quantity: int, now: Optional[datetime] = None,
        trail: Optional[dict] = None, exit_condition: Optional[dict] = None, exit_timeframe: Optional[str] = None,
        exit_condition_2: Optional[dict] = None, exit_timeframe_2: Optional[str] = None,
    ) -> PaperPosition:
        position = PaperPosition(
            id=new_id(), signal_id=signal.id, symbol=signal.symbol, side=signal.side, quantity=quantity,
            entry_price=signal.entry, stop=signal.stop, target=signal.target, status="open",
            opened_at=now or datetime.now(timezone.utc), initial_stop=signal.stop, trail=trail,
            exit_condition=exit_condition, exit_timeframe=exit_timeframe if exit_condition else None,
            exit_condition_2=exit_condition_2, exit_timeframe_2=exit_timeframe_2 if exit_condition_2 else None,
            exit_level=1, multiplier=self.point_value(signal.symbol),
        )
        self.db.execute(
            """
            INSERT INTO terminal_paper_positions
                (id, signal_id, symbol, side, quantity, entry_price, stop, target, status, opened_at,
                 initial_stop, trail, exit_condition, exit_timeframe, exit_condition_2, exit_timeframe_2, multiplier)
            VALUES (:id, :sig, :symbol, :side, :qty, :entry, :stop, :target, 'open', :opened,
                    :initial, CAST(:trail AS JSONB), CAST(:exit AS JSONB), :exit_tf,
                    CAST(:exit2 AS JSONB), :exit_tf2, :mult)
            """,
            id=position.id, sig=position.signal_id, symbol=position.symbol, side=position.side, qty=quantity,
            entry=position.entry_price, stop=position.stop, target=position.target, opened=position.opened_at,
            initial=position.initial_stop, trail=jdump(trail) if trail else None,
            exit=jdump(exit_condition) if exit_condition else None, exit_tf=position.exit_timeframe,
            exit2=jdump(exit_condition_2) if exit_condition_2 else None, exit_tf2=position.exit_timeframe_2,
            mult=position.multiplier,
        )
        with self._lock:
            self._open.setdefault(position.symbol, []).append(position)
        return position

    def has_open_for_rule(self, rule_id: str) -> bool:
        return bool(self.db.scalar(
            """
            SELECT EXISTS (
                SELECT 1 FROM terminal_paper_positions p
                JOIN terminal_signals s ON s.id = p.signal_id
                WHERE p.status = 'open' AND s.rule_id = :r)
            """,
            r=rule_id,
        ))

    # ----------------------------------------------------------- tick handling
    @staticmethod
    def _trigger(p: PaperPosition, price: float) -> Optional[str]:
        if p.side == "BUY":
            if p.stop is not None and price <= p.stop:
                return "stop"
            if p.target is not None and price >= p.target:
                return "target"
        else:
            if p.stop is not None and price >= p.stop:
                return "stop"
            if p.target is not None and price <= p.target:
                return "target"
        return None

    def would_trigger(self, symbol: str, price: float) -> bool:
        """Cheap in-memory check so the tick path only touches the DB when something closes."""
        with self._lock:
            return any(self._trigger(p, price) for p in self._open.get(symbol, ()))

    def on_tick(self, symbol: str, price: float, ts: Optional[datetime] = None) -> list[PaperPosition]:
        """Close every position of `symbol` whose stop or target the price has reached."""
        with self._lock:
            hits = [(p, r) for p in self._open.get(symbol, ()) if (r := self._trigger(p, price))]
        return [self._close(p, price, reason, ts) for p, reason in hits]

    def close_manually(self, position_id: str, price: float, ts: Optional[datetime] = None) -> Optional[PaperPosition]:
        with self._lock:
            found = next((p for ps in self._open.values() for p in ps if p.id == position_id), None)
        return self._close(found, price, "manual", ts) if found else None

    def partial_close(self, p: PaperPosition, price: float, reason: str, ts: Optional[datetime] = None) -> PaperPosition:
        """Exit the first half of a 2-level position; advance it to exit level 2.

        Sells floor(quantity/2) shares, records the partial P&L, then updates the
        position in-place so the engine checks exit_condition_2 going forward.
        Returns the mutated (still-open) position for broadcasting.
        """
        closed_at = ts or datetime.now(timezone.utc)
        qty_1 = p.quantity // 2
        remaining = p.quantity - qty_1
        direction = 1 if p.side == "BUY" else -1
        pnl_1 = (price - p.entry_price) * direction * qty_1 * p.multiplier
        self.db.execute(
            """
            UPDATE terminal_paper_positions SET
                quantity     = :remaining,
                exit_level   = 2,
                exit_qty_1   = :qty1,
                exit_price_1 = :px1,
                exit_pnl_1   = :pnl1
            WHERE id = :id AND status = 'open'
            """,
            remaining=remaining, qty1=qty_1, px1=price, pnl1=pnl_1, id=p.id,
        )
        with self._lock:
            p.quantity = remaining
            p.exit_level = 2
            p.exit_qty_1 = qty_1
            p.exit_price_1 = price
            p.exit_pnl_1 = pnl_1
        return p

    def move_stop(self, p: PaperPosition, stop: float) -> PaperPosition:
        """Trail the stop. Only ever tightens: a stop that would loosen (or not move) is ignored."""
        if p.stop is not None and not (stop > p.stop if p.side == "BUY" else stop < p.stop):
            return p
        self.db.execute("UPDATE terminal_paper_positions SET stop = :s WHERE id = :id AND status = 'open'", s=stop, id=p.id)
        with self._lock:
            p.stop = stop
        return p

    def close(self, p: PaperPosition, price: float, reason: str, ts: Optional[datetime] = None) -> PaperPosition:
        """Close at `price` for a reason other than a tick touching the stop/target (e.g. an exit rule)."""
        return self._close(p, price, reason, ts)

    def _close(self, p: PaperPosition, price: float, reason: str, ts: Optional[datetime]) -> PaperPosition:
        closed_at = ts or datetime.now(timezone.utc)
        pnl = p.unrealized(price)
        self.db.execute(
            """
            UPDATE terminal_paper_positions
               SET status = 'closed', closed_at = :at, exit_price = :px, exit_reason = :why, realized_pnl = :pnl
             WHERE id = :id AND status = 'open'
            """,
            at=closed_at, px=price, why=reason, pnl=pnl, id=p.id,
        )
        with self._lock:
            siblings = self._open.get(p.symbol, [])
            self._open[p.symbol] = [x for x in siblings if x.id != p.id]
            if not self._open[p.symbol]:
                del self._open[p.symbol]
        p.status, p.closed_at, p.exit_price, p.exit_reason, p.realized_pnl = "closed", closed_at, price, reason, pnl
        return p

    # ----------------------------------------------------------------- queries
    def open_positions(self, symbol: Optional[str] = None) -> list[PaperPosition]:
        with self._lock:
            if symbol:
                return list(self._open.get(symbol, ()))
            return [p for ps in self._open.values() for p in ps]

    def history(self, symbol: Optional[str] = None, limit: int = 100) -> list[PaperPosition]:
        sql = "SELECT * FROM terminal_paper_positions WHERE status = 'closed'"
        params: dict = {"n": max(1, min(limit, 1000))}
        if symbol:
            sql += " AND symbol = :s"
            params["s"] = symbol
        sql += " ORDER BY closed_at DESC LIMIT :n"
        return [_from_row(r) for r in self.db.rows(sql, **params)]
