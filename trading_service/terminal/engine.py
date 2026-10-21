"""The rule engine: turns a closed candle into signals and paper positions.

`on_candle_close(symbol, timeframe, bar_start)` is the only entry point and it is
called by the candle builder when a bar finishes. It

1. loads the enabled rules for that symbol + timeframe,
2. fetches the bars and keeps ONLY completed ones (`candles.completed`),
3. evaluates each rule's condition at the latest completed bar,
4. computes entry (the bar's close), stop and target, rejecting nonsensical risk,
5. records a signal - once per rule per bar, enforced by the database - and opens
   a paper position for it,
6. then looks after the positions that are already open on that symbol: a trailing stop
   is tightened and an exit condition may close the position, each on completed candles of
   the timeframe the position was opened with (not the one on anyone's screen), and
7. returns events for the websocket hub to broadcast.

It never reads the forming bar or a live tick, which is what makes entries
non-repainting: the same bars give the same answer however often it is called.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from .candles import Candle, completed
from .conditions import (
    ConditionError,
    EvalContext,
    InvalidRisk,
    compute_stop,
    compute_target,
    compute_trail,
    evaluate,
)
from .db import Database
from .drawings import DrawingRepo
from .paper import PaperBook, PaperPosition
from .rules import Rule, RuleRepo, Signal, SignalRepo
from .timeframes import bucket_end, exchange_of

log = logging.getLogger(__name__)

# (symbol, timeframe, how many bars) -> bars ascending; may include a forming bar.
CandleSource = Callable[[str, str, int], Sequence[Candle]]
# (rule, open signal) -> (quantity, why not). Live trading uses the rule's own quantity; the backtest sizes by capital.
Sizer = Callable[[Rule, Signal], "tuple[int, str]"]

LOOKBACK_BARS = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _score_quantity(rule: Rule, close: float, ctx: EvalContext) -> int:
    """Compute entry qty from the rule's entry_score_indicator distance at the signal bar."""
    import numpy as np
    from . import indicators as ind

    spec = rule.entry_score_indicator
    if not spec or not rule.entry_levels:
        return rule.quantity
    try:
        highs = np.array([b.high for b in ctx.candles])
        lows = np.array([b.low for b in ctx.candles])
        closes = np.array([b.close for b in ctx.candles])
        vals = ind.compute(spec["name"], spec.get("params", {}), highs, lows, closes)
        raw = vals.get(spec.get("output", "value"), [None])[-1]
        if raw is None or np.isnan(raw):
            return rule.quantity
        indicator_val = float(raw)
    except Exception as e:
        log.warning("entry score indicator computation failed for rule %s: %s", rule.id, e)
        return rule.quantity
    return rule.score_quantity(close, indicator_val)


