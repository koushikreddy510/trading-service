"""Rules on real money: mirror a rule's paper position with live orders, so the paper engine stays the one brain.

A rule with `execution = {account, product}` keeps trading on paper exactly as before; every change to its paper
position is repeated on the account:

    paper opens            -> a pending approval (when the account requires one; the default), then on approval
                              the live entry (marketable limit), quantity = the paper quantity
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
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), background: bool = True,
                 broadcast: Optional[Callable[[dict], None]] = None):
        self.db, self.orders = db, orders
        self.broadcast = broadcast or (lambda event: None)
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
        if link["state"] == "awaiting_approval" and p["status"] == "closed":
            a = self.db.row("SELECT * FROM terminal_approvals WHERE position_id = :p AND status = 'pending'", p=p["id"])
            if a:
                self._close_approval(a, "expired", "the paper position closed before approval")
            return
        if link["state"] not in ("live", "entering"):
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
        limits = self.orders.settings.get(account)
        if limits["require_approval"]:
            self._ask_approval(p, rule, row, int(limits["approval_minutes"]))
            return
        self.links.insert(**{**row, "state": "entering"})
        self._send_entry(p["id"], rule.id, rule.name, account, p["symbol"], p["side"], int(p["quantity"]), product)

    def _send_entry(self, position_id: str, rule_id: str, rule_name: str, account: str, symbol: str, side: str, qty: int,
                    product: str) -> bool:
        try:
            self.orders.place(account, symbol, side, qty, product, "MKT", source=f"rule:{rule_id}", link=position_id, purpose="entry")
        except OrderRefused as e:
            self.links.update(position_id, state="refused", note=str(e)[:300])
            self.notify(f"⛔ Live entry refused for {rule_name or rule_id} ({side} {qty} {symbol}): {e}")
            return False
        except Exception as e:  # noqa: BLE001 - recorded by the order service already
            self.links.update(position_id, state="failed", note=str(e)[:300])
            return False
        self.links.update(position_id, state="live", note=None)
        return True

    # -------------------------------------------------------------- approvals
    def _ask_approval(self, p: dict, rule: Any, row: dict, minutes: int) -> None:
        from .db import new_id

        aid = new_id()
        expires = self.clock() + timedelta(minutes=minutes)
        self.links.insert(**{**row, "state": "awaiting_approval", "note": f"approval {aid}"})
        self.db.execute(
            """
            INSERT INTO terminal_approvals (id, position_id, rule_id, account, product, symbol, side, quantity, signal_price, stop, target,
                                            reason, expires_at)
            VALUES (:id, :pid, :rid, :acc, :prd, :sym, :side, :qty, :px, :stop, :target, :reason, :exp)
            """,
            id=aid, pid=p["id"], rid=rule.id, acc=row["account"], prd=row["product"], sym=p["symbol"], side=p["side"], qty=int(p["quantity"]),
            px=p.get("entry_price"), stop=p.get("stop"), target=p.get("target"), reason=self._signal_reason(p), exp=expires,
        )
        product = {"I": "Intraday", "F": "MTF", "M": "NRML"}.get(row["product"], row["product"])
        self.notify(f"⏳ *Approval needed* · {p['side']} {p['quantity']} {p['symbol']} · {product} · rule “{rule.name or rule.id}”\n"
                    f"signal @ ₹{p.get('entry_price')} · stop ₹{p.get('stop')}" + (f" · target ₹{p['target']}" if p.get("target") else "")
                    + f"\nApprove in the Workspace (Orders → Approvals) within {minutes} min, or it expires.")
        self.broadcast({"type": "approval", "approval": self.approval(aid)})

    def _signal_reason(self, p: dict) -> Optional[str]:
        return self.db.scalar("SELECT reason FROM terminal_signals WHERE id = :s", s=p.get("signal_id")) if p.get("signal_id") else None

    def approval(self, approval_id: str) -> Optional[dict]:
        row = self.db.row("SELECT * FROM terminal_approvals WHERE id = :i", i=approval_id)
        if row is None:
            return None
        out = dict(row)
        for k in ("created_at", "expires_at", "decided_at"):
            if out.get(k) is not None:
                out[k] = out[k].isoformat()
        out["rule_name"] = self.db.scalar("SELECT name FROM terminal_rules WHERE id = :i", i=row["rule_id"]) if row["rule_id"] else None
        return out

    def approvals(self, pending_only: bool = False) -> list[dict]:
        sql = "SELECT id FROM terminal_approvals" + (" WHERE status = 'pending'" if pending_only else " WHERE created_at >= now() - interval '2 days'")
        return [self.approval(r["id"]) for r in self.db.rows(sql + " ORDER BY created_at DESC LIMIT 50")]

    def decide(self, approval_id: str, approve: bool) -> dict:
        """A person's decision. Approving sends the entry now (all order checks still apply); stops and exits follow by themselves."""
        a = self.db.row("SELECT * FROM terminal_approvals WHERE id = :i", i=approval_id)
        if a is None:
            raise OrderRefused("no such approval")
        if a["status"] != "pending":
            raise OrderRefused(f"already {a['status']}")
        if self.clock() > a["expires_at"]:
            self._close_approval(a, "expired", "the approval window had passed")
            raise OrderRefused("this approval has expired")
        open_now = self.db.scalar("SELECT status FROM terminal_paper_positions WHERE id = :p", p=a["position_id"]) == "open"
        if approve and not open_now:
            self._close_approval(a, "expired", "the paper position closed before approval")
            raise OrderRefused("the rule's position has already closed; nothing to enter")
        if not approve:
            self._close_approval(a, "rejected", "rejected by a person")
            return self.approval(approval_id)  # type: ignore[return-value]
        self.db.execute("UPDATE terminal_approvals SET status = 'approved', decided_at = now() WHERE id = :i", i=approval_id)
        self.links.update(a["position_id"], state="entering", note=f"approved {approval_id}")
        ok = self._send_entry(a["position_id"], a["rule_id"], "", a["account"], a["symbol"], a["side"], a["quantity"], a["product"])
        if not ok:
            link = self.links.get(a["position_id"]) or {}
            self.db.execute("UPDATE terminal_approvals SET status = 'failed', note = :n WHERE id = :i", n=link.get("note"), i=approval_id)
        self.broadcast({"type": "approval", "approval": self.approval(approval_id)})
        return self.approval(approval_id)  # type: ignore[return-value]

    def _close_approval(self, a: dict, status: str, note: str) -> None:
        self.db.execute("UPDATE terminal_approvals SET status = :s, note = :n, decided_at = now() WHERE id = :i AND status = 'pending'",
                        s=status, n=note, i=a["id"])
        self.links.update(a["position_id"], state=status, note=note)
        if status == "expired":
            self.notify(f"⌛ Approval expired · {a['side']} {a['quantity']} {a['symbol']}: {note}; no live order was sent")
        self.broadcast({"type": "approval", "approval": self.approval(a["id"])})

    def expire_due(self) -> int:
        due = self.db.rows("SELECT * FROM terminal_approvals WHERE status = 'pending' AND expires_at < :now", now=self.clock())
        for a in due:
            self._close_approval(a, "expired", "nobody approved it in time")
        return len(due)

    def _entry_filled(self, link_id: str) -> None:
        link = self.links.get(link_id)
        if link is None or link["state"] not in ("live", "entering") or link["stop_order_id"] or link["last_stop"] is None:
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
