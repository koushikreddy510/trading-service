"""Live orders: the only code that sends an order to a broker, and the checks it must pass first.

Every order is written to our ledger (terminal_orders) before it is sent and carries our id in the broker's remarks, so
we always know which orders and quantities are ours. The account also holds positions we did not make (NRML positions
the user manages by hand); nothing here may touch them:

* an order is refused if the broker already has a position in that instrument and product that our ledger does not
  explain (a new order would net into it);
* products are our policy (stocks MTF / Intraday, derivatives NRML / Intraday; see account.POLICY);
* live orders are off per account until switched on; a global kill switch stops everything;
* each order is capped in value, and the number of orders per day is capped;
* "market" orders are sent as limit orders just through the spread (Flattrade's API v2 takes limit orders only);
* a stop-loss (SL) only closes what our own fills hold, and all our pending stops together never exceed it;
* an order that adds exposure must pass the broker's margin calculator (MCX has its own funds); exits never wait on it.

Tracking: open orders are refreshed from the broker's order book every few seconds until they finish.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .db import Database, new_id
from .timeframes import IST, exchange_of

log = logging.getLogger(__name__)

EQUITY = {"NSE", "BSE"}
KILL = "*"                                   # the settings row that is the global kill switch
# require_approval: a live rule's entry waits for a person (approval_minutes to decide); stops and exits never wait
DEFAULTS = {"live_enabled": False, "max_order_value": 20000.0, "max_orders_per_day": 20, "require_approval": True, "approval_minutes": 5}
MARKETABLE_PCT = 0.3                         # a "market" order is a limit this far through the touch
OPEN = ("submitting", "open")


class OrderRefused(Exception):
    """A check said no; nothing was sent."""


def round_to_tick(price: float, tick: float, up: bool) -> float:
    tick = tick or 0.05
    n = price / tick
    return round((math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * tick, 2)


class LiveSettings:
    def __init__(self, db: Database):
        self.db = db

    def get(self, account: str) -> dict:
        row = self.db.row("SELECT live_enabled, max_order_value, max_orders_per_day, require_approval, approval_minutes "
                          "FROM terminal_live_settings WHERE account = :a", a=account)
        out = dict(DEFAULTS, **({k: v for k, v in row.items() if v is not None} if row else {}))
        if account == KILL and not row:
            out["live_enabled"] = True       # the kill switch starts released; accounts start switched off
        return out

    def set(self, account: str, **changes: Any) -> dict:
        cur = self.get(account)
        cur.update({k: v for k, v in changes.items() if k in DEFAULTS and v is not None})
        if cur["max_order_value"] <= 0 or cur["max_orders_per_day"] < 1 or not 1 <= int(cur["approval_minutes"]) <= 60:
            raise OrderRefused("limits must be positive (approval window 1-60 minutes)")
        self.db.execute(
            """
            INSERT INTO terminal_live_settings (account, live_enabled, max_order_value, max_orders_per_day, require_approval, approval_minutes, updated_at)
            VALUES (:a, :e, :v, :n, :r, :m, now())
            ON CONFLICT (account) DO UPDATE SET live_enabled = EXCLUDED.live_enabled, max_order_value = EXCLUDED.max_order_value,
                max_orders_per_day = EXCLUDED.max_orders_per_day, require_approval = EXCLUDED.require_approval,
                approval_minutes = EXCLUDED.approval_minutes, updated_at = now()
            """,
            a=account, e=bool(cur["live_enabled"]), v=float(cur["max_order_value"]), n=int(cur["max_orders_per_day"]),
            r=bool(cur["require_approval"]), m=int(cur["approval_minutes"]),
        )
        return self.get(account)

    def killed(self) -> bool:
        return not self.get(KILL)["live_enabled"]


class OrderRepo:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, o: dict) -> None:
        self.db.execute(
            """
            INSERT INTO terminal_orders (id, account, symbol, side, quantity, units, product, price, trigger_price, status, source, link, purpose,
                                         order_type)
            VALUES (:id, :account, :symbol, :side, :quantity, :units, :product, :price, :trigger_price, 'submitting', :source, :link, :purpose,
                    :price_type)
            """,
            **{k: o.get(k) for k in ("id", "account", "symbol", "side", "quantity", "units", "product", "price", "trigger_price", "source",
                                     "link", "purpose", "price_type")},
        )

    def update(self, order_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        self.db.execute(f"UPDATE terminal_orders SET {sets}, updated_at = now() WHERE id = :id", id=order_id, **fields)

    def get(self, order_id: str) -> Optional[dict]:
        return self.db.row("SELECT * FROM terminal_orders WHERE id = :i", i=order_id)

    def today(self, days: int = 1) -> list[dict]:
        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=max(1, days) - 1)
        return self.db.rows("SELECT * FROM terminal_orders WHERE created_at >= :s ORDER BY created_at DESC", s=start)

    def positions(self) -> list[dict]:
        """What this app holds, from its own fills only (average-cost method). Intraday counts today's orders only."""
        today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self.db.rows("SELECT * FROM terminal_orders WHERE filled_units > 0 AND (product <> 'I' OR created_at >= :d) "
                            "ORDER BY created_at", d=today)
        book: dict[tuple, dict] = {}
        for r in rows:
            k = (r["account"], r["symbol"], r["product"])
            pos = book.setdefault(k, {"account": k[0], "symbol": k[1], "product": k[2], "units": 0, "avg_price": 0.0, "realized": 0.0,
                                      "per_qty": max(1, r["units"] // max(1, r["quantity"]))})
            signed = r["filled_units"] if r["side"] == "BUY" else -r["filled_units"]
            px = r["avg_price"] or r["price"]
            if pos["units"] == 0 or (pos["units"] > 0) == (signed > 0):          # opening or adding
                total = pos["units"] + signed
                pos["avg_price"] = (pos["avg_price"] * abs(pos["units"]) + px * abs(signed)) / abs(total)
                pos["units"] = total
            else:                                                                   # reducing (or flipping)
                closing = min(abs(signed), abs(pos["units"]))
                pos["realized"] += closing * (px - pos["avg_price"]) * (1 if pos["units"] > 0 else -1)
                left = pos["units"] + signed
                if left and (left > 0) != (pos["units"] > 0):
                    pos["avg_price"] = px
                pos["units"] = left
        return list(book.values())

    def open(self) -> list[dict]:
        return self.db.rows("SELECT * FROM terminal_orders WHERE status IN ('submitting', 'open') ORDER BY created_at")

    def sent_today(self, account: str) -> int:
        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(self.db.scalar("SELECT count(*) FROM terminal_orders WHERE account = :a AND created_at >= :s AND status <> 'refused'",
                                  a=account, s=start) or 0)

    def pending_stop_units(self, account: str, symbol: str, product: str, side: str, exclude: Optional[str] = None) -> int:
        return int(self.db.scalar(
            "SELECT coalesce(sum(units - filled_units), 0) FROM terminal_orders WHERE account = :a AND symbol = :s AND product = :p "
            "AND side = :sd AND trigger_price IS NOT NULL AND status IN ('submitting', 'open') AND id <> :x",
            a=account, s=symbol, p=product, sd=side, x=exclude or "") or 0)

    def for_link(self, link: str) -> list[dict]:
        return self.db.rows("SELECT * FROM terminal_orders WHERE link = :l ORDER BY created_at", l=link)

    def link_net(self, link: str) -> int:
        """Units a mirrored position holds through its own filled orders (+ long / - short)."""
        return int(self.db.scalar("SELECT coalesce(sum(CASE WHEN side = 'BUY' THEN filled_units ELSE -filled_units END), 0) "
                                  "FROM terminal_orders WHERE link = :l", l=link) or 0)

    def our_net(self, account: str, symbol: str, product: str) -> int:
        """Units we hold through our own filled orders (+ long / - short). Intraday positions end with the day."""
        sql = ("SELECT coalesce(sum(CASE WHEN side = 'BUY' THEN filled_units ELSE -filled_units END), 0) FROM terminal_orders "
               "WHERE account = :a AND symbol = :s AND product = :p")
        params: dict[str, Any] = {"a": account, "s": symbol, "p": product}
        if product == "I":
            sql += " AND created_at >= :d"
            params["d"] = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(self.db.scalar(sql, **params) or 0)


PRODUCT_NAME = {"I": "Intraday", "F": "MTF", "M": "NRML", "C": "Delivery"}


def _rs(v: Optional[float]) -> str:
    return "-" if v is None else f"₹{v:,.2f}"


def _json(o: dict) -> dict:
    out = dict(o)
    for k in ("created_at", "updated_at"):
        if isinstance(out.get(k), datetime):
            out[k] = out[k].isoformat()
    return out


class OrderService:
    def __init__(self, db: Database, accounts: Callable[[], dict[str, Any]], quote: Callable[[str], dict],
                 notify: Optional[Callable[[str], None]] = None, broadcast: Optional[Callable[[dict], None]] = None):
        """`accounts()` -> {name: broker}; `quote(symbol)` -> the data feed's quote (ltp, bid, ask, tick_size, lot_size,
        point_value, circuits). Data always comes from the data broker, whichever account the order goes to."""
        self.db, self.accounts, self.quote = db, accounts, quote
        self.repo, self.settings = OrderRepo(db), LiveSettings(db)
        self.notify = notify or (lambda text, channel="orders": None)   # channel: orders (fills, alerts) | updates (the steps)
        self.broadcast = broadcast or (lambda event: None)
        self.on_change: list[Callable[[dict], None]] = []     # told about every order whose status/fill changed (live_mirror)

    # ------------------------------------------------------------------ checks
    def _broker(self, account: str) -> Any:
        broker = self.accounts().get(account)
        if broker is None:
            raise OrderRefused(f"account {account!r} is not connected")
        return broker

    def check(self, account: str, symbol: str, side: str, quantity: int, product: str, price_type: str = "LMT",
              price: Optional[float] = None, trigger_price: Optional[float] = None, replacing: Optional[str] = None) -> dict:
        """Everything that must hold before an order is sent. Returns the order as it would be sent; raises OrderRefused."""
        side, price_type = side.upper(), price_type.upper()
        if side not in ("BUY", "SELL"):
            raise OrderRefused("side must be BUY or SELL")
        if quantity < 1:
            raise OrderRefused("quantity must be at least 1")
        if price_type not in ("LMT", "MKT", "SL"):
            raise OrderRefused("price type must be LMT, MKT or SL")
        if symbol.upper().endswith("-INDEX"):
            raise OrderRefused("an index cannot be traded")
        broker = self._broker(account)
        if self.settings.killed():
            raise OrderRefused("the kill switch is on: no live orders until it is released")
        limits = self.settings.get(account)
        if not limits["live_enabled"]:
            raise OrderRefused(f"live orders are off for {account}; switch them on in the order ticket first")
        exchange = exchange_of(symbol)
        allowed = {p["prd"]: p for p in broker.products().get(exchange, [])}
        if product not in allowed:
            raise OrderRefused(f"product {product!r} is not used for {exchange}; use " + " or ".join(p["name"] for p in allowed.values()))
        if not allowed[product]["listed"]:
            raise OrderRefused(f"{allowed[product]['name']} is not activated on the {account} account")

        q = self.quote(symbol)
        tick = q.get("tick_size") or 0.05
        trigger = None
        if price_type == "SL":
            if not trigger_price or trigger_price <= 0:
                raise OrderRefused("a stop-loss needs a trigger price")
            trigger = round_to_tick(trigger_price, tick, up=side == "BUY")
            ltp = q.get("ltp") or 0
            if ltp and (trigger >= ltp if side == "SELL" else trigger <= ltp):
                raise OrderRefused(f"a {side.lower()} stop must trigger {'below' if side == 'SELL' else 'above'} the last price {ltp}")
            # the limit once triggered: given, or 0.5% beyond the trigger so it fills in a fast move
            px = round_to_tick(price or trigger * (0.995 if side == "SELL" else 1.005), tick, up=side == "BUY")
            if (px > trigger if side == "SELL" else px < trigger):
                raise OrderRefused("a stop's limit must be at or beyond its trigger")
        elif price_type == "LMT":
            if not price or price <= 0:
                raise OrderRefused("a limit order needs a price")
            px = round_to_tick(price, tick, up=False)
        else:
            touch = (q.get("ask") if side == "BUY" else q.get("bid")) or q.get("ltp")
            if not touch:
                raise OrderRefused(f"{symbol}: no price to build a marketable limit from")
            px = round_to_tick(touch * (1 + MARKETABLE_PCT / 100 if side == "BUY" else 1 - MARKETABLE_PCT / 100), tick, up=side == "BUY")
            lo, hi = q.get("lower_circuit"), q.get("upper_circuit")
            px = min(px, hi) if hi else px
            px = max(px, lo) if lo else px
        units = quantity if exchange in EQUITY else quantity * int(q.get("lot_size") or 1)
        value = px * (units if exchange in EQUITY else quantity * float(q.get("point_value") or 1))
        if value > limits["max_order_value"]:
            raise OrderRefused(f"order value ₹{value:,.0f} is above the ₹{limits['max_order_value']:,.0f} limit for {account}")
        if replacing is None and self.repo.sent_today(account) >= limits["max_orders_per_day"]:
            raise OrderRefused(f"{limits['max_orders_per_day']} orders already sent today on {account} (the daily limit)")
        if trigger is not None:
            held = self.repo.our_net(account, symbol, product) * (1 if side == "SELL" else -1)
            covered = self.repo.pending_stop_units(account, symbol, product, side, exclude=replacing)
            if units > held - covered:
                raise OrderRefused(f"a stop-loss only protects what we hold: {held} units held, {covered} already covered by stops")
        held = self.repo.our_net(account, symbol, product)
        reduces = trigger is not None or (held > 0 and side == "SELL") or (held < 0 and side == "BUY")
        if not reduces and hasattr(broker, "margin_check"):     # an exit or a stop is never held back for margin
            ok, remark, margin = broker.margin_check(symbol, side, units, product, px)
            if not ok:
                segment = "commodity (MCX)" if exchange == "MCX" else "equity" if exchange in EQUITY else "derivatives"
                raise OrderRefused(f"not enough funds in the {segment} segment for this order (margin ₹{margin:,.0f}; broker: {remark})")
        external = broker.net_position(symbol, product) - held
        if external:
            raise OrderRefused(f"the account already holds {external:+d} units of {symbol} as {allowed[product]['name']} that are not ours; "
                               "an order here would net into that position, so it is refused")
        return {"account": account, "symbol": symbol, "side": side, "quantity": quantity, "units": units, "product": product,
                "price": px, "trigger_price": trigger, "value": round(value, 2), "price_type": price_type}

    # ------------------------------------------------------------------ actions
    def place(self, account: str, symbol: str, side: str, quantity: int, product: str, price_type: str = "LMT",
              price: Optional[float] = None, source: str = "ticket", trigger_price: Optional[float] = None,
              link: Optional[str] = None, purpose: Optional[str] = None) -> dict:
        o = self.check(account, symbol, side, quantity, product, price_type, price, trigger_price)
        o.update(id=new_id(), source=source, link=link, purpose=purpose)
        self.repo.insert(o)                       # in the ledger before it leaves, so a crash mid-send is still ours
        try:
            broker_id = self._broker(account).place({**o, "tag": f"atlas:{o['id']}"})
        except Exception as e:  # noqa: BLE001 - whatever the broker says, record it
            self.repo.update(o["id"], status="failed", message=str(e)[:500])
            self.notify(f"❌ *Order failed* · {self._what(o)}\n{e}", channel="orders")
            raise
        self.repo.update(o["id"], status="open", broker_order_id=broker_id)
        row = _json(self.repo.get(o["id"]))  # type: ignore[arg-type]
        self.broadcast({"type": "order", "order": row})
        head = "🛡 *Stop-loss placed*" if o.get("trigger_price") else "📤 *Order sent*"
        self.notify(f"{head} · {self._what(o)} · #{broker_id}", channel="updates")
        return row

    def modify_stop(self, order_id: str, trigger_price: float, price: Optional[float] = None, quantity: Optional[int] = None) -> dict:
        """Move an open stop-loss (trail it) or shrink it after a partial exit: the same broker order, a new trigger
        and/or quantity. Same checks as a new stop."""
        o = self.repo.get(order_id)
        if o is None or o["trigger_price"] is None:
            raise OrderRefused("no such stop-loss order")
        if o["status"] not in OPEN or not o["broker_order_id"]:
            raise OrderRefused(f"order is {o['status']}; only an open stop can be moved")
        qty = quantity or o["quantity"]
        new = self.check(o["account"], o["symbol"], o["side"], qty, o["product"], "SL", price, trigger_price, replacing=order_id)
        self._broker(o["account"]).modify(o["broker_order_id"], new)
        self.repo.update(order_id, trigger_price=new["trigger_price"], price=new["price"], quantity=qty, units=new["units"])
        row = _json(self.repo.get(order_id))  # type: ignore[arg-type]
        self.broadcast({"type": "order", "order": row})
        step = abs(new["trigger_price"] - o["trigger_price"])
        kind = "Stop resized" if qty != o["quantity"] and not step else "Trailing stop moved"
        self.notify(f"🔁 *{kind}* · {o['side'].lower()}-stop {qty} {o['symbol']} · trigger {_rs(o['trigger_price'])} → {_rs(new['trigger_price'])}"
                    + (f" ({_rs(step)} tighter)" if step else "") + f" · {PRODUCT_NAME.get(o['product'], o['product'])} · from {self._source(o)}",
                    channel="updates")
        return row

    def cancel(self, order_id: str) -> dict:
        o = self.repo.get(order_id)
        if o is None:
            raise OrderRefused("no such order")
        if o["status"] not in OPEN or not o["broker_order_id"]:
            raise OrderRefused(f"order is {o['status']}; only an open order can be cancelled")
        self._broker(o["account"]).cancel(o["broker_order_id"])
        self.sync()
        return _json(self.repo.get(order_id))  # type: ignore[arg-type]

    def sync(self) -> list[dict]:
        """Refresh open orders from each broker's order book; returns the ones that changed."""
        changed = []
        by_account: dict[str, list[dict]] = {}
        for o in self.repo.open():
            by_account.setdefault(o["account"], []).append(o)
        for account, mine in by_account.items():
            try:
                book = {b["broker_order_id"]: b for b in self._broker(account).orders()}
            except Exception as e:  # noqa: BLE001 - try again on the next round
                log.warning("order sync %s: %s", account, e)
                continue
            for o in mine:
                b = book.get(o["broker_order_id"] or "")
                if b is None:
                    continue
                if (b["status"], b["filled_units"]) != (o["status"], o["filled_units"]):
                    self.repo.update(o["id"], status=b["status"], filled_units=b["filled_units"], avg_price=b["avg_price"],
                                     message=(b["message"] or "")[:500])
                    row = _json(self.repo.get(o["id"]))  # type: ignore[arg-type]
                    changed.append(row)
                    self.broadcast({"type": "order", "order": row})
                    for listener in self.on_change:
                        try:
                            listener(row)
                        except Exception:  # noqa: BLE001 - a listener must never break tracking
                            log.exception("order listener failed")
                    if b["status"] in ("complete", "rejected", "cancelled"):
                        self._announce(row)
        return changed

    # --------------------------------------------------------------- messages
    def _source(self, o: dict) -> str:
        src = o.get("source") or "ticket"
        if src.startswith("rule:"):
            name = self.db.scalar("SELECT name FROM terminal_rules WHERE id = :i", i=src[5:]) or src[5:]
            return f"rule “{name}”" + (f" ({o['purpose']})" if o.get("purpose") else "")
        return "order ticket"

    def _what(self, o: dict) -> str:
        """SELL 2 MCX:GOLDPETAL30OCT26 · Market (limit ₹14,520.00) · Intraday · from rule “…”"""
        if o.get("trigger_price"):
            kind = f"Stop-loss, trigger {_rs(o['trigger_price'])} (limit {_rs(o['price'])})"
        elif (o.get("price_type") or o.get("order_type")) == "MKT":
            kind = f"Market (limit {_rs(o['price'])})"
        else:
            kind = f"Limit {_rs(o['price'])}"
        return f"{o['side']} {o['quantity']} {o['symbol']} · {kind} · {PRODUCT_NAME.get(o['product'], o['product'])} · from {self._source(o)}"

    def _announce(self, o: dict) -> None:
        """A finished order: fills and refusals to #orders, cancellations to #updates."""
        if o["status"] == "cancelled":
            self.notify(f"⚪ *Cancelled* · {self._what(o)}", channel="updates")
            return
        if o["status"] == "rejected":
            self.notify(f"⛔ *Rejected by the broker* · {self._what(o)}\n{o.get('message') or ''}", channel="orders")
            return
        verb = "BOUGHT" if o["side"] == "BUY" else "SOLD"
        head = "🛑 *Stop-loss hit*" if o.get("trigger_price") else ("🟢" if o["side"] == "BUY" else "🔴") + f" *{verb}*"
        text = f"{head} · {o['quantity']} {o['symbol']} @ {_rs(o.get('avg_price'))} · {PRODUCT_NAME.get(o['product'], o['product'])} · from {self._source(o)}"
        pnl = self._exit_pnl(o)
        if pnl is not None:
            text += f"\n{'✅' if pnl >= 0 else '🔻'} P&L on this exit: {'+' if pnl >= 0 else '−'}{_rs(abs(pnl))}"
        self.notify(text, channel="orders")

    def _exit_pnl(self, o: dict) -> Optional[float]:
        """For an exit of a mirrored position: (exit - entry) x quantity x direction x point value."""
        if not o.get("link") or o.get("purpose") not in ("exit", "stop") or not o.get("avg_price"):
            return None
        entry = next((r for r in self.repo.for_link(o["link"]) if r["purpose"] == "entry" and r["avg_price"]), None)
        if entry is None:
            return None
        direction = 1 if entry["side"] == "BUY" else -1
        if exchange_of(o["symbol"]) in EQUITY:
            size, pv = o["filled_units"], 1.0
        else:                                  # derivatives: lots x the lot's point value
            size = o["filled_units"] / max(1, entry["units"] // max(1, entry["quantity"]))
            try:
                pv = float(self.quote(o["symbol"]).get("point_value") or 1.0)
            except Exception:  # noqa: BLE001
                pv = 1.0
        return round((o["avg_price"] - entry["avg_price"]) * direction * size * pv, 2)

    def today(self, days: int = 1) -> list[dict]:
        return [_json(o) for o in self.repo.today(days)]

    def positions(self, mark: Callable[[str], Optional[float]] = lambda s: None) -> list[dict]:
        """Our live positions with the last price, unrealised P&L and the open stops protecting each."""
        open_orders = self.repo.open()
        out = []
        for p in self.repo.positions():
            ltp = mark(p["symbol"])
            p["quantity"] = abs(p["units"]) // p["per_qty"]
            p["side"] = "BUY" if p["units"] > 0 else "SELL" if p["units"] < 0 else None
            p["ltp"] = ltp
            p["unrealized"] = round((ltp - p["avg_price"]) * p["units"], 2) if ltp and p["units"] else 0.0
            p["realized"] = round(p["realized"], 2)
            p["avg_price"] = round(p["avg_price"], 4)
            p["stops"] = [{"id": o["id"], "trigger_price": o["trigger_price"], "quantity": o["quantity"]} for o in open_orders
                          if o["trigger_price"] and o["symbol"] == p["symbol"] and o["product"] == p["product"] and o["account"] == p["account"]]
            out.append(p)
        return sorted(out, key=lambda x: (x["units"] == 0, x["symbol"]))
