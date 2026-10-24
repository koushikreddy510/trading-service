"""HTTP + websocket API for the terminal UI. Mounted under /terminal.

REST (JSON)                                                   websocket  /terminal/ws
  GET    /status                                                client -> {"type":"subscribe","symbols":[...]}
  GET    /symbols/search?q=  (stocks + MCX futures)                         {"type":"unsubscribe","symbols":[...]}
  GET    /candles?symbol=&timeframe=&limit=                                 {"type":"ping"}
  GET    /indicators                                            server -> {"type":"hello"|"tick"|"bar_closed"|
  GET/POST /watchlists   PATCH/DELETE /watchlists/{id}                       "signal"|"position"|"status"|"error"}
         POST /watchlists/{id}/symbols  DELETE .../symbols/{symbol}  PUT .../order
  GET/POST /drawings     PUT/DELETE /drawings/{id}
  GET/POST /rules        PUT/DELETE /rules/{id}   PATCH /rules/{id}/enabled   GET /rules/watch (how close each rule is)
  GET    /signals
  GET    /positions      POST /positions/{id}/close
  GET    /financials?symbol=      GET /results/recent
  GET    /sectors                 GET /sectors/compare?group=|sector=
  POST   /notify/data-digest   (Slack digest of data freshness and symbol-list changes)
  GET    /account/funds  GET /account/products  GET /quote?symbol=    (live broker, read-only)
  POST   /order-preview  {symbol, side, quantity, product?, price_type?, price?}  margin + value; never places an order
  GET    /brokers   POST /brokers/{broker}/login {code?}   POST /brokers/login-all   (daily broker logins)
  POST   /instruments/refresh  (reloads the MCX futures list from Flattrade's scrip master; a daily job calls this)
  POST   /notify/broker-tokens (checks each broker's token; Slack says which logins are needed; a scheduled job calls this)
  GET    /data/status          (how complete and fresh the data is, symbol changes, the scrape jobs)
  POST   /data/run             {job, mode?, days?, limit?} starts a scrape job in the strategy-screener
  POST   /backtests (runs in the background)   GET /backtests   GET/DELETE /backtests/{id}
  GET    /screener/presets  /screener/columns  /screener/preset/{name}
  POST   /screener/run  /screener/scan
  GET    /screener-sync/strategies  /screener-sync/links
  POST   /screener-sync/preview  /screener-sync/apply  /screener-sync/refresh     DELETE /screener-sync/links/{watchlist_id}
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from . import datajobs, financials as fin, instruments
from . import screener, screener_sync as sync, sectors, symbols as symbol_search
from .backtest import BacktestError
from .backtest_jobs import BacktestStore
from .notify import data_digest, digest_text
from .db import get_db
from .drawings import Drawing, InvalidDrawing, validate_drawing
from .indicators import SPECS
from .rules import InvalidRule, referenced_ids, referenced_refs_of, rule_from_payload
from .runtime import Terminal, build_terminal
from .timeframes import TIMEFRAMES, exchange_of, is_complete, validate_timeframe
from .strategies import StrategyError, StrategyRepo
from .watchlists import WatchlistError, normalise_symbol

router = APIRouter(prefix="/terminal", tags=["terminal"])


def terminal_of(request: Request) -> Terminal:
    t = getattr(request.app.state, "terminal", None)
    if t is None:
        raise HTTPException(503, "terminal is not running (is DATABASE_URL set?)")
    return t


def bad_request(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


def symbol_param(raw: str) -> str:
    try:
        return normalise_symbol(raw)
    except WatchlistError as e:
        raise bad_request(e) from None


# ------------------------------------------------------------------------ meta
@router.get("/status")
def status(request: Request):
    return terminal_of(request).status()


@router.get("/indicators")
def indicator_specs():
    """Indicators the chart and the rule builder offer, with default parameters and output names."""
    return {"indicators": [{"name": n, "defaults": d, "outputs": list(o)} for n, (d, o) in SPECS.items()], "timeframes": TIMEFRAMES}


@router.get("/symbols/search")
def search_symbols(request: Request, q: str = Query("", max_length=40), limit: int = Query(20, ge=1, le=50)):
    return {"results": symbol_search.search(terminal_of(request).db, q, limit)}


# ------------------------------------------------------------------ broker logins
@router.get("/brokers")
async def brokers(request: Request):
    """Each broker: is today's token accepted, where it came from, is unattended login set up, its login page."""
    return {"brokers": await asyncio.to_thread(terminal_of(request).logins.status)}


@router.post("/brokers/{broker}/login")
async def broker_login(request: Request, broker: str, body: dict = Body(default={})):
    """Log in now: unattended (no body), or with the URL the broker's login page redirected to ({"code": url})."""
    from broker_auth.providers import AuthError

    try:
        return await terminal_of(request).logins.login(broker, (body or {}).get("code") or None)
    except ValueError as e:
        raise bad_request(e) from None
    except AuthError as e:
        raise HTTPException(502, str(e)) from None


