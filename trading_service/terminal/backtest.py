"""Backtesting: replay history through the REAL rule engine.

Nothing is re-implemented here. For a date range we

1. load the candles (daily from the data-store's `ohlcv_1d`; intraday from the stored 1-minute bars),
2. create a throw-away Postgres schema holding a private copy of the terminal tables,
3. walk every candle close in time order and call `RuleEngine.on_candle_close` exactly as the live
   runtime does, with a clock that says "it is now the close of this candle",
4. between closes, sweep the 1-minute (or daily) bars for stop / target touches, which live trading
   catches tick by tick,
5. size every entry from capital and an allocation %, and report trades, open positions and an equity curve.

So entries (completed candles only), trailing stops and exit conditions (each on their own timeframe)
behave identically to live trading. The scratch schema is dropped afterwards; the real tables are never written.

Honest limits (also in the result under `notes`):
* Intraday history exists only where `terminal_ohlcv_1m` has it; a year-long run needs 1D/1W rules.
* When a candle touches both stop and target the STOP is taken (pessimistic). A gap through a stop fills at the open.
* Screens use price/technical metrics as of the day before (no look-ahead). market-cap, sector and financial-data filters use today's values.
* Positions still open at the end are marked at the last close, not squared off.
"""
from __future__ import annotations

import heapq
import logging
import math
import uuid
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import create_engine, text

from . import screener
from .candles import Candle, resample
from .db import Database, new_id
from .drawings import DrawingRepo
from .engine import RuleEngine
from .market import MinuteStore
from .paper import PaperBook, PaperPosition
from .rules import InvalidRule, Rule, RuleRepo, referenced_refs_of, rule_from_payload
from .timeframes import IST, UTC, bucket_end, exchange_of, is_intraday, session_close, session_open, validate_timeframe
from .watchlists import WatchlistRepo, normalise_symbol

log = logging.getLogger(__name__)

MAX_DAYS = 1100
MAX_TEMPLATES = 10
MAX_SYMBOLS = 500
SCREEN_HISTORY_DAYS = 600     # daily history loaded before the start for screening (200-day average + 52 weeks)
RULE_HISTORY_DAYS = 900       # ... and for the rules' own 500-bar lookback on 1D
INTRADAY_WARMUP_DAYS = 30
MAX_TRADES_RETURNED = 2000
PLACEHOLDER = "NSE:TEMPLATE-EQ"


class BacktestError(ValueError):
    pass


# ----------------------------------------------------------------- parameters
@dataclass
class Params:
    start: date
    end: date
    capital: float
    allocation_pct: float
    templates: list[Rule]
    symbols: Optional[list[str]] = None
    screen: Optional[dict] = None
    rescreen_days: int = 7
    max_positions: Optional[int] = None
    cost_pct: float = 0.0          # charged on each side of a trade, % of its value
    account: str = "default"       # the seam for several broker accounts later; nothing reads it yet

    def to_json(self) -> dict:
        return {
            "start": self.start.isoformat(), "end": self.end.isoformat(), "capital": self.capital,
            "allocation_pct": self.allocation_pct, "max_positions": self.max_positions, "cost_pct": self.cost_pct,
            "symbols": self.symbols, "screen": self.screen, "rescreen_days": self.rescreen_days, "account": self.account,
            "rules": [t.to_json() | {"symbol": None} for t in self.templates],
        }


def _num(raw: Mapping[str, Any], key: str, default: float, lo: float, hi: float, inclusive_lo: bool = True) -> float:
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v > hi or (v < lo if inclusive_lo else v <= lo):
        raise BacktestError(f"{key} must be a number {'from' if inclusive_lo else 'above'} {lo} up to {hi}")
    return float(v)


def _day(raw: Mapping[str, Any], key: str) -> date:
    try:
        return date.fromisoformat(str(raw.get(key))[:10])
    except ValueError:
        raise BacktestError(f"{key} must be a date like 2025-10-01") from None