class RuleEngine:
    def __init__(
        self,
        db: Database,
        candles: CandleSource,
        book: PaperBook,
        clock: Callable[[], datetime] = _utcnow,
        sizer: Optional[Sizer] = None,
    ):
        self.db = db
        self.candles = candles
        self.book = book
        self.clock = clock
        self.sizer = sizer
        self.rules = RuleRepo(db)
        self.drawings = DrawingRepo(db)
        self.signals = SignalRepo(db)

    def on_candle_close(self, symbol: str, timeframe: str, bar_start: datetime) -> list[dict]:
        return self._enter(symbol, timeframe, bar_start) + self._manage(symbol, timeframe, bar_start)

    # ------------------------------------------------------------- entries
    def _enter(self, symbol: str, timeframe: str, bar_start: datetime) -> list[dict]:
        rules = self.rules.list(symbol=symbol, timeframe=timeframe, enabled_only=True)
        if not rules:
            return []
        now = self.clock()
        bars = completed(self.candles(symbol, timeframe, LOOKBACK_BARS), timeframe, now, symbol)
        if not bars or bars[-1].time < bar_start:
            log.debug("rule engine: no completed %s bar >= %s for %s yet", timeframe, bar_start, symbol)
            return []
        ctx = EvalContext(bars, timeframe, {d.id: d for d in self.drawings.list(symbol)}, exchange_of(symbol))
        events: list[dict] = []
        for rule in rules:
            events.extend(self._run_rule(rule, ctx))
        return events

    def _run_rule(self, rule: Rule, ctx: EvalContext) -> list[dict]:
        try:
            result = evaluate(rule.condition, ctx)
        except ConditionError as e:
            log.warning("rule %s skipped: %s", rule.id, e)
            return []
        if not result.fired:
            return []

        bar = ctx.candles[-1]
        entry = bar.close
        signal = Signal(
            symbol=rule.symbol, timeframe=rule.timeframe, side=rule.side, bar_time=bar.time,
            entry=entry, status="open", rule_id=rule.id, reason=result.note,
        )
        try:
            signal.stop = compute_stop(rule.stop, rule.side, entry, ctx)
            signal.target = compute_target(rule.target, rule.side, entry, signal.stop)
        except InvalidRisk as e:
            signal.status, signal.reason = "rejected", str(e)
        if signal.status == "open" and self.book.has_open_for_rule(rule.id):
            signal.status, signal.reason = "rejected", "already in a position from this rule"

        quantity = rule.quantity
        if signal.status == "open" and rule.entry_score_indicator:
            quantity = _score_quantity(rule, entry, ctx)
        if signal.status == "open" and self.sizer:
            quantity, why = self.sizer(rule, signal)
            if quantity < 1:
                signal.status, signal.reason = "rejected", why

        if not self.signals.insert_if_new(signal):
            return []  # this rule already fired on this bar
        events: list[dict] = [{"type": "signal", "signal": signal.to_json()}]
        if signal.status == "open":
            position = self.book.open_from_signal(signal, quantity, self.clock(), **rule.exit_plan())  # type: ignore[arg-type]
            events.append({"type": "position", "position": position.to_json()})
            if rule.once:
                self.rules.set_enabled(rule.id, False)
        return events

    # ----------------------------------------------------- open positions
    @staticmethod
    def _due(p: PaperPosition, timeframe: str) -> tuple[bool, bool]:
        """(trail is due, exit rule is due) for a candle of `timeframe` closing."""
        trail_due = bool(p.trail) and p.trail.get("timeframe") == timeframe
        if p.exit_level == 1:
            exit_due = bool(p.exit_condition) and p.exit_timeframe == timeframe
        else:
            exit_due = bool(p.exit_condition_2) and (p.exit_timeframe_2 or p.exit_timeframe) == timeframe
        return trail_due, exit_due

    def _manage(self, symbol: str, timeframe: str, bar_start: datetime) -> list[dict]:
        due = [(p, *self._due(p, timeframe)) for p in self.book.open_positions(symbol)]
        due = [(p, trail, exit_) for p, trail, exit_ in due if trail or exit_]
        if not due:
            return []
        now = self.clock()
        bars = completed(self.candles(symbol, timeframe, LOOKBACK_BARS), timeframe, now, symbol)
        if not bars or bars[-1].time < bar_start:
            return []
        last = bars[-1]
        ctx = EvalContext(bars, timeframe, {d.id: d for d in self.drawings.list(symbol)}, exchange_of(symbol))
        closed_at = bucket_end(last.time, timeframe, exchange_of(symbol))
        events: list[dict] = []
        for p, trail, exit_ in due:
            if closed_at <= p.opened_at:
                continue  # the candle the position was opened on (or one before it): the plan starts with the next one
            if exit_ and self._exit_fires(p, ctx):
                if p.exit_level == 1 and p.exit_condition_2:
                    # First of two exits: close 50%, stay open waiting for level 2
                    events.append({"type": "position", "position": self.book.partial_close(p, last.close, "exit_rule_1", closed_at).to_json()})
                else:
                    # Either a single-exit rule, or the second level of a 2-level exit
                    reason = "exit_rule_2" if p.exit_level == 2 else "exit_rule"
                    events.append({"type": "position", "position": self.book.close(p, last.close, reason, closed_at).to_json()})
            elif trail and self._trail(p, ctx):
                events.append({"type": "position", "position": p.to_json()})
        return events

    def _exit_fires(self, p: PaperPosition, ctx: EvalContext) -> bool:
        cond = p.exit_condition if p.exit_level == 1 else p.exit_condition_2
        if not cond:
            return False
        try:
            return evaluate(cond, ctx).fired
        except ConditionError as e:
            log.warning("exit rule of position %s (level %s) skipped: %s", p.id, p.exit_level, e)
            return False

    def _trail(self, p: PaperPosition, ctx: EvalContext) -> bool:
        """Tighten the stop if the trail allows it. True when the stop moved."""
        try:
            level = compute_trail(p.trail, p.side, ctx)
        except ConditionError as e:
            log.warning("trail of position %s skipped: %s", p.id, e)
            return False
        close = float(ctx.close[-1])
        if level is None or not (level < close if p.side == "BUY" else level > close):
            return False   # unknown, or on the wrong side of the last close: it would stop the trade out at once
        before = p.stop
        self.book.move_stop(p, level)
        return p.stop != before