@router.post("/brokers/login-all")
async def broker_login_all(request: Request):
    """The morning job: unattended login for every broker that has it set up; Slack hears about failures."""
    t = terminal_of(request)
    results = await t.logins.login_all()
    failed = {b: r["detail"] for b, r in results.items() if r["ok"] is False}
    if failed:
        t.notifier.send("🔑 *Broker login failed*\n" + "\n".join(f"  {b}: {d}" for b, d in failed.items())
                        + "\n  Log in from the Workspace: Data → Broker logins.", channel="jobs")
    return {"results": results}


# ------------------------------------------------------------------ live orders
def order_call(fn, *args, **kwargs):
    from .feed.base import BrokerError
    from .orders import OrderRefused

    try:
        return fn(*args, **kwargs)
    except OrderRefused as e:
        raise HTTPException(409, str(e)) from None
    except BrokerError as e:
        raise HTTPException(502, str(e)) from None


@router.get("/orders")
def list_orders(request: Request, days: int = Query(1, ge=1, le=90)):
    """Live orders from our ledger (only orders this app sent): today, or the last `days` days."""
    return {"orders": terminal_of(request).orders.today(days)}


@router.get("/live-positions")
def live_positions(request: Request):
    """What this app holds through its own filled orders (not positions made elsewhere), with live P&L."""
    t = terminal_of(request)
    def mark(symbol: str):
        q = t.quotes.get(symbol)
        return q.price if q else None
    return {"positions": t.orders.positions(mark)}


@router.post("/orders")
async def place_order(request: Request, body: dict = Body(...)):
    """Send a live order after every check in orders.OrderService. Needs {"confirm": true}."""
    if body.get("confirm") is not True:
        raise HTTPException(400, "a live order needs confirm: true")
    try:
        quantity = int(body.get("quantity") or 0)
        price = float(body["price"]) if body.get("price") not in (None, "") else None
        trigger = float(body["trigger_price"]) if body.get("trigger_price") not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "quantity and prices must be numbers") from None
    t = terminal_of(request)
    row = await asyncio.to_thread(order_call, t.orders.place, str(body.get("account") or "flattrade"), symbol_param(body.get("symbol")),
                                  str(body.get("side") or ""), quantity, str(body.get("product") or ""),
                                  str(body.get("price_type") or "LMT"), price, "ticket", trigger)
    await t.refresh_background_subscriptions()            # live P&L for what we now hold
    return row


@router.post("/orders/{order_id}/modify")
async def modify_order(request: Request, order_id: str, body: dict = Body(...)):
    """Move an open stop-loss: {"trigger_price": ..., "price"?: ...} (a trailing step)."""
    try:
        trigger = float(body["trigger_price"])
        price = float(body["price"]) if body.get("price") not in (None, "") else None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "trigger_price (and price) must be numbers") from None
    return await asyncio.to_thread(order_call, terminal_of(request).orders.modify_stop, order_id, trigger, price)


@router.post("/orders/{order_id}/cancel")
async def cancel_order(request: Request, order_id: str):
    return await asyncio.to_thread(order_call, terminal_of(request).orders.cancel, order_id)


@router.get("/approvals")
def list_approvals(request: Request, pending: bool = False):
    """Live rule entries waiting for (or decided by) a person."""
    return {"approvals": terminal_of(request).mirror.approvals(pending_only=pending)}


@router.post("/approvals/{approval_id}/{decision}")
async def decide_approval(request: Request, approval_id: str, decision: str):
    """approve: send the live entry now (all order checks apply) · reject: no order."""
    if decision not in ("approve", "reject"):
        raise HTTPException(404, "decision must be approve or reject")
    t = terminal_of(request)
    row = await asyncio.to_thread(order_call, t.mirror.decide, approval_id, decision == "approve")
    await t.refresh_background_subscriptions()
    return row


@router.get("/live-settings")
def live_settings(request: Request):
    s = terminal_of(request).orders.settings
    return {"kill_switch": s.killed(), "accounts": {a: s.get(a) for a in ("flattrade",)}}


@router.put("/live-settings/{account}")
def set_live_settings(request: Request, account: str, body: dict = Body(...)):
    from .orders import OrderRefused

    if account not in ("flattrade",):
        raise HTTPException(404, "unknown account")
    try:
        return terminal_of(request).orders.settings.set(account, live_enabled=body.get("live_enabled"),
                                                        max_order_value=body.get("max_order_value"), max_orders_per_day=body.get("max_orders_per_day"),
                                                        require_approval=body.get("require_approval"), approval_minutes=body.get("approval_minutes"))
    except OrderRefused as e:
        raise bad_request(e) from None


@router.post("/kill-switch")
def kill_switch(request: Request, body: dict = Body(...)):
    """{"on": true} stops all live orders at once; {"on": false} releases it."""
    t = terminal_of(request)
    t.orders.settings.set("*", live_enabled=not bool(body.get("on")))
    t.notifier.send("🛑 *Kill switch ON*: no live orders" if body.get("on") else "🟢 Kill switch released", channel="orders")
    return {"kill_switch": t.orders.settings.killed()}


# ------------------------------------------------------------- account (read-only)
def broker_of(request: Request):
    b = terminal_of(request).broker
    if b is None:
        raise HTTPException(503, "account data needs the live broker (TERMINAL_MODE=flattrade)")
    return b


