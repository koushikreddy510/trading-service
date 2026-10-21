"""Read-only views of the broker account for the order ticket: funds, a full quote, and what an order would cost.

Nothing here places, modifies or cancels an order. GetOrderMargin only calculates the margin an order would block;
PositionBook is read so the ticket can warn when an order would net into a position that is already there (the
account holds NRML positions we must never touch, and a new NRML order in the same contract would merge with them).

Quantities: derivatives (MCX, NFO, ...) are entered in LOTS and sent to the broker as lots x lot size; stocks in shares.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .feed.base import BrokerError
from .timeframes import exchange_of

EQUITY = {"NSE", "BSE"}
FUNDS_CACHE_SECONDS = 15
PRODUCTS_CACHE_SECONDS = 3600


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _opt(row: dict, key: str) -> Optional[float]:
    try:
        v = float(row[key])
    except (KeyError, TypeError, ValueError):
        return None
    return v


def parse_limits(row: dict) -> dict:
    """Flattrade `Limits` -> what the ticket shows. `marginused` is left out of the reply when it is zero."""
    cash, payin, collateral = _f(row, "cash"), _f(row, "payin"), _f(row, "brkcollamt")
    extra = _f(row, "daycash") + _f(row, "unclearedcash")
    used = _f(row, "marginused")
    return {
        "cash": cash, "payin": payin, "collateral": collateral, "margin_used": used,
        "available": round(cash + payin + collateral + extra - used, 2),
        "payout": _f(row, "payout"), "blocked": _f(row, "blk_amt"),
    }


def parse_quote(q: dict, symbol: str) -> dict:
    ltp, prev = _opt(q, "lp"), _opt(q, "c")
    change = ltp - prev if ltp is not None and prev else None
    return {
        "symbol": symbol, "ltp": ltp, "prev_close": prev, "change": change,
        "change_pct": change / prev * 100 if change is not None and prev else None,
        "open": _opt(q, "o"), "high": _opt(q, "h"), "low": _opt(q, "l"),
        "bid": _opt(q, "bp1"), "bid_qty": _opt(q, "bq1"), "ask": _opt(q, "sp1"), "ask_qty": _opt(q, "sq1"),
        "upper_circuit": _opt(q, "uc"), "lower_circuit": _opt(q, "lc"),
        "volume": _opt(q, "v"), "oi": _opt(q, "oi"), "lot_size": int(_f(q, "ls") or 1), "tick_size": _opt(q, "ti"),
        "expiry": q.get("exd"), "instrument": q.get("instname"),
    }


class BrokerAccount:
    """The Flattrade account behind the live terminal (None in demo mode)."""

    def __init__(self, api: Any, resolver: Any, instruments: Any, account_id: str):
        self.api, self.resolver, self.instruments, self.account_id = api, resolver, instruments, account_id
        self._funds: tuple[float, dict] | None = None
        self._products: tuple[float, dict[str, list[dict]]] | None = None

    def _post(self, endpoint: str, payload: dict) -> Any:
        return self.api.call(endpoint, payload)

    # ------------------------------------------------------------------ funds
    def funds(self) -> dict:
        now = time.monotonic()
        if self._funds and now - self._funds[0] < FUNDS_CACHE_SECONDS:
            return self._funds[1]
        out = parse_limits(self._post("Limits", {"actid": self.account_id}))
        self._funds = (now, out)
        return out

    def products(self) -> dict[str, list[dict]]:
        """Per exchange, the products this account may use: {"MCX": [{"prd": "M", "name": "NRML"}, ...]}."""
        now = time.monotonic()
        if self._products and now - self._products[0] < PRODUCTS_CACHE_SECONDS:
            return self._products[1]
        out: dict[str, list[dict]] = {}
        for p in self._post("UserDetails", {}).get("prarr") or []:
            for ex in p.get("exch") or []:
                out.setdefault(ex, []).append({"prd": p["prd"], "name": p.get("s_prdt_ali") or p["prd"]})
        self._products = (now, out)
        return out

    # ------------------------------------------------------------------ quote
    def quote(self, symbol: str) -> dict:
        exchange, token = self.resolver.resolve(symbol)
        out = parse_quote(self._post("GetQuotes", {"exch": exchange, "token": token}), symbol)
        out["point_value"] = self.instruments.point_value(symbol) if exchange not in EQUITY else 1.0
        out["quantity_unit"] = "shares" if exchange in EQUITY else "lots"
        return out

    # ---------------------------------------------------------------- preview
    def preview(self, symbol: str, side: str, quantity: int, product: Optional[str] = None,
                price_type: str = "LMT", price: Optional[float] = None) -> dict:
        """What this order would cost: contract value, the margin the broker would block, funds left. Never sends it."""
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if quantity < 1:
            raise ValueError("quantity must be at least 1")
        price_type = price_type.upper()
        if price_type not in ("LMT", "MKT"):
            raise ValueError("price type must be LMT or MKT")
        exchange = exchange_of(symbol)
        allowed = self.products().get(exchange, [])
        if product is None:
            product = "C" if exchange in EQUITY else "M"
        if product not in {p["prd"] for p in allowed}:
            names = ", ".join(f"{p['name']} ({p['prd']})" for p in allowed) or "none"
            raise ValueError(f"product {product!r} is not enabled for {exchange} on this account; allowed: {names}")

        q = self.quote(symbol)
        px = price if (price_type == "LMT" and price) else q["ltp"]
        if not px:
            raise BrokerError(f"{symbol}: no price to calculate with")
        lots = quantity
        units = quantity if exchange in EQUITY else quantity * q["lot_size"]
        tsym = symbol.split(":", 1)[1]
        m = self._post("GetOrderMargin", {
            "actid": self.account_id, "exch": exchange, "tsym": tsym, "qty": str(units), "prc": str(px),
            "prctyp": price_type, "prd": product, "trantype": "B" if side == "BUY" else "S",
        })
        funds = self.funds()
        margin = _f(m, "ordermargin")
        value = px * (units if exchange in EQUITY else lots * q["point_value"])
        return {
            "symbol": symbol, "side": side, "product": product,
            "product_name": next((p["name"] for p in allowed if p["prd"] == product), product),
            "price_type": price_type, "price": px, "quantity": lots, "units": units,
            "quantity_unit": q["quantity_unit"], "lot_size": q["lot_size"],
            "contract_value": round(value, 2), "margin_required": margin,
            "available": funds["available"], "shortfall": round(max(0.0, margin - funds["available"]), 2),
            "broker_remarks": m.get("remarks") or "", "existing_position": self._existing(exchange, tsym, product),
            "quote": q, "preview_only": True,
        }

    def _existing(self, exchange: str, tsym: str, product: str) -> Optional[dict]:
        """A position already open in this contract and product: a new order would net with it."""
        try:
            rows = self._post("PositionBook", {"actid": self.account_id})
        except BrokerError:
            return None                       # "no data" when the book is empty
        for r in rows if isinstance(rows, list) else []:
            if r.get("exch") == exchange and r.get("tsym") == tsym and r.get("prd") == product and int(float(r.get("netqty") or 0)):
                return {"net_quantity": int(float(r["netqty"])), "product": product, "avg_price": _opt(r, "netavgprc")}
        return None