def parse_params(db: Database, raw: Mapping[str, Any]) -> Params:
    """Validate a request body. Rules are given as payloads (no symbol needed) and/or the ids of saved rules."""
    start, end = _day(raw, "start"), _day(raw, "end")
    if end < start:
        raise BacktestError("end is before start")
    if end > datetime.now(IST).date():
        raise BacktestError("end is in the future")
    if (end - start).days > MAX_DAYS:
        raise BacktestError(f"at most {MAX_DAYS} days per backtest")

    templates: list[Rule] = []
    for payload in raw.get("rules") or []:
        try:
            templates.append(rule_from_payload({"symbol": PLACEHOLDER, "quantity": 1, **payload}))
        except InvalidRule as e:
            raise BacktestError(f"rule: {e}") from None
    for rule_id in raw.get("rule_ids") or []:
        saved = RuleRepo(db).get(str(rule_id))
        if saved is None:
            raise BacktestError(f"rule {rule_id} does not exist")
        templates.append(saved)
    if not templates:
        raise BacktestError("give at least one rule (rules or rule_ids)")
    if len(templates) > MAX_TEMPLATES:
        raise BacktestError(f"at most {MAX_TEMPLATES} rules")

    chosen = [k for k in ("symbols", "watchlist_id", "screen") if raw.get(k)]
    pinned = [t for t in templates if referenced_refs_of(t)]
    if len(chosen) != 1 and not (pinned and not chosen):
        raise BacktestError("choose the stocks with exactly one of symbols, watchlist_id or screen")
    symbols: Optional[list[str]] = None
    screen = None
    try:
        if raw.get("symbols"):
            symbols = [normalise_symbol(s) for s in raw["symbols"]]
        elif raw.get("watchlist_id"):
            wl = WatchlistRepo(db).get(str(raw["watchlist_id"]))
            if wl is None:
                raise BacktestError("watchlist not found")
            symbols = list(wl["symbols"])
        elif raw.get("screen"):
            screen = _clean_screen(raw["screen"])
            screener.run_screen(db, screen["filters"], screen["sort"], screen["descending"], 1, run_date=date(1900, 1, 1))   # rejects bad filters now
    except ValueError as e:
        raise BacktestError(str(e)) from None
    if symbols is not None and not (0 < len(symbols) <= MAX_SYMBOLS):
        raise BacktestError(f"between 1 and {MAX_SYMBOLS} symbols")
    mp = raw.get("max_positions")
    if mp is not None and (isinstance(mp, bool) or not isinstance(mp, int) or mp < 1):
        raise BacktestError("max_positions must be a whole number of at least 1")
    days = raw.get("rescreen_days", 7)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
        raise BacktestError("rescreen_days must be a whole number from 1 to 90")
    return Params(
        start=start, end=end, capital=_num(raw, "capital", 1_000_000, 1, 1e12),
        allocation_pct=_num(raw, "allocation_pct", 10, 0, 100, inclusive_lo=False),
        templates=templates, symbols=symbols, screen=screen, rescreen_days=days, max_positions=mp,
        cost_pct=_num(raw, "cost_pct", 0, 0, 5), account=str(raw.get("account") or "default")[:40],
    )


def _clean_screen(raw: Any) -> dict:
    """{"preset": name} or {"filters": [...]}, plus optional sort/limit/base (a symbol list to screen within)."""
    if not isinstance(raw, Mapping):
        raise BacktestError("screen must be an object")
    if raw.get("preset"):
        preset = screener.PRESETS.get(str(raw["preset"]))
        if preset is None:
            raise BacktestError(f"unknown preset {raw['preset']!r}")
        out = {"preset": str(raw["preset"]), "filters": preset["filters"], "sort": preset["sort"], "descending": preset["descending"]}
    else:
        out = {"filters": list(raw.get("filters") or []), "sort": raw.get("sort", "volume_ratio"), "descending": bool(raw.get("descending", True))}
        if not out["filters"]:
            raise BacktestError("a screen needs a preset or at least one filter")
    limit = raw.get("limit", 30)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SYMBOLS:
        raise BacktestError(f"screen limit must be 1 to {MAX_SYMBOLS}")
    out["limit"] = limit
    out["base"] = [normalise_symbol(s) for s in raw.get("base") or []] or None
    return out