def broker_call(fn, *args, **kwargs):
    from .feed.base import BrokerError

    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        raise bad_request(e) from None
    except BrokerError as e:
        raise HTTPException(502, str(e)) from None


@router.get("/account/funds")
def account_funds(request: Request):
    return broker_call(broker_of(request).funds)


@router.get("/account/products")
def account_products(request: Request):
    return broker_call(broker_of(request).products)


@router.get("/quote")
def quote(request: Request, symbol: str):
    return broker_call(broker_of(request).quote, symbol_param(symbol))


@router.post("/order-preview")
def order_preview(request: Request, body: dict = Body(...)):
    """What an order would cost (contract value, margin, funds left). Calculates only; never sends an order."""
    try:
        quantity = int(body.get("quantity") or 0)
        price = float(body["price"]) if body.get("price") not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "quantity and price must be numbers") from None
    return broker_call(
        broker_of(request).preview, symbol_param(body.get("symbol")), str(body.get("side") or ""), quantity,
        body.get("product") or None, str(body.get("price_type") or "LMT"), price,
    )


@router.post("/instruments/refresh")
async def refresh_instruments(request: Request):
    """Reload the MCX futures list from Flattrade's scrip master (a daily job calls this)."""
    try:
        return await asyncio.to_thread(instruments.refresh, terminal_of(request).db)
    except Exception as e:  # noqa: BLE001 - a download failure keeps the current list; the job reports it
        raise HTTPException(502, f"instrument refresh failed: {e}") from None


# --------------------------------------------------------------------- candles
@router.get("/candles")
async def candles(request: Request, symbol: str, timeframe: str = "1H", limit: int = Query(500, ge=1, le=5000)):
    t = terminal_of(request)
    symbol = symbol_param(symbol)
    try:
        validate_timeframe(timeframe)
    except ValueError as e:
        raise bad_request(e) from None
    if t.backfill_due(symbol):
        await t.backfill_now(symbol)
    bars = t.candles.candles(symbol, timeframe, limit)
    forming = bool(bars) and not is_complete(bars[-1].time, timeframe, t.clock(), exchange_of(symbol))
    return {"symbol": symbol, "timeframe": timeframe, "forming": forming, "candles": [b.to_json() for b in bars]}


# ------------------------------------------------------------------ watchlists
@router.get("/watchlists")
def list_watchlists(request: Request):
    return {"watchlists": terminal_of(request).watchlists.list()}


@router.post("/watchlists", status_code=201)
async def create_watchlist(request: Request, body: dict = Body(...)):
    t = terminal_of(request)
    try:
        wl = t.watchlists.create(body.get("name"), body.get("symbols") or [])
    except WatchlistError as e:
        raise bad_request(e) from None
    return wl


@router.patch("/watchlists/{watchlist_id}")
def rename_watchlist(request: Request, watchlist_id: str, body: dict = Body(...)):
    t = terminal_of(request)
    try:
        ok = t.watchlists.rename(watchlist_id, body.get("name"))
    except WatchlistError as e:
        raise bad_request(e) from None
    if not ok:
        raise HTTPException(404, "watchlist not found")
    return t.watchlists.get(watchlist_id)


@router.delete("/watchlists/{watchlist_id}", status_code=204)
def delete_watchlist(request: Request, watchlist_id: str):
    if not terminal_of(request).watchlists.delete(watchlist_id):
        raise HTTPException(404, "watchlist not found")


@router.post("/watchlists/{watchlist_id}/symbols")
def add_to_watchlist(request: Request, watchlist_id: str, body: dict = Body(...)):
    t = terminal_of(request)
    try:
        added = t.watchlists.add(watchlist_id, body.get("symbols") or [])
    except WatchlistError as e:
        raise (HTTPException(404, str(e)) if "not found" in str(e) else bad_request(e)) from None
    return {"added": added, "watchlist": t.watchlists.get(watchlist_id)}


@router.delete("/watchlists/{watchlist_id}/symbols/{symbol}")
def remove_from_watchlist(request: Request, watchlist_id: str, symbol: str):
    t = terminal_of(request)
    if not t.watchlists.remove(watchlist_id, symbol_param(symbol)):
        raise HTTPException(404, "symbol is not in that watchlist")
    return t.watchlists.get(watchlist_id)


@router.put("/watchlists/{watchlist_id}/order")
def reorder_watchlist(request: Request, watchlist_id: str, body: dict = Body(...)):
    t = terminal_of(request)
    try:
        t.watchlists.reorder(watchlist_id, body.get("symbols") or [])
    except WatchlistError as e:
        raise HTTPException(404, str(e)) from None
    return t.watchlists.get(watchlist_id)


# -------------------------------------------------------------------- drawings
def _drawing_from(body: dict, drawing_id: Optional[str] = None, symbol: Optional[str] = None) -> Drawing:
    d = Drawing(
        symbol=symbol or symbol_param(body.get("symbol", "")),
        kind=str(body.get("kind", "")),
        points=body.get("points") or [],
        style=body.get("style") or {},
        label=str(body.get("label") or "")[:120],
    )
    if drawing_id:
        d.id = drawing_id
    return validate_drawing(d)


