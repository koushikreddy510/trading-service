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
* a stop-loss (SL) only closes what our own fills hold, and all our pending stops together never exceed it.

Tracking: open orders are refreshed from the broker's order book every few seconds until they finish.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .db import Database, new_id
from .timeframes import IST, exchange_of

log = logging.getLogger(__name__)

EQUITY = {"NSE", "BSE"}
KILL = "*"                                   # the settings row that is the global kill switch
DEFAULTS = {"live_enabled": False, "max_order_value": 20000.0, "max_orders_per_day": 20}
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
        row = self.db.row("SELECT live_enabled, max_order_value, max_orders_per_day FROM terminal_live_settings WHERE account = :a", a=account)
        out = dict(DEFAULTS, **({k: v for k, v in row.items() if v is not None} if row else {}))
        if account == KILL and not row:
            out["live_enabled"] = True       # the kill switch starts released; accounts start switched off
        return out

    def set(self, account: str, **changes: Any) -> dict:
        cur = self.get(account)
        cur.update({k: v for k, v in changes.items() if k in DEFAULTS and v is not None})
        if cur["max_order_value"] <= 0 or cur["max_orders_per_day"] < 1:
            raise OrderRefused("limits must be positive")
        self.db.execute(
            """
            INSERT INTO terminal_live_settings (account, live_enabled, max_order_value, max_orders_per_day, updated_at)
            VALUES (:a, :e, :v, :n, now())
            ON CONFLICT (account) DO UPDATE SET live_enabled = EXCLUDED.live_enabled, max_order_value = EXCLUDED.max_order_value,
                max_orders_per_day = EXCLUDED.max_orders_per_day, updated_at = now()
            """,
            a=account, e=bool(cur["live_enabled"]), v=float(cur["max_order_value"]), n=int(cur["max_orders_per_day"]),
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
            INSERT INTO terminal_orders (id, account, symbol, side, quantity, units, product, price, trigger_price, status, source, link, purpose)
            VALUES (:id, :account, :symbol, :side, :quantity, :units, :product, :price, :trigger_price, 'submitting', :source, :link, :purpose)
            """,
            **{k: o.get(k) for k in ("id", "account", "symbol", "side", "quantity", "units", "product", "price", "trigger_price", "source",
                                     "link", "purpose")},
        )

    def update(self, order_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        self.db.execute(f"UPDATE terminal_orders SET {sets}, updated_at = now() WHERE id = :id", id=order_id, **fields)

    def get(self, order_id: str) -> Optional[dict]:
        return self.db.row("SELECT * FROM terminal_orders WHERE id = :i", i=order_id)

    def today(self) -> list[dict]:
        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.db.rows("SELECT * FROM terminal_orders WHERE created_at >= :s ORDER BY created_at DESC", s=start)

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
        self.notify = notify or (lambda text: None)
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
        external = broker.net_position(symbol, product) - self.repo.our_net(account, symbol, product)
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
            self.notify(f"❌ *Order failed* {account} · {side} {quantity} {symbol} @ {o['price']}: {e}")
            raise
        self.repo.update(o["id"], status="open", broker_order_id=broker_id)
        row = _json(self.repo.get(o["id"]))  # type: ignore[arg-type]
        self.broadcast({"type": "order", "order": row})
        stop = f" stop {o['trigger_price']}" if o.get("trigger_price") else ""
        self.notify(f"📤 *Order sent* {account} · {side} {quantity} {symbol}{stop} @ {o['price']} ({o['product']}) · #{broker_id}")
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
        self.notify(f"↗️ *Stop moved* {o['account']} · {o['side']} {o['quantity']} {o['symbol']} stop {o['trigger_price']} → {new['trigger_price']}")
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
                        icon = {"complete": "✅", "rejected": "⛔", "cancelled": "⚪"}[b["status"]]
                        extra = f" @ {b['avg_price']}" if b["status"] == "complete" else (f": {b['message']}" if b["message"] else "")
                        self.notify(f"{icon} *Order {b['status']}* {account} · {o['side']} {o['quantity']} {o['symbol']}{extra}")
        return changed

    def today(self) -> list[dict]:
        return [_json(o) for o in self.repo.today()]
