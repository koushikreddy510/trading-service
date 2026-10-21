"""Rules on real money: mirror a rule's paper position with live orders, so the paper engine stays the one brain.

A rule with `execution = {account, product}` keeps trading on paper exactly as before; every change to its paper
position is repeated on the account:

    paper opens            -> live entry (marketable limit), quantity = the paper quantity
    entry fills            -> protective stop-loss at the paper stop, for exactly the filled quantity
    paper stop tightens    -> the same broker stop order is modified (a trailing step)
    paper exit level 1     -> the stop shrinks to what stays, then the first part is sold
    paper closes (any why) -> the stop is cancelled, then whatever this position still holds is closed

Every order goes through orders.OrderService, so all its checks apply (live switch, kill switch, products, limits,
never netting into a position that is not ours). Quantities come from our ledger, never from the broker's net.
Actions run one at a time, in order, on one worker thread: never on the tick path.
Intraday (product I) positions are closed before the exchange's cut-off (Terminal._cutoff_loop).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Optional

from .db import Database
from .orders import OPEN, OrderRefused, OrderService
from .rules import RuleRepo
from .timeframes import IST, NEW_YORK, exchange_of

log = logging.getLogger(__name__)

FRESH = timedelta(minutes=5)                 # only a position that has just opened gets a live entry (not one reloaded later)


def intraday_cutoff(exchange: str, day: datetime) -> time:
    """When our intraday positions are closed (a few minutes before the broker's own auto square-off)."""
    if exchange == "MCX":
        winter = not datetime.combine(day.date(), time(12), tzinfo=NEW_YORK).dst()
        return time(23, 40) if winter else time(23, 15)
    return time(15, 12)


def past_cutoff(exchange: str, now: datetime, early: timedelta = timedelta(0)) -> bool:
    local = now.astimezone(IST)
    cut = datetime.combine(local.date(), intraday_cutoff(exchange, local), tzinfo=IST) - early
    return local >= cut


class LinkRepo:
    def __init__(self, db: Database):
        self.db = db

    def get(self, position_id: str) -> Optional[dict]:
        return self.db.row("SELECT * FROM terminal_live_links WHERE position_id = :p", p=position_id)

    def insert(self, **row: Any) -> None:
        self.db.execute(
            """
            INSERT INTO terminal_live_links (position_id, rule_id, account, product, symbol, side, last_stop, state, note)
            VALUES (:position_id, :rule_id, :account, :product, :symbol, :side, :last_stop, :state, :note)
            """,
            **row,
        )

    def update(self, position_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        self.db.execute(f"UPDATE terminal_live_links SET {sets}, updated_at = now() WHERE position_id = :p", p=position_id, **fields)

    def active(self) -> list[dict]:
        return self.db.rows("SELECT * FROM terminal_live_links WHERE state = 'live'")


class LiveMirror:
    def __init__(self, db: Database, orders: OrderService, notify: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), background: bool = True):
        self.db, self.orders = db, orders
        self.links, self.rules = LinkRepo(db), RuleRepo(db)
        self.notify = notify or (lambda text: None)
        self.clock = clock
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-mirror") if background else None
        orders.on_change.append(self.on_order)

    def _run(self, fn: Callable, *args: Any) -> None:
        def safe() -> None:
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 - one failed action must not stop the next
                log.exception("live mirror action failed")
        if self._pool is None:
            safe()
        else:
            self._pool.submit(safe)

    # ----------------------------------------------------------------- inputs
    def on_event(self, event: dict) -> None:
        """Hub listener: every paper position change."""
        if event.get("type") == "position":
            self._run(self._position, dict(event["position"]))

    def on_order(self, order: dict) -> None:
        """OrderService listener: an order of ours changed status."""
        if order.get("link") and order.get("purpose") == "entry" and order["status"] == "complete":
            self._run(self._entry_filled, order["link"])
        elif order.get("link") and order.get("purpose") == "stop" and order["status"] == "complete":
            self._run(self._note, order["link"], f"stop filled @ {order.get('avg_price')}")

    # ---------------------------------------------------------------- actions
    def _position(self, p: dict) -> None:
        link = self.links.get(p["id"])
        if link is None:
            if p["status"] == "open" and p.get("exit_level", 1) == 1:
                self._maybe_enter(p)
            return
        if link["state"] != "live":
            return
        if p["status"] == "closed":
            self._close(link, p.get("exit_reason") or "closed")
        elif p.get("exit_level") == 2 and p.get("exit_qty_1") and not link["partial_done"]:
            self._partial(link, int(p["exit_qty_1"]), p.get("stop"))
        elif p.get("stop") is not None and self._tighter(link, p["stop"]):
            self._trail(link, float(p["stop"]))

    def _maybe_enter(self, p: dict) -> None:
        rule = self._rule_of(p)
        if rule is None or not rule.execution:
            return
        opened = datetime.fromtimestamp(p["opened_at"], tz=timezone.utc) if isinstance(p.get("opened_at"), (int, float)) else None
        if opened is not None and self.clock() - opened > FRESH:
            return                                    # an old position seen again (restart, trail): never enter late
        account, product = rule.execution["account"], rule.execution["product"]
        row = {"position_id": p["id"], "rule_id": rule.id, "account": account, "product": product, "symbol": p["symbol"],
               "side": p["side"], "last_stop": p.get("stop"), "state": "live", "note": None}
        if product == "I" and past_cutoff(exchange_of(p["symbol"]), self.clock(), early=timedelta(minutes=5)):
            self.links.insert(**{**row, "state": "skipped", "note": "too close to the intraday cut-off"})
            self.notify(f"⏭ Live entry skipped for {rule.name or rule.id} ({p['symbol']}): too close to the intraday cut-off")
            return
        try:
            self.orders.place(account, p["symbol"], p["side"], int(p["quantity"]), product, "MKT", source=f"rule:{rule.id}",
                              link=p["id"], purpose="entry")
        except OrderRefused as e:
            self.links.insert(**{**row, "state": "refused", "note": str(e)[:300]})
            self.notify(f"⛔ Live entry refused for {rule.name or rule.id} ({p['side']} {p['quantity']} {p['symbol']}): {e}")
            return
        except Exception as e:  # noqa: BLE001 - recorded by the order service already
            self.links.insert(**{**row, "state": "failed", "note": str(e)[:300]})
            return
        self.links.insert(**row)

    def _entry_filled(self, link_id: str) -> None:
        link = self.links.get(link_id)
        if link is None or link["state"] != "live" or link["stop_order_id"] or link["last_stop"] is None:
            return
        qty = self._held_qty(link)
        if qty <= 0:
            return
        exit_side = "SELL" if link["side"] == "BUY" else "BUY"
        try:
            stop = self.orders.place(link["account"], link["symbol"], exit_side, qty, link["product"], "SL",
                                     trigger_price=float(link["last_stop"]), source=f"rule:{link['rule_id']}", link=link_id, purpose="stop")
        except OrderRefused as e:
            if "trigger" in str(e) and ("below" in str(e) or "above" in str(e)):   # price already through the stop: get out
                self._exit(link, qty, "stop already passed at entry")
                return
            self._note(link_id, f"stop not placed: {e}")
            self.notify(f"⚠️ Stop not placed for {link['symbol']}: {e}")
            return
        self.links.update(link_id, stop_order_id=stop["id"])

    def _trail(self, link: dict, new_stop: float) -> None:
        self.links.update(link["position_id"], last_stop=new_stop)
        stop = self._open_stop(link)
        if stop is None:
            return                                     # entry not filled yet: the stop goes in at the latest level
        try:
            self.orders.modify_stop(stop["id"], new_stop)
        except OrderRefused as e:
            self._note(link["position_id"], f"trail to {new_stop} not sent: {e}")

    def _partial(self, link: dict, exit_qty: int, stop_now: Optional[float]) -> None:
        held = self._held_qty(link)
        exit_qty = min(exit_qty, held)
        if exit_qty <= 0:
            self.links.update(link["position_id"], partial_done=True)
            return
        stop = self._open_stop(link)
        remaining = held - exit_qty
        if stop is not None:                           # shrink the stop first, or a later trigger could sell more than we hold
            if remaining > 0:
                self.orders.modify_stop(stop["id"], float(stop_now or stop["trigger_price"]), quantity=remaining)
            else:
                self.orders.cancel(stop["id"])
        self._exit(link, exit_qty, "exit level 1")
        self.links.update(link["position_id"], partial_done=True)

    def _close(self, link: dict, why: str) -> None:
        stop = self._open_stop(link)
        if stop is not None:
            try:
                self.orders.cancel(stop["id"])
            except Exception as e:  # noqa: BLE001 - it may have just triggered; the ledger decides below
                log.info("cancel stop %s: %s", stop["id"], e)
        self.orders.sync()
        held = self._held_qty(link)
        if held > 0:
            self._exit(link, held, why)
        self.links.update(link["position_id"], state="closed", note=why)

    def _exit(self, link: dict, qty: int, why: str) -> None:
        exit_side = "SELL" if link["side"] == "BUY" else "BUY"
        try:
            self.orders.place(link["account"], link["symbol"], exit_side, qty, link["product"], "MKT", source=f"rule:{link['rule_id']}",
                              link=link["position_id"], purpose="exit")
        except OrderRefused as e:
            self._note(link["position_id"], f"exit ({why}) refused: {e}")
            self.notify(f"🚨 *Live exit refused* {link['symbol']} ({why}): {e}. Close it by hand.")

    # ---------------------------------------------------------------- helpers
    def _rule_of(self, p: dict):
        if not p.get("signal_id"):
            return None
        rid = self.db.scalar("SELECT rule_id FROM terminal_signals WHERE id = :s", s=p["signal_id"])
        return self.rules.get(rid) if rid else None

    def _held_qty(self, link: dict) -> int:
        """What this position holds through its own fills, in order quantity (lots for derivatives)."""
        rows = self.orders.repo.for_link(link["position_id"])
        entry = next((r for r in rows if r["purpose"] == "entry"), None)
        if entry is None or not entry["quantity"]:
            return 0
        per = entry["units"] // entry["quantity"] or 1
        net = self.orders.repo.link_net(link["position_id"]) * (1 if link["side"] == "BUY" else -1)
        return max(0, net // per)

    def _open_stop(self, link: dict) -> Optional[dict]:
        return next((r for r in reversed(self.orders.repo.for_link(link["position_id"]))
                     if r["purpose"] == "stop" and r["status"] in OPEN), None)

    @staticmethod
    def _tighter(link: dict, stop: float) -> bool:
        last = link["last_stop"]
        if last is None:
            return True
        return stop > last if link["side"] == "BUY" else stop < last

    def _note(self, link_id: str, text: str) -> None:
        self.links.update(link_id, note=text[:300])

    def due_for_cutoff(self) -> list[dict]:
        """Live intraday links whose exchange has reached our cut-off."""
        now = self.clock()
        return [l for l in self.links.active() if l["product"] == "I" and past_cutoff(exchange_of(l["symbol"]), now)]