@router.get("/drawings")
def list_drawings(request: Request, symbol: Optional[str] = None):
    return {"drawings": [d.to_json() for d in terminal_of(request).drawings.list(symbol_param(symbol) if symbol else None)]}


@router.post("/drawings", status_code=201)
def create_drawing(request: Request, body: dict = Body(...)):
    t = terminal_of(request)
    try:
        return t.drawings.save(_drawing_from(body)).to_json()
    except InvalidDrawing as e:
        raise bad_request(e) from None


@router.put("/drawings/{drawing_id}")
def update_drawing(request: Request, drawing_id: str, body: dict = Body(...)):
    t = terminal_of(request)
    existing = t.drawings.get(drawing_id)
    if existing is None:
        raise HTTPException(404, "drawing not found")
    try:
        merged = {**existing.to_json(), **{k: v for k, v in body.items() if k in ("kind", "points", "style", "label")}}
        return t.drawings.save(_drawing_from(merged, drawing_id, existing.symbol)).to_json()
    except InvalidDrawing as e:
        raise bad_request(e) from None


@router.delete("/drawings/{drawing_id}", status_code=204)
def delete_drawing(request: Request, drawing_id: str):
    t = terminal_of(request)
    existing = t.drawings.get(drawing_id)
    if existing is None:
        raise HTTPException(404, "drawing not found")
    users = [r for r in t.rules.list(symbol=existing.symbol) if drawing_id in referenced_ids(r)]
    if users:
        names = ", ".join(r.name or r.id for r in users)
        raise HTTPException(409, f"this line is used by rule(s): {names}. Delete or edit the rule first.")
    t.drawings.delete(drawing_id)


# ----------------------------------------------------------------------- rules
def _check_drawings_exist(t: Terminal, rule) -> None:
    known = {d.id: d for d in t.drawings.list(rule.symbol)}
    missing = referenced_ids(rule) - known.keys()
    if missing:
        raise HTTPException(400, f"unknown drawing id(s) for {rule.symbol}: {', '.join(sorted(missing))}")
    for drawing_id, edge in referenced_refs_of(rule):
        kind = known[drawing_id].kind
        if kind == "text":
            raise HTTPException(400, "a text note has no price; a rule cannot use it")
        if kind == "rectangle" and edge is None:
            raise HTTPException(400, "pick the top or bottom edge of the rectangle")


@router.get("/rules")
def list_rules(request: Request, symbol: Optional[str] = None):
    t = terminal_of(request)
    return {"rules": [r.to_json() for r in t.rules.list(symbol=symbol_param(symbol) if symbol else None)]}


@router.post("/rules", status_code=201)
async def create_rule(request: Request, body: dict = Body(...)):
    t = terminal_of(request)
    try:
        rule = rule_from_payload({**body, "symbol": symbol_param(body.get("symbol", ""))})
    except InvalidRule as e:
        raise bad_request(e) from None
    _check_drawings_exist(t, rule)
    t.rules.save(rule)
    await t.refresh_background_subscriptions()
    return rule.to_json()


@router.put("/rules/{rule_id}")
async def update_rule(request: Request, rule_id: str, body: dict = Body(...)):
    t = terminal_of(request)
    if t.rules.get(rule_id) is None:
        raise HTTPException(404, "rule not found")
    try:
        rule = rule_from_payload({**body, "symbol": symbol_param(body.get("symbol", ""))}, rule_id)
    except InvalidRule as e:
        raise bad_request(e) from None
    _check_drawings_exist(t, rule)
    t.rules.save(rule)
    await t.refresh_background_subscriptions()
    return rule.to_json()


@router.patch("/rules/{rule_id}/enabled")
async def set_rule_enabled(request: Request, rule_id: str, body: dict = Body(...)):
    t = terminal_of(request)
    if not t.rules.set_enabled(rule_id, bool(body.get("enabled"))):
        raise HTTPException(404, "rule not found")
    await t.refresh_background_subscriptions()
    return t.rules.get(rule_id).to_json()  # type: ignore[union-attr]


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(request: Request, rule_id: str):
    t = terminal_of(request)
    if not t.rules.delete(rule_id):
        raise HTTPException(404, "rule not found")
    await t.refresh_background_subscriptions()


# ------------------------------------------------------------------ strategies (rule template x watchlist)
@router.get("/strategies")
def list_strategies(request: Request):
    return {"strategies": StrategyRepo(terminal_of(request).db).list()}


@router.post("/strategies", status_code=201)
def save_strategy(request: Request, body: dict = Body(...)):
    try:
        return StrategyRepo(terminal_of(request).db).save(body)
    except StrategyError as e:
        raise bad_request(e) from None


@router.delete("/strategies/{strategy_id}", status_code=204)
def delete_strategy(request: Request, strategy_id: str):
    if not StrategyRepo(terminal_of(request).db).delete(strategy_id):
        raise HTTPException(404, "strategy not found")


@router.post("/strategies/apply")
async def apply_strategies(request: Request):
    """Bring every strategy's per-symbol rules in line with its watchlist (also run after each screener refresh)."""
    t = terminal_of(request)
    out = await asyncio.to_thread(StrategyRepo(t.db).apply_all, t.rules, t.watchlists)
    await t.refresh_background_subscriptions()
    return {"results": out}


