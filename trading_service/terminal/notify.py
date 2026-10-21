"""Slack messages for what happens in the terminal.

* orders channel: every paper entry, partial exit and exit (heard from the hub: it sees every `signal` and `position`
  event, whichever path made it: a candle close, a stop touched by a tick, a click on "Close")
* jobs channel: a finished backtest, broker-login alerts
* data channel: a screener -> watchlist sync (what was added / removed), and the daily data digest: stocks added to /
  removed from the symbol list, and whether each data feed is fresh

Deliberately silent (too frequent to read, and they would bury the trades): skipped signals, trail-stop moves.

Sending never blocks the tick path: messages go through one background worker, in order. Without a Slack webhook
(SLACK_WEBHOOK_URL) everything is a quiet no-op. `channel` picks the jobs / data webhook (SLACK_JOBS_WEBHOOK_URL,
SLACK_DATA_WEBHOOK_URL; unset = the shared one). For orders, `account` picks the webhook (SLACK_WEBHOOK_URL_<ACCOUNT>), which
is where several broker accounts will plug in; today everything is the "default" account.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from .db import Database

log = logging.getLogger(__name__)

MAX_REMEMBERED_SIGNALS = 200
LIST_LIMIT = 15


def short(symbol: str) -> str:
    """'NSE:SBIN-EQ' -> 'SBIN'."""
    name = symbol.split(":", 1)[-1]
    return name[:-3] if name.endswith("-EQ") else name


def px(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:,.2f}"


def names(symbols: list[str]) -> str:
    shown = ", ".join(short(s) for s in symbols[:LIST_LIMIT])
    return shown + (f" and {len(symbols) - LIST_LIMIT} more" if len(symbols) > LIST_LIMIT else "")


class Notifier:
    def __init__(self, tag: str = "", sender: Optional[Callable[..., Any]] = None, background: Optional[bool] = None):
        """`sender(text, account=..., channel=...)`; tests pass one and get synchronous delivery."""
        if sender is None:
            from ..slack_notify import send_slack

            sender = send_slack
        self.tag = f"[{tag}] " if tag and tag not in ("flattrade", "custom") else ""
        self.sender = sender
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slack") if (background if background is not None else True) else None
        self._signals: "OrderedDict[str, dict]" = OrderedDict()
        self._partials: "OrderedDict[str, None]" = OrderedDict()   # positions whose first-level exit was announced

    # ------------------------------------------------------------------ sending
    def send(self, text: str, account: str = "default", channel: str = "orders") -> None:
        text = self.tag + text
        if self._pool is None:
            self._deliver(text, account, channel)
        else:
            self._pool.submit(self._deliver, text, account, channel)

    def _deliver(self, text: str, account: str, channel: str) -> None:
        try:
            self.sender(text, account=account, channel=channel)
        except Exception:  # noqa: BLE001 - Slack being down must never matter to trading
            log.exception("slack delivery failed")

    # ------------------------------------------------------------- hub events
    def on_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "signal":
            s = event["signal"]
            if s["status"] != "rejected":                      # skipped signals happen all day; they stay in the terminal
                self._signals[s["id"]] = s
                while len(self._signals) > MAX_REMEMBERED_SIGNALS:
                    self._signals.popitem(last=False)
        elif kind == "position":
            p = event["position"]
            if p["status"] == "closed":
                self._partials.pop(p["id"], None)
                self.send(self._exit_text(p))
            elif p.get("signal_id") in self._signals:           # the position that follows a signal is an entry; a trail move is not
                self.send(self._entry_text(p, self._signals.pop(p["signal_id"])))
            elif p.get("exit_level") == 2 and p.get("exit_price_1") is not None and p["id"] not in self._partials:
                self._partials[p["id"]] = None                  # later events for it (trail moves) say nothing
                while len(self._partials) > MAX_REMEMBERED_SIGNALS:
                    self._partials.popitem(last=False)
                self.send(self._partial_text(p))

    @staticmethod
    def _entry_text(p: dict, signal: dict) -> str:
        return (f"🟢 *Paper ENTRY* {p['side']} {p['quantity']} {short(p['symbol'])} @ {px(p['entry_price'])} "
                f"· stop {px(p['stop'])} · target {px(p['target'])} · {signal['timeframe']} · {signal['reason']}")

    @staticmethod
    def _partial_text(p: dict) -> str:
        pnl = p.get("exit_pnl_1") or 0.0
        icon = "✅" if pnl >= 0 else "🔻"
        return (f"{icon} *Paper PARTIAL EXIT* (exit rule 1) {p['side']} {p['exit_qty_1']} {short(p['symbol'])} "
                f"{px(p['entry_price'])} → {px(p['exit_price_1'])} · P&L {pnl:+,.2f} · {p['quantity']} left for exit rule 2")

    @staticmethod
    def _exit_text(p: dict) -> str:
        pnl = (p["realized_pnl"] or 0.0) + (p.get("exit_pnl_1") or 0.0)          # a 2-level exit: both halves
        qty = p["quantity"] + (p.get("exit_qty_1") or 0)
        notional = qty * p["entry_price"] * (p.get("multiplier") or 1.0)
        pct = pnl / notional * 100 if notional else 0.0
        icon = "✅" if pnl >= 0 else "🔻"
        return (f"{icon} *Paper EXIT* ({str(p['exit_reason']).replace('_', ' ')}) {p['side']} {qty} {short(p['symbol'])} "
                f"{px(p['entry_price'])} → {px(p['exit_price'])} · P&L {pnl:+,.2f} ({pct:+.2f}%)")

    # --------------------------------------------------------- other messages
    def backtest_done(self, row: Optional[dict]) -> None:
        if not row:
            return
        if row["status"] != "done":
            self.send(f"⚠️ Backtest failed: {row.get('error')}", row.get("account") or "default", "jobs")
            return
        r, p = row["result"]["summary"], row["params"]
        wr = "-" if r["win_rate_pct"] is None else f"{r['win_rate_pct']}%"
        self.send(f"📊 *Backtest done* {p['start']} → {p['end']}: {r['return_pct']:+.2f}% (equity {r['final_equity']:,.0f} from {r['initial_capital']:,.0f}) "
                  f"· {r['trades']} closed trades, win rate {wr} · {r['open_positions']} still open · max drawdown {r['max_drawdown_pct']}%",
                  row.get("account") or "default", "jobs")

    def sync_result(self, res: dict) -> None:
        wl = res["watchlist"]["name"]
        head = f"🔄 *Screener sync* “{wl}”" + (" (created)" if res.get("created") else "")
        lines = [f"{head}: {res['matched']} match, {res['near']} near"]
        if res["added"]:
            lines.append(f"  added: {names(res['added'])}")
        if res["removed"]:
            lines.append(f"  removed: {names(res['removed'])}")
        if res.get("kept_for_trading"):
            lines.append(f"  kept (open position or rule): {names(res['kept_for_trading'])}")
        if len(lines) == 1:
            lines[0] += " - no change"
        self.send("\n".join(lines), channel="data")

    def sync_refresh(self, results: list[dict]) -> None:
        lines = ["🔄 *Screener sync (daily)*"]
        for r in results:
            if r["ok"]:
                lines.append(f"  strategy {r['strategy_id']}: +{r['added']} −{r['removed']} · {r['matched']} match, {r['near']} near")
            else:
                lines.append(f"  strategy {r['strategy_id']}: ⚠️ {r['error']}")
        self.send("\n".join(lines if results else [lines[0] + ": no linked lists"]), channel="data")


# ------------------------------------------------------------------ data digest
def data_digest(db: Database) -> dict:
    """What the data feeds look like now, and which symbols appeared or disappeared since the last digest.
    The first run only records the list (it cannot know what changed)."""
    out: dict[str, Any] = {}
    if db.table_exists("symbols"):
        now = {r["tradingsymbol"] for r in db.rows("SELECT tradingsymbol FROM symbols")}
        before = {r["symbol"] for r in db.rows("SELECT symbol FROM terminal_symbol_snapshot")}
        first = not before
        out["symbols"] = {"count": len(now), "added": None if first else sorted(now - before), "removed": None if first else sorted(before - now)}
        with db.tx() as conn:
            from sqlalchemy import text

            conn.execute(text("DELETE FROM terminal_symbol_snapshot"))
            if now:
                conn.execute(text("INSERT INTO terminal_symbol_snapshot (symbol) VALUES (:s)"), [{"s": s} for s in now])
    if db.table_exists("ohlcv_1d"):
        latest = db.scalar("SELECT max(date) FROM ohlcv_1d")
        out["ohlcv_1d"] = {"latest": latest.isoformat() if latest else None,
                           "symbols_on_latest": db.scalar("SELECT count(*) FROM ohlcv_1d WHERE date = :d", d=latest) if latest else 0}
    if db.table_exists("stock_financials"):
        out["financials"] = {"companies": db.scalar("SELECT count(DISTINCT tradingsymbol) FROM stock_financials")}
    if db.table_exists("result_announcements"):
        out["results"] = {"latest": _iso(db.scalar("SELECT max(result_date) FROM result_announcements")),
                          "announced_last_7_days": db.scalar("SELECT count(*) FROM result_announcements WHERE result_date >= current_date - 7")}
    run = db.scalar("SELECT max(run_date) FROM terminal_screener_metrics")
    out["screener"] = {"run_date": _iso(run), "stocks": db.scalar("SELECT count(*) FROM terminal_screener_metrics WHERE run_date = :d", d=run) if run else 0}
    return out


def _iso(d: Any) -> Optional[str]:
    return d.isoformat() if d else None


def digest_text(d: dict) -> str:
    lines = ["🗂 *Daily data digest*"]
    if "symbols" in d:
        s = d["symbols"]
        lines.append(f"  symbols: {s['count']:,}" + ("  (list recorded; changes are reported from the next digest)" if s["added"] is None else
                     f"  (+{len(s['added'])} added, −{len(s['removed'])} removed)"))
        if s["added"]:
            lines.append(f"    added: {names(s['added'])}")
        if s["removed"]:
            lines.append(f"    removed: {names(s['removed'])}")
    if "ohlcv_1d" in d:
        lines.append(f"  daily candles: latest {d['ohlcv_1d']['latest']} · {d['ohlcv_1d']['symbols_on_latest']:,} symbols on that day")
    if "financials" in d:
        lines.append(f"  financials: {d['financials']['companies']:,} companies")
    if "results" in d:
        lines.append(f"  result announcements: latest {d['results']['latest']} · {d['results']['announced_last_7_days']} in the last 7 days")
    lines.append(f"  screener scan: {d['screener']['run_date'] or 'never run'} · {d['screener']['stocks']:,} stocks")
    return "\n".join(lines)