# ----------------------------------------------------------------- portfolio
class Portfolio:
    """Cash and open positions. Cash is locked at the position's value on entry (longs and shorts alike)."""

    def __init__(self, capital: float, allocation_pct: float, max_positions: Optional[int], cost_pct: float):
        self.capital, self.cash = capital, capital
        self.allocation_pct, self.max_positions, self.cost_pct = allocation_pct, max_positions, cost_pct
        self.open: dict[str, PaperPosition] = {}
        self.closed: list[tuple[PaperPosition, float]] = []     # (position, fees paid)

    def settle(self, book: PaperBook) -> None:
        """Bring cash in line with whatever the book opened or closed since the last call."""
        live = {p.id: p for p in book.open_positions()}
        for pid, p in live.items():
            if pid not in self.open:
                self.open[pid] = p
                self.cash -= p.quantity * p.entry_price
        for pid in [i for i in self.open if i not in live]:
            p = self.open.pop(pid)
            fee = self.cost_pct / 100 * p.quantity * (p.entry_price + (p.exit_price or p.entry_price))
            self.cash += p.quantity * p.entry_price + (p.realized_pnl or 0.0) - fee
            self.closed.append((p, fee))

    def equity(self, marks: Mapping[str, float]) -> float:
        return self.cash + sum(p.quantity * p.entry_price + p.unrealized(marks.get(p.symbol, p.entry_price)) for p in self.open.values())

    def size(self, entry: float, marks: Mapping[str, float]) -> tuple[int, str]:
        if self.max_positions and len(self.open) >= self.max_positions:
            return 0, "position limit reached"
        budget = min(self.equity(marks) * self.allocation_pct / 100, self.cash)
        qty = int(budget // entry) if entry > 0 else 0
        return (qty, "") if qty >= 1 else (0, "not enough cash for one share")


# ------------------------------------------------------------------- history
@dataclass
class Series:
    times: list[datetime]
    bars: list[Candle]


class History:
    """Candles per symbol, loaded once and cut into every timeframe the rules need."""

    def __init__(self, db: Database, start: date, end: date):
        self.db, self.start, self.end = db, start, end
        self._daily: dict[str, list[Candle]] = {}
        self._daily_from: dict[str, date] = {}
        self._minutes: dict[str, list[Candle]] = {}
        self._series: dict[tuple[str, str], Series] = {}

    def load_daily(self, symbols: list[str], history_days: int) -> None:
        since = self.start - timedelta(days=history_days)
        todo = [s for s in symbols if s not in self._daily or self._daily_from[s] > since]
        if not todo or not self.db.table_exists("ohlcv_1d"):
            return
        for i in range(0, len(todo), 200):
            chunk = todo[i:i + 200]
            rows = self.db.rows(
                "SELECT tradingsymbol, date, open, high, low, close, volume FROM ohlcv_1d "
                "WHERE tradingsymbol = ANY(:s) AND date >= :a AND date <= :b ORDER BY tradingsymbol, date",
                s=chunk, a=since, b=self.end,
            )
            got: dict[str, list[Candle]] = {s: [] for s in chunk}
            for r in rows:
                got[r["tradingsymbol"]].append(Candle(session_open(r["date"], exchange_of(r["tradingsymbol"])), r["open"], r["high"], r["low"], r["close"], int(r["volume"] or 0)))
            for s, bars in got.items():
                self._daily[s], self._daily_from[s] = bars, since
                self._series = {k: v for k, v in self._series.items() if k[0] != s}

    def daily(self, symbol: str) -> list[Candle]:
        return self._daily.get(symbol, [])

    def minutes(self, symbol: str) -> list[Candle]:
        if symbol not in self._minutes:
            a = session_open(self.start - timedelta(days=INTRADAY_WARMUP_DAYS), exchange_of(symbol))
            self._minutes[symbol] = MinuteStore(self.db).load(symbol, a, session_close(self.end, exchange_of(symbol)))
        return self._minutes[symbol]

    def series(self, symbol: str, tf: str) -> Series:
        key = (symbol, tf)
        if key not in self._series:
            if is_intraday(tf):
                bars = self.minutes(symbol) if tf == "1m" else resample(self.minutes(symbol), tf, exchange_of(symbol))
            else:
                bars = self.daily(symbol) if tf == "1D" else resample(self.daily(symbol), "1W", exchange_of(symbol))
            self._series[key] = Series([b.time for b in bars], bars)
        return self._series[key]

    def base(self, symbol: str, intraday: bool) -> Series:
        """The finest bars available, which the stop/target sweep walks."""
        return self.series(symbol, "1m" if intraday else "1D")


# ------------------------------------------------------------------ the run
class Backtest:
    def __init__(self, db: Database, params: Params, on_progress: Optional[Callable[[str], None]] = None):
        self.live, self.p = db, params
        self.on_progress = on_progress or (lambda _: None)
        self.hist = History(db, params.start, params.end)
        self.now = session_open(params.start)
        self.portfolio = Portfolio(params.capital, params.allocation_pct, params.max_positions, params.cost_pct)
        self.marks: dict[str, float] = {}
        self.swept: dict[str, datetime] = {}
        self.members: set[str] = set()
        self.rule_ids: dict[tuple[int, str], str] = {}        # (template index, symbol) -> scratch rule id
        self.enabled: dict[str, bool] = {}
        self.queued: set[str] = set()
        self.heap: list[tuple[datetime, str, str, datetime, float]] = []
        self.curve: list[dict] = []
        self.universe_log: list[dict] = []
        self._funds: Optional[dict[str, dict]] = None
        self.no_data: set[str] = set()
        self.rejected: dict[str, int] = {}
        self.pinned = {t.symbol for t in params.templates if referenced_refs_of(t)}
        self.intraday = any(is_intraday(tf) for t in params.templates for tf in self._template_tfs(t))
        self.once: set[str] = set()

    # ................................................................ scratch schema
    def _open_scratch(self) -> tuple[Database, Callable[[], None]]:
        name = "bt_" + uuid.uuid4().hex[:12]
        with self.live.tx() as conn:
            conn.execute(text(f'CREATE SCHEMA "{name}"'))
            if self.live.table_exists("symbols"):
                conn.execute(text(f'CREATE VIEW "{name}".symbols AS SELECT * FROM symbols'))
        engine = create_engine(self.live.engine.url, connect_args={"options": f"-c search_path={name}"}, pool_pre_ping=True)
        scratch = Database(engine)
        scratch.ensure_schema()

        def drop() -> None:
            engine.dispose()
            with self.live.tx() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))

        return scratch, drop

    # ................................................................ universe
    def _screen_members(self, day: date) -> list[str]:
        spec = self.p.screen
        assert spec is not None
        candidates = spec["base"] or [r["tradingsymbol"] for r in self.live.rows(
            "SELECT DISTINCT tradingsymbol FROM ohlcv_1d WHERE date >= :a AND date <= :b AND tradingsymbol LIKE '%-EQ' ORDER BY 1",
            a=self.p.start - timedelta(days=14), b=self.p.end)]
        self.hist.load_daily(candidates, SCREEN_HISTORY_DAYS)
        rows = []
        for sym in candidates:
            bars = self.hist.daily(sym)
            i = bisect_left([b.time for b in bars], session_open(day, exchange_of(sym)))      # bars of earlier days only
            window = bars[max(0, i - 420):i]
            if len(window) < screener.MIN_BARS or (day - window[-1].time.astimezone(IST).date()).days > screener.STALE_DAYS + 3:
                continue
            m = screener.compute_metrics(window)
            if m:
                rows.append({"run_date": day, "symbol": sym, **m, "asof": window[-1].time.astimezone(IST).date()})
        for r in rows:                                              # financials are today's (see the note in the result)
            r.update(self._fundamentals().get(r["symbol"], {}))
        cols = ["run_date", "symbol", "asof", *screener.STORED]
        with self.scratch.tx() as conn:
            conn.execute(text("DELETE FROM terminal_screener_metrics"))
            if rows:
                conn.execute(text(f"INSERT INTO terminal_screener_metrics ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
                             [{c: r.get(c) for c in cols} for r in rows])
        found = screener.run_screen(self.scratch, spec["filters"], spec["sort"], spec["descending"], spec["limit"], run_date=day)
        return [r["symbol"] for r in found["rows"]]

    def _fundamentals(self) -> dict[str, dict]:
        if self._funds is None:
            cols = ", ".join(screener.FUNDAMENTALS)
            self._funds = {r["symbol"]: {k: r[k] for k in screener.FUNDAMENTALS} for r in self.live.rows(
                f"SELECT symbol, {cols} FROM terminal_screener_metrics WHERE run_date = (SELECT max(run_date) FROM terminal_screener_metrics)")}
        return self._funds

    def _refresh_universe(self, day: date) -> None:
        wanted = set(self.p.symbols) if self.p.symbols is not None else (set(self._screen_members(day)) if self.p.screen else set())
        wanted |= self.pinned
        added, removed = sorted(wanted - self.members), sorted(self.members - wanted)
        if added or removed or not self.universe_log:
            self.universe_log.append({"date": day.isoformat(), "count": len(wanted), "added": added[:25], "removed": removed[:25]})
        self.members = wanted
        for sym in added:
            self._ensure_rules(sym)
            self._queue_events(sym)
        for (_, sym), rid in self.rule_ids.items():
            on = sym in wanted
            if self.enabled.get(rid) != on:
                if on and rid in self.once and self.scratch.scalar("SELECT EXISTS (SELECT 1 FROM terminal_signals WHERE rule_id = :r)", r=rid):
                    continue          # a "once" rule that has already fired stays off
                RuleRepo(self.scratch).set_enabled(rid, on)
                self.enabled[rid] = on

    def _ensure_rules(self, sym: str) -> None:
        for i, t in enumerate(self.p.templates):
            if (i, sym) in self.rule_ids or (referenced_refs_of(t) and t.symbol != sym):
                continue                  # a rule that reads drawn lines only makes sense on the chart that holds them
            rule = replace(t, id=new_id(), symbol=sym, quantity=1, enabled=True)
            RuleRepo(self.scratch).save(rule)
            self.rule_ids[(i, sym)], self.enabled[rule.id] = rule.id, True
            if t.once:
                self.once.add(rule.id)
            if referenced_refs_of(t):
                for d in DrawingRepo(self.live).list(sym):
                    DrawingRepo(self.scratch).save(d)

    @staticmethod
    def _template_tfs(t: Rule) -> set[str]:
        """Every timeframe a rule looks at: entry, trail and exit condition."""
        tfs = {t.timeframe}
        if t.trail:
            tfs.add(t.trail.get("timeframe") or t.timeframe)
        if t.exit_condition:
            tfs.add(t.exit_timeframe or t.timeframe)
        return tfs

    def _timeframes(self, sym: str) -> set[str]:
        return {tf for i, t in enumerate(self.p.templates) if (i, sym) in self.rule_ids for tf in self._template_tfs(t)}

    def _queue_events(self, sym: str) -> None:
        if sym in self.queued:
            return
        self.queued.add(sym)
        tfs = self._timeframes(sym)
        self.hist.load_daily([sym], RULE_HISTORY_DAYS)
        lo, hi = session_open(self.p.start, exchange_of(sym)), session_close(self.p.end, exchange_of(sym))
        for tf in sorted(tfs):
            s = self.hist.series(sym, tf)
            if not s.bars:
                self.no_data.add(sym)
                continue
            for b in s.bars:
                end = bucket_end(b.time, tf, exchange_of(sym))
                if lo < end <= hi and end > self.now:
                    heapq.heappush(self.heap, (end, sym, tf, b.time, b.close))

    # ................................................................ the replay
    def _candles(self, sym: str, tf: str, n: int) -> list[Candle]:
        s = self.hist.series(sym, tf)
        i = bisect_right(s.times, self.now)
        return s.bars[max(0, i - n):i]

    def _sweep(self, sym: str, upto: datetime) -> None:
        """Stops and targets touched by the bars that closed since the last look, in time order."""
        last = self.swept.get(sym, self.now)
        self.swept[sym] = upto
        if not self.book.open_positions(sym):
            return
        minutes = self.intraday and bool(self.hist.minutes(sym))
        base = self.hist.base(sym, minutes)
        step = timedelta(minutes=1) if minutes else None
        for k in range(bisect_left(base.times, last - (step or timedelta(days=1))), len(base.bars)):
            bar = base.bars[k]
            bar_end = bar.time + step if step else bucket_end(bar.time, "1D", exchange_of(sym))
            if bar_end > upto:
                break
            if bar_end <= last:
                continue
            for p in list(self.book.open_positions(sym)):
                if bar.time >= p.opened_at and (hit := self._touch(p, bar)):
                    self.book.close(p, hit[0], hit[1], bar_end)

    @staticmethod
    def _touch(p: PaperPosition, bar: Candle) -> Optional[tuple[float, str]]:
        """(fill price, reason) if this bar reaches the stop or target. Stop first when it reaches both."""
        sign = 1 if p.side == "BUY" else -1
        if p.stop is not None:
            if (bar.open - p.stop) * sign <= 0:
                return bar.open, "stop"                          # gapped through the stop
            if ((bar.low if sign == 1 else bar.high) - p.stop) * sign <= 0:
                return p.stop, "stop"
        if p.target is not None:
            if (bar.open - p.target) * sign >= 0:
                return bar.open, "target"
            if ((bar.high if sign == 1 else bar.low) - p.target) * sign >= 0:
                return p.target, "target"
        return None

    def _record_day(self, day: date) -> None:
        self.curve.append({"date": day.isoformat(), "equity": round(self.portfolio.equity(self.marks), 2), "open": len(self.portfolio.open)})

    def run(self) -> dict:
        self.scratch, drop = self._open_scratch()
        try:
            self.book = PaperBook(self.scratch)
            self.engine = RuleEngine(self.scratch, self._candles, self.book, clock=lambda: self.now, sizer=self._size)
            return self._run()
        finally:
            drop()

    def _size(self, rule: Rule, signal) -> tuple[int, str]:
        self.portfolio.settle(self.book)
        qty, why = self.portfolio.size(signal.entry, {**self.marks, signal.symbol: signal.entry})
        if not qty:
            self.rejected[why] = self.rejected.get(why, 0) + 1
        return qty, why

    def _run(self) -> dict:
        cur_day: Optional[date] = None
        next_screen = self.p.start
        first = True
        while self.heap or first:
            if first:
                self._refresh_universe(self.p.start)
                next_screen, first = self.p.start + timedelta(days=self.p.rescreen_days), False
                continue
            day = self.heap[0][0].astimezone(IST).date()
            if day != cur_day:
                if cur_day is not None:
                    self._record_day(cur_day)
                cur_day = day
                self.on_progress(day.isoformat())
                if self.p.screen and day >= next_screen:
                    self._refresh_universe(day)
                    next_screen = day + timedelta(days=self.p.rescreen_days)
                    continue
            end, sym, tf, bar_start, close = heapq.heappop(self.heap)
            self.now = end
            self.marks[sym] = close
            self._sweep(sym, end)
            self.portfolio.settle(self.book)
            if sym in self.members or self.book.open_positions(sym):
                self.engine.on_candle_close(sym, tf, bar_start)
                self.portfolio.settle(self.book)
        if cur_day is not None:
            self._record_day(cur_day)
        return self._report()

    # ................................................................ report
    def _report(self) -> dict:
        pf = self.portfolio
        pf.settle(self.book)
        trades = [self._trade(p, fee) for p, fee in pf.closed]
        trades.sort(key=lambda t: t["exit_time"])
        open_rows = [self._open_row(p) for p in pf.open.values()]
        net = [t["pnl"] for t in trades]
        wins, losses = [x for x in net if x > 0], [x for x in net if x < 0]
        equity = pf.equity(self.marks)
        peak, drawdown = pf.capital, 0.0
        for point in self.curve:
            peak = max(peak, point["equity"])
            drawdown = max(drawdown, (peak - point["equity"]) / peak * 100)
        unrealized = sum(o["unrealized_pnl"] for o in open_rows)
        summary = {
            "initial_capital": pf.capital, "final_equity": round(equity, 2), "return_pct": round((equity / pf.capital - 1) * 100, 2),
            "realized_pnl": round(sum(net), 2), "unrealized_pnl": round(unrealized, 2), "fees": round(sum(f for _, f in pf.closed), 2),
            "trades": len(trades), "open_positions": len(open_rows), "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None, "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses else None,
            "max_drawdown_pct": round(drawdown, 2), "cash": round(pf.cash, 2),
        }
        notes = ["Stop is taken when one candle reaches both stop and target; a gap through a stop fills at the open.",
                 "Positions still open are marked at the last close, not squared off."]
        if self.p.screen:
            notes.append("Screens use price metrics as of the previous trading day; market-cap, sector and financial-data (P/E, ROCE, growth...) filters use today's values.")
        if self.no_data:
            notes.append(f"No candles for {len(self.no_data)} symbol(s) in the needed timeframe; they were skipped.")
        return {
            "summary": summary, "equity": self.curve, "trades": trades[-MAX_TRADES_RETURNED:], "open": open_rows,
            "rejected": self.rejected, "universe": self.universe_log, "no_data": sorted(self.no_data)[:100], "notes": notes,
        }

    def _trade(self, p: PaperPosition, fee: float) -> dict:
        pnl = (p.realized_pnl or 0.0) - fee
        return {
            "symbol": p.symbol, "side": p.side, "quantity": p.quantity, "entry_time": p.opened_at.isoformat(), "entry_price": p.entry_price,
            "exit_time": (p.closed_at or p.opened_at).isoformat(), "exit_price": p.exit_price, "exit_reason": p.exit_reason,
            "pnl": round(pnl, 2), "pnl_pct": round(pnl / (p.quantity * p.entry_price) * 100, 2), "initial_stop": p.initial_stop,
        }

    def _open_row(self, p: PaperPosition) -> dict:
        mark = self.marks.get(p.symbol, p.entry_price)
        return {
            "symbol": p.symbol, "side": p.side, "quantity": p.quantity, "entry_time": p.opened_at.isoformat(), "entry_price": p.entry_price,
            "stop": p.stop, "target": p.target, "last_price": mark, "unrealized_pnl": round(p.unrealized(mark), 2),
        }


def run_params(db: Database, params: Params, on_progress: Optional[Callable[[str], None]] = None) -> dict:
    return {"params": params.to_json(), **Backtest(db, params, on_progress).run()}


def run_backtest(db: Database, raw: Mapping[str, Any], on_progress: Optional[Callable[[str], None]] = None) -> dict:
    return run_params(db, parse_params(db, raw), on_progress)