@router.get("/signals")
def list_signals(request: Request, symbol: Optional[str] = None, limit: int = Query(100, ge=1, le=1000)):
    t = terminal_of(request)
    return {"signals": [s.to_json() for s in t.signals.list(symbol_param(symbol) if symbol else None, limit)]}


# ------------------------------------------------------------------- positions
def _mark(t: Terminal, symbol: str) -> Optional[float]:
    q = t.quotes.get(symbol)
    return q.price if q else None


@router.get("/trade-lines")
def trade_lines(request: Request, symbol: str, days: int = Query(7, ge=1, le=60)):
    """A symbol's open and recent paper positions with the stop's path, target and entry, for drawing on the chart."""
    return {"positions": terminal_of(request).book.trade_lines(symbol_param(symbol), days)}


@router.get("/positions")
def positions(request: Request, symbol: Optional[str] = None, history: int = Query(50, ge=0, le=500)):
    t = terminal_of(request)
    sym = symbol_param(symbol) if symbol else None
    open_ = [p.to_json(mark=_mark(t, p.symbol)) for p in t.book.open_positions(sym)]
    closed = [p.to_json() for p in t.book.history(sym, history)] if history else []
    return {"open": open_, "closed": closed}


@router.post("/positions/{position_id}/close")
async def close_position(request: Request, position_id: str):
    t = terminal_of(request)
    position = next((p for p in t.book.open_positions() if p.id == position_id), None)
    if position is None:
        raise HTTPException(404, "no open position with that id")
    price = _mark(t, position.symbol)
    if price is None:
        raise HTTPException(409, "no live price for this symbol yet, so the position cannot be closed at market")
    closed = t.book.close_manually(position_id, price, t.clock())
    event = {"type": "position", "position": closed.to_json()}  # type: ignore[union-attr]
    t.hub.broadcast(event)
    await t.refresh_background_subscriptions()
    return event["position"]


# ------------------------------------------------------------- financial data
@router.get("/financials")
def financials(request: Request, symbol: str):
    return fin.get_financials(terminal_of(request).db, symbol_param(symbol))


@router.get("/results/recent")
def recent_results(request: Request, days: int = Query(7, ge=1, le=60), limit: int = Query(100, ge=1, le=500)):
    return {"results": fin.recent_results(terminal_of(request).db, days, limit)}


# --------------------------------------------------------------------- sectors
@router.get("/sectors")
def list_sectors(request: Request):
    return {"groups": sectors.list_groups(terminal_of(request).db)}


@router.get("/sectors/compare")
def compare_sector(request: Request, group: Optional[str] = Query(None, max_length=80), sector: Optional[str] = Query(None, max_length=120),
                   limit: int = Query(sectors.DEFAULT_LIMIT, ge=1, le=sectors.MAX_LIMIT)):
    if not group and not sector:
        raise HTTPException(400, "give a group or a sector")
    try:
        return sectors.compare(terminal_of(request).db, group=group, sector=sector, limit=limit)
    except sectors.UnknownSector:
        raise HTTPException(404, f"no symbols in {sector or group!r}") from None


# ------------------------------------------------------------------- backtests
@router.post("/backtests", status_code=202)
def start_backtest(request: Request, body: dict = Body(...)):
    try:
        t = terminal_of(request)
        return {"id": BacktestStore(t.db).start(body, on_done=t.notifier.backtest_done)}
    except BacktestError as e:
        raise bad_request(e) from None


@router.get("/backtests")
def list_backtests(request: Request, limit: int = Query(50, ge=1, le=200)):
    return {"backtests": BacktestStore(terminal_of(request).db).list(limit)}


@router.get("/backtests/{backtest_id}")
def get_backtest(request: Request, backtest_id: str):
    found = BacktestStore(terminal_of(request).db).get(backtest_id)
    if found is None:
        raise HTTPException(404, "backtest not found")
    return found


@router.delete("/backtests/{backtest_id}", status_code=204)
def delete_backtest(request: Request, backtest_id: str):
    if not BacktestStore(terminal_of(request).db).delete(backtest_id):
        raise HTTPException(404, "backtest not found")


# -------------------------------------------------------------------- screener
@router.get("/screener/columns")
def screener_columns():
    return {"columns": [{"name": k, "label": v, "unit": screener.UNITS.get(k, "num"), "short": screener.SHORT.get(k, v), "group": screener.group_of(k),
                         "technical": screener.group_of(k) in screener.TECHNICAL_GROUPS} for k, v in screener.FILTERABLE.items()],
            "groups": list(screener.FIELD_GROUPS) + ["Other"], "operators": list(screener.OPS), "quick": screener.QUICK}


@router.get("/screener/saved")
def saved_screens(request: Request):
    return {"screens": screener.list_saved(terminal_of(request).db)}


@router.post("/screener/saved")
def save_screen(request: Request, body: dict = Body(...)):
    try:
        return screener.save_screen(terminal_of(request).db, str(body.get("name") or ""), body.get("filters") or [],
                                    str(body.get("sort") or "volume_ratio"), bool(body.get("descending", True)))
    except screener.ScreenError as e:
        raise bad_request(e) from None


@router.delete("/screener/saved/{screen_id}")
def delete_saved_screen(request: Request, screen_id: str):
    if not screener.delete_saved(terminal_of(request).db, screen_id):
        raise HTTPException(404, "no such saved screen")
    return {"deleted": screen_id}


@router.get("/screener/presets")
def screener_presets():
    return {"presets": [{"name": k, **v} for k, v in screener.PRESETS.items()]}


@router.get("/screener/preset/{name}")
def screener_preset(request: Request, name: str, limit: int = Query(100, ge=1, le=1000)):
    try:
        return screener.run_preset(terminal_of(request).db, name, limit)
    except screener.ScreenError as e:
        raise HTTPException(404 if "unknown preset" in str(e) else 400, str(e)) from None


@router.post("/screener/run")
def screener_run(request: Request, body: dict = Body(default_factory=dict)):
    try:
        return screener.run_screen(
            terminal_of(request).db,
            body.get("filters") or [],
            body.get("sort", "volume_ratio"),
            bool(body.get("descending", True)),
            int(body.get("limit", 100)),
        )
    except (screener.ScreenError, ValueError, TypeError) as e:
        raise bad_request(e) from None


@router.post("/screener/scan")
def screener_scan(request: Request):
    """Run the daily scan now (normally the scheduler does this once a day after the OHLCV job)."""
    return screener.run_scan(terminal_of(request).db)


# ------------------------------------------------------------------- websocket
@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    t: Optional[Terminal] = getattr(ws.app.state, "terminal", None)
    if t is None:
        await ws.close(code=1013)
        return
    await ws.accept()
    client = t.hub.connect()
    await ws.send_json({"type": "hello", **t.status()})
    pump = asyncio.create_task(t.hub.pump(client, ws.send_json))
    try:
        while True:
            try:
                msg = json.loads(await ws.receive_text())
                kind = msg.get("type")
                if kind == "ping":
                    await ws.send_json({"type": "pong"})
                elif kind in ("subscribe", "unsubscribe"):
                    wanted, bad = [], []
                    for raw in msg.get("symbols") or []:
                        try:
                            wanted.append(normalise_symbol(raw))
                        except WatchlistError:
                            bad.append(raw)
                    if kind == "subscribe":
                        await t.hub.subscribe(client, wanted)
                    else:
                        await t.hub.unsubscribe(client, wanted)
                    if bad:
                        await ws.send_json({"type": "error", "message": f"ignored invalid symbols: {bad[:5]}"})
                else:
                    await ws.send_json({"type": "error", "message": f"unknown message type {kind!r}"})
            except (json.JSONDecodeError, AttributeError, TypeError):
                await ws.send_json({"type": "error", "message": "messages must be JSON objects like {\"type\":\"subscribe\",\"symbols\":[...]}"})
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        await t.hub.disconnect(client)


# ------------------------------------------------------------- app integration
def mount(app: FastAPI) -> None:
    app.include_router(router)


def _first_instruments(db) -> None:
    import logging

    try:
        logging.getLogger(__name__).info("MCX instruments loaded: %s", instruments.refresh(db))
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("MCX instruments not loaded: %s", e)


@asynccontextmanager
async def terminal_lifespan(app: FastAPI, terminal: Optional[Terminal] = None):
    """Start the terminal with the app and stop it on shutdown. If it cannot start
    (e.g. no DATABASE_URL) the rest of the service keeps running and /terminal answers 503."""
    started: Optional[Terminal] = None
    try:
        t = terminal
        if t is None:
            db = get_db()
            db.ensure_schema()
            t = build_terminal(db)
        app.state.terminal = t
        BacktestStore(t.db).fail_stale()
        await t.start()
        started = t
        if terminal is None and not t.db.scalar("SELECT EXISTS (SELECT 1 FROM terminal_instruments)"):
            asyncio.get_running_loop().run_in_executor(None, _first_instruments, t.db)   # first start: make MCX searchable
    except Exception as e:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("terminal not started: %s", e)
        app.state.terminal = None
    try:
        yield
    finally:
        if started is not None:
            await started.stop()


# ------------------------------------------------- strategy-screener -> watchlist
def screener_client_of(request: Request):
    """The HTTP client for the strategy-screener service (tests put a fake on app.state)."""
    client = getattr(request.app.state, "screener_client", None)
    if client is None:
        client = request.app.state.screener_client = sync.ScreenerClient()
    return client


def sync_failure(e: sync.SyncError) -> HTTPException:
    return HTTPException(502 if isinstance(e, sync.ScreenerUnavailable) else 400, str(e))


def sync_settings(body: dict) -> dict:
    keys = ("tolerance_pct", "max_failing", "max_symbols", "per_cap", "filters")
    return {k: body[k] for k in keys if k in body}


def strategy_id_of(body: dict) -> int:
    try:
        return int(body.get("strategy_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "strategy_id is required") from None


@router.get("/screener-sync/strategies")
def sync_strategies(request: Request):
    """The strategies defined in the strategy-screener, for the 'sync to watchlist' picker."""
    terminal_of(request)
    try:
        return {"strategies": sync.list_strategies(screener_client_of(request))}
    except sync.SyncError as e:
        raise sync_failure(e) from None


@router.get("/screener-sync/links")
def sync_links(request: Request):
    t = terminal_of(request)
    return {"links": sync.list_links(t.db, t.watchlists.user)}


@router.post("/screener-sync/preview")
def sync_preview(request: Request, body: dict = Body(...)):
    """What WOULD be on the list - matched stocks and near-misses - without changing anything."""
    t = terminal_of(request)
    try:
        plan = sync.build_plan(screener_client_of(request), strategy_id_of(body), financials=lambda s: fin.get_financials(t.db, s), **sync_settings(body))
    except sync.SyncError as e:
        raise sync_failure(e) from None
    return plan.to_json()


@router.post("/screener-sync/apply")
async def sync_apply(request: Request, body: dict = Body(...)):
    """Create or refresh the managed watchlist for a strategy."""
    t = terminal_of(request)
    try:
        result = await asyncio.to_thread(
            sync.sync_strategy, t.db, t.watchlists, screener_client_of(request), strategy_id_of(body), body.get("name") or None, **sync_settings(body)
        )
    except sync.SyncError as e:
        raise sync_failure(e) from None
    except WatchlistError as e:
        raise bad_request(e) from None
    await t.refresh_background_subscriptions()
    t.notifier.sync_result(result)
    return result


@router.post("/screener-sync/refresh")
async def sync_refresh(request: Request):
    """Re-run every linked list with the settings it was created with (what the daily job does)."""
    t = terminal_of(request)
    results = await asyncio.to_thread(sync.re_sync_all, t.db, t.watchlists, screener_client_of(request))
    applied = await asyncio.to_thread(StrategyRepo(t.db).apply_all, t.rules, t.watchlists)   # rules follow the fresh lists, same run
    await t.refresh_background_subscriptions()
    t.notifier.sync_refresh(results)
    return {"results": results, "strategies": applied}


@router.post("/notify/data-digest")
def notify_data_digest(request: Request):
    """Post the daily data digest to Slack (a scheduled job calls this) and return it."""
    t = terminal_of(request)
    digest = data_digest(t.db)
    t.notifier.send(digest_text(digest), channel="data")
    return digest


@router.post("/notify/broker-tokens")
def notify_broker_tokens(request: Request):
    t = terminal_of(request)
    from broker_auth.check import alert_text, check_all

    results = check_all(t.logins.store.env())        # stored logins count, not only the environment
    text = alert_text(results)
    if text:
        t.notifier.send(text, channel="jobs")
    return results


@router.get("/data/status")
def data_status(request: Request):
    """Data health from the market tables, plus the running scrape jobs when the strategy-screener answers."""
    t = terminal_of(request)
    out: dict[str, Any] = {"status": datajobs.data_status(t.db), "catalog": datajobs.catalog(), "jobs": None, "jobs_error": None}
    try:
        out["jobs"] = datajobs.running_jobs(screener_client_of(request))
    except sync.SyncError as e:
        out["jobs_error"] = str(e)
    return out


@router.post("/data/run", status_code=202)
def data_run(request: Request, body: dict = Body(...)):
    terminal_of(request)
    try:
        return datajobs.run_job(screener_client_of(request), str(body.get("job", "")), body)
    except sync.SyncError as e:
        raise sync_failure(e) from None


@router.delete("/screener-sync/links/{watchlist_id}", status_code=204)
def sync_unlink(request: Request, watchlist_id: str):
    t = terminal_of(request)
    if not sync.unlink(t.db, watchlist_id, t.watchlists.user):
        raise HTTPException(404, "that watchlist is not synced from a screener strategy")


# ------------------------------------------------------------------ rule watch
def _rule_watch(t: Terminal) -> list[dict]:
    from .candles import completed
    from .conditions import EvalContext, explain
    from .drawings import DrawingRepo
    from .engine import lookback_bars
    from .timeframes import bucket_end, bucket_start, in_session

    now = t.clock()
    drawings_by_symbol: dict[str, dict] = {}
    bars_memo: dict[tuple, list] = {}
    open_by_rule: dict[str, list] = {}
    for p in t.book.open_positions():
        rid = t.db.scalar("SELECT rule_id FROM terminal_signals WHERE id = :s", s=p.signal_id) if p.signal_id else None
        if rid:
            open_by_rule.setdefault(rid, []).append(p)
    out = []
    for rule in t.rules.list(enabled_only=True) + [r for r in t.rules.list() if not r.enabled and r.id in open_by_rule]:
        ex = exchange_of(rule.symbol)
        if rule.symbol not in drawings_by_symbol:
            drawings_by_symbol[rule.symbol] = {d.id: d for d in DrawingRepo(t.db).list(rule.symbol)}
        q = t.quotes.get(rule.symbol)
        live = q.price if q else None

        def ctx_for(tf: str):
            need = lookback_bars(tf, rule.condition, rule.stop, rule.trail, rule.exit_condition, rule.exit_condition_2, rule.entry_score_indicator)
            key = (rule.symbol, tf, need)
            if key not in bars_memo:        # eight strategies share a symbol: read its candles once per request
                bars_memo[key] = completed(t.candles.candles(rule.symbol, tf, need), tf, now, rule.symbol)
            bars = bars_memo[key]
            return EvalContext(bars, tf, drawings_by_symbol[rule.symbol], ex), bars

        try:
            ctx, bars = ctx_for(rule.timeframe)
            entry = explain(rule.condition, ctx, live)
        except Exception as e:  # noqa: BLE001 - one broken rule must not hide the others
            entry, bars = {"type": "error", "label": str(e), "met": False, "distance_pct": None}, []
        session = in_session(now, ex)
        plan = _plan(rule, ctx if bars else None)
        row = {
            "plan": plan,
            "rule": rule.to_json(), "live": live, "session_open": session,
            "last_candle": int(bars[-1].time.timestamp()) if bars else None,
            "next_check": int(bucket_end(bucket_start(now, rule.timeframe, ex), rule.timeframe, ex).timestamp()) if session else None,
            "entry": entry, "positions": [],
        }
        for p in open_by_rule.get(rule.id, []):
            exits = []
            for level, cond, tf in ((1, rule.exit_condition, rule.exit_timeframe), (2, rule.exit_condition_2, rule.exit_timeframe_2 or rule.exit_timeframe)):
                if cond and not (level == 1 and p.exit_level == 2):        # level 1 until it has fired
                    try:
                        exits.append({"level": level, "timeframe": tf or rule.timeframe, **explain(cond, ctx_for(tf or rule.timeframe)[0], live)})
                    except Exception as e:  # noqa: BLE001
                        exits.append({"level": level, "type": "error", "label": str(e), "met": False})
            link = t.mirror.links.get(p.id)
            row["positions"].append({**p.to_json(live), "exits": exits,
                                     "live_link": {k: link[k] for k in ("state", "note", "last_stop")} if link else None})
        out.append(row)
    def closeness(r: dict) -> tuple:
        d = r["entry"].get("distance_pct")
        return (not r["positions"], d is None, d if d is not None else 0)
    return sorted(out, key=closeness)


def _describe_stop(spec: dict, side: str) -> str:
    edge = "high" if side == "SELL" else "low"
    k = spec["type"]
    if k == "candle":
        return f"signal candle {edge}" + (f" {'+' if side == 'SELL' else '−'} {spec.get('buffer_pct', 0):g}%" if spec.get("buffer_pct") else "")
    if k == "pct":
        return f"{spec['value']:g}% {'above' if side == 'SELL' else 'below'} entry"
    if k == "atr":
        return f"{spec['mult']:g} × ATR({spec['period']}) {'above' if side == 'SELL' else 'below'} entry"
    if k == "fixed":
        return f"fixed at ₹{spec['price']:,.2f}"
    return "the drawn line"


def _describe_trail(spec: Optional[dict], side: str, tf: str) -> Optional[str]:
    if not spec:
        return None
    every = f"after each completed {spec.get('timeframe') or tf} candle"
    edge = "high" if side == "SELL" else "low"
    k = spec["type"]
    what = {"candle": f"the {edge} of the last {spec.get('bars', 1)} candle(s)" + (f" ± {spec['buffer_pct']:g}%" if spec.get("buffer_pct") else ""),
            "atr": f"{spec.get('mult', 2):g} × ATR({spec.get('period', 14)}) from the close",
            "pct": f"{spec.get('value', 0):g}% from the close",
            "indicator": f"the {spec.get('name', '').upper()}({','.join(f'{v:g}' for v in (spec.get('params') or {}).values())}) line",
            "line": "the drawn line"}.get(k, k)
    return f"{every}: move the stop to {what}; only ever tighter, never past the close"


def _plan(rule, ctx) -> dict:
    """What a fire on the last completed candle would do: the stop / target it would use and how the stop trails."""
    from .conditions import InvalidRisk, compute_stop, compute_target

    out = {"stop": _describe_stop(rule.stop, rule.side), "trail": _describe_trail(rule.trail, rule.side, rule.timeframe),
           "target": (f"{rule.target['ratio']:g}R" if rule.target and rule.target["type"] == "rr" else
                      f"{rule.target['value']:g}%" if rule.target and rule.target["type"] == "pct" else
                      f"₹{rule.target['price']:,.2f}" if rule.target else None),
           "entry_if_now": None, "stop_if_now": None, "target_if_now": None}
    if ctx is None or not ctx.candles:
        return out
    entry = float(ctx.close[-1])
    try:
        stop = compute_stop(rule.stop, rule.side, entry, ctx)
        out.update(entry_if_now=entry, stop_if_now=round(stop, 4),
                   target_if_now=round(compute_target(rule.target, rule.side, entry, stop), 4) if rule.target else None)
    except (InvalidRisk, Exception):  # noqa: BLE001 - a plan that cannot be computed just shows the description
        pass
    return out


@router.get("/rules/watch")
async def rules_watch(request: Request):
    """Every enabled rule (and any rule with an open position): its conditions evaluated now, how far each part is
    from triggering, when it is checked next, and its open position with the exits that would close it."""
    return {"rules": await asyncio.to_thread(_rule_watch, terminal_of(request))}
