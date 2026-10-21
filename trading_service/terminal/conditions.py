"""Entry conditions, evaluated on COMPLETED candles only.

A condition is a small JSON tree so it can be stored, edited in the UI and
evaluated identically every time:

    {"type": "cross", "direction": "above", "a": <operand>, "b": <operand>}
    {"type": "compare", "op": ">", "a": <operand>, "b": <operand>}
    {"type": "all", "conditions": [<condition>, ...]}      # every one holds
    {"type": "any", "conditions": [<condition>, ...]}      # at least one holds

Operands are the things a trader compares:

    {"kind": "close" | "open" | "high" | "low" | "volume"}
    {"kind": "value", "value": 30}                          # a fixed level
    {"kind": "line", "drawing_id": "abc123"}                # a drawn trend/horizontal line
    {"kind": "indicator", "name": "ema", "params": {"period": 20}, "output": "value"}
    {"kind": "highest" | "lowest", "bars": 20, "source": "high"}   # the extreme of the PREVIOUS 20 bars (range breakout)

and one condition that is about the clock, not the price:

    {"type": "time_between", "start": "09:20", "end": "15:00"}     # IST, judged at the candle's close

Examples (all expressible without code):
    close crosses above a trend line         cross/above  close  vs  line
    close crosses above EMA(20)              cross/above  close  vs  indicator ema
    EMA(9) crosses above EMA(21)             cross/above  ema9   vs  ema21
    RSI(14) crosses below 30                 cross/below  rsi    vs  value 30
    Supertrend flips to up                   cross/above  close  vs  supertrend line

"Crosses above" means: on the previous bar a <= b and on the latest bar a > b. It is
a transition, so a condition that stays true does not fire again on every bar.
The evaluator only ever sees finished bars, so it cannot repaint.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from . import indicators as ind
from .candles import Candle
from .drawings import EDGES, Drawing, line_series
from .timeframes import IST, bucket_end, to_epoch, validate_timeframe

PRICE_KINDS = ("open", "high", "low", "close", "volume")
COMPARE_OPS = (">", ">=", "<", "<=")
MAX_BARS = 500  # longest look-back for highest/lowest
MAX_DEPTH = 4
MAX_NODES = 20


class ConditionError(ValueError):
    """The condition is malformed or cannot be evaluated (e.g. a deleted line)."""


# ------------------------------------------------------------------ validation
def validate_operand(raw: Any) -> dict:
    if not isinstance(raw, Mapping):
        raise ConditionError("operand must be an object")
    kind = raw.get("kind")
    if kind in PRICE_KINDS:
        return {"kind": kind}
    if kind == "value":
        v = raw.get("value")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ConditionError("value operand needs a finite number")
        return {"kind": "value", "value": float(v)}
    if kind == "line":
        did = raw.get("drawing_id")
        if not isinstance(did, str) or not did:
            raise ConditionError("line operand needs a drawing_id")
        return {"kind": "line", "drawing_id": did, **_edge(raw)}
    if kind in ("highest", "lowest"):
        bars = raw.get("bars", 20)
        if isinstance(bars, bool) or not isinstance(bars, int) or not 1 <= bars <= MAX_BARS:
            raise ConditionError(f"{kind}: 'bars' must be a whole number from 1 to {MAX_BARS}")
        source = raw.get("source") or ("high" if kind == "highest" else "low")
        if source not in PRICE_KINDS:
            raise ConditionError(f"{kind}: 'source' must be one of {', '.join(PRICE_KINDS)}")
        return {"kind": kind, "bars": bars, "source": source}
    if kind == "indicator":
        name = raw.get("name")
        try:
            params = ind.normalise_params(name, raw.get("params"))
        except ValueError as e:
            raise ConditionError(str(e)) from None
        output = raw.get("output") or ind.default_output(name)
        if output not in ind.SPECS[name][1]:
            raise ConditionError(f"{name} has no output {output!r}; expected one of {', '.join(ind.SPECS[name][1])}")
        return {"kind": "indicator", "name": name, "params": params, "output": output}
    raise ConditionError(f"unknown operand kind {kind!r}")


def validate_condition(raw: Any, _depth: int = 1, _count: Optional[list] = None) -> dict:
    """Return a normalised copy of `raw` or raise ConditionError."""
    count = _count if _count is not None else [0]
    count[0] += 1
    if count[0] > MAX_NODES:
        raise ConditionError(f"condition is too large (max {MAX_NODES} nodes)")
    if _depth > MAX_DEPTH:
        raise ConditionError(f"condition is nested too deeply (max {MAX_DEPTH} levels)")
    if not isinstance(raw, Mapping):
        raise ConditionError("condition must be an object")
    kind = raw.get("type")
    if kind == "cross":
        if raw.get("direction") not in ("above", "below"):
            raise ConditionError("cross direction must be 'above' or 'below'")
        return {"type": "cross", "direction": raw["direction"], "a": validate_operand(raw.get("a")), "b": validate_operand(raw.get("b"))}
    if kind == "compare":
        if raw.get("op") not in COMPARE_OPS:
            raise ConditionError(f"compare op must be one of {', '.join(COMPARE_OPS)}")
        return {"type": "compare", "op": raw["op"], "a": validate_operand(raw.get("a")), "b": validate_operand(raw.get("b"))}
    if kind in ("all", "any"):
        children = raw.get("conditions")
        if not isinstance(children, Sequence) or isinstance(children, (str, bytes)) or not children:
            raise ConditionError(f"'{kind}' needs a non-empty list of conditions")
        return {"type": kind, "conditions": [validate_condition(c, _depth + 1, count) for c in children]}
    if kind == "time_between":
        start, end = _clock(raw.get("start"), "start"), _clock(raw.get("end"), "end")
        if start > end:
            raise ConditionError("time_between: 'start' must not be after 'end' (a window cannot run past midnight)")
        return {"type": "time_between", "start": raw["start"], "end": raw["end"]}
    raise ConditionError(f"unknown condition type {kind!r}")


def _clock(text: Any, name: str) -> int:
    """'HH:MM' -> minutes since midnight."""
    try:
        h, m = str(text).split(":")
        if len(h) != 2 or len(m) != 2:
            raise ValueError
        h, m = int(h), int(m)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
    except ValueError:
        raise ConditionError(f"time_between: '{name}' must look like 09:20") from None
    return h * 60 + m


def _edge(raw: Mapping[str, Any]) -> dict:
    """Which edge of a rectangle a line reference reads (ignored for other drawings)."""
    edge = raw.get("edge")
    if edge is None:
        return {}
    if edge not in EDGES:
        raise ConditionError(f"edge must be one of {', '.join(EDGES)}")
    return {"edge": edge}


def referenced_refs(cond: Mapping[str, Any]) -> set[tuple[str, Optional[str]]]:
    """(drawing id, rectangle edge) of every drawing the condition reads."""
    found: set[tuple[str, Optional[str]]] = set()
    for key in ("a", "b"):
        operand = cond.get(key)
        if isinstance(operand, Mapping) and operand.get("kind") == "line":
            found.add((operand["drawing_id"], operand.get("edge")))
    for child in cond.get("conditions", []):
        found |= referenced_refs(child)
    return found


def referenced_drawings(cond: Mapping[str, Any]) -> set[str]:
    """Ids of every drawing the condition depends on."""
    return {i for i, _ in referenced_refs(cond)}


# ------------------------------------------------------------------ evaluation
@dataclass(frozen=True)
class Evaluation:
    fired: bool
    note: str = ""


class EvalContext:
    """Candles (completed, ascending) plus lazily computed operand series."""

    def __init__(self, candles: Sequence[Candle], tf: str, drawings: Mapping[str, Drawing] | None = None, exchange: str = "NSE"):
        self.candles = list(candles)
        self.tf = tf
        self.exchange = exchange  # decides when a candle ends (NSE 15:30, MCX 23:30)
        self.drawings = dict(drawings or {})
        self.times = [to_epoch(c.time) for c in self.candles]
        self.open = np.array([c.open for c in self.candles], dtype=float)
        self.high = np.array([c.high for c in self.candles], dtype=float)
        self.low = np.array([c.low for c in self.candles], dtype=float)
        self.close = np.array([c.close for c in self.candles], dtype=float)
        self.volume = np.array([c.volume for c in self.candles], dtype=float)
        self._cache: dict[str, np.ndarray] = {}

    def series(self, operand: Mapping[str, Any]) -> np.ndarray:
        kind = operand["kind"]
        n = len(self.candles)
        if kind in PRICE_KINDS:
            return getattr(self, kind)
        if kind == "value":
            return np.full(n, operand["value"])
        key = repr(sorted(operand.items(), key=lambda kv: kv[0]))
        if key in self._cache:
            return self._cache[key]
        if kind in ("highest", "lowest"):
            out = _extreme(getattr(self, operand["source"]), operand["bars"], kind == "highest")
        elif kind == "line":
            drawing = self.drawings.get(operand["drawing_id"])
            if drawing is None:
                raise ConditionError(f"drawing {operand['drawing_id']} no longer exists")
            values = line_series(drawing, self.times, self.tf, operand.get("edge"))
            out = np.array([np.nan if v is None else v for v in values], dtype=float)
        else:
            out = ind.compute(operand["name"], operand["params"], self.high, self.low, self.close)[operand["output"]]
        self._cache[key] = out
        return out


def _extreme(values: np.ndarray, bars: int, highest: bool) -> np.ndarray:
    """At each bar, the highest/lowest of the `bars` bars BEFORE it (NaN until there are that many).
    Looking only at earlier bars is what makes "close crosses above the 20-bar high" possible."""
    out = np.full(len(values), np.nan)
    if len(values) > bars:
        windows = np.lib.stride_tricks.sliding_window_view(values, bars)[:-1]  # window i covers bars i..i+bars-1
        out[bars:] = windows.max(axis=1) if highest else windows.min(axis=1)
    return out


def _label(operand: Mapping[str, Any]) -> str:
    kind = operand["kind"]
    if kind in ("highest", "lowest"):
        return f"{kind}({operand['bars']} {operand['source']})"
    if kind == "value":
        return f"{operand['value']:g}"
    if kind == "line":
        return "line"
    if kind == "indicator":
        args = ",".join(f"{v:g}" for v in operand["params"].values())
        return f"{operand['name'].upper()}({args})" + ("" if operand["output"] in ("value", ind.default_output(operand["name"])) else f".{operand['output']}")
    return kind


def evaluate(cond: Mapping[str, Any], ctx: EvalContext) -> Evaluation:
    """Evaluate a *validated* condition at the latest completed bar."""
    kind = cond["type"]
    if kind == "all":
        results = [evaluate(c, ctx) for c in cond["conditions"]]
        ok = all(r.fired for r in results)
        return Evaluation(ok, "; ".join(r.note for r in results if r.note) if ok else "")
    if kind == "any":
        for c in cond["conditions"]:
            r = evaluate(c, ctx)
            if r.fired:
                return r
        return Evaluation(False)

    if kind == "time_between":
        if not ctx.candles:
            return Evaluation(False)
        end = bucket_end(ctx.candles[-1].time, ctx.tf, ctx.exchange).astimezone(IST)  # when the candle finished
        closed = end.hour * 60 + end.minute
        ok = _clock(cond["start"], "start") <= closed <= _clock(cond["end"], "end")
        return Evaluation(ok, f"candle closed at {closed // 60:02d}:{closed % 60:02d} IST, inside {cond['start']}-{cond['end']}" if ok else "")

    if len(ctx.candles) < 2:
        return Evaluation(False)
    a, b = ctx.series(cond["a"]), ctx.series(cond["b"])
    a1, b1 = a[-1], b[-1]
    if not (math.isfinite(a1) and math.isfinite(b1)):
        return Evaluation(False)
    la, lb = _label(cond["a"]), _label(cond["b"])

    if kind == "compare":
        op = cond["op"]
        ok = {">": a1 > b1, ">=": a1 >= b1, "<": a1 < b1, "<=": a1 <= b1}[op]
        return Evaluation(bool(ok), f"{la} {a1:.2f} {op} {lb} {b1:.2f}" if ok else "")

    a0, b0 = a[-2], b[-2]
    if not (math.isfinite(a0) and math.isfinite(b0)):
        return Evaluation(False)
    if cond["direction"] == "above":
        ok = a0 <= b0 and a1 > b1
    else:
        ok = a0 >= b0 and a1 < b1
    note = f"{la} crossed {cond['direction']} {lb} ({a1:.2f} vs {b1:.2f})" if ok else ""
    return Evaluation(bool(ok), note)


# ------------------------------------------------------------- stops & targets
class InvalidRisk(ValueError):
    """The computed stop or target is on the wrong side of the entry."""


STOP_TYPES = ("fixed", "pct", "atr", "candle", "line")
TARGET_TYPES = ("rr", "pct", "fixed")


def validate_stop(raw: Any) -> dict:
    if not isinstance(raw, Mapping):
        raise ConditionError("stop must be an object")
    kind = raw.get("type")
    if kind not in STOP_TYPES:
        raise ConditionError(f"stop type must be one of {', '.join(STOP_TYPES)}")
    def positive(key: str, default: Optional[float] = None) -> float:
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0:
            raise ConditionError(f"stop {kind}: '{key}' must be a positive number")
        return float(v)
    if kind == "fixed":
        return {"type": "fixed", "price": positive("price")}
    if kind == "pct":
        return {"type": "pct", "value": positive("value")}
    if kind == "atr":
        return {"type": "atr", "period": int(positive("period", 14)), "mult": positive("mult", 2.0)}
    if kind == "candle":
        buffer = raw.get("buffer_pct", 0)
        if isinstance(buffer, bool) or not isinstance(buffer, (int, float)) or buffer < 0:
            raise ConditionError("stop candle: 'buffer_pct' must be zero or positive")
        return {"type": "candle", "buffer_pct": float(buffer)}
    did = raw.get("drawing_id")
    if not isinstance(did, str) or not did:
        raise ConditionError("stop line needs a drawing_id")
    return {"type": "line", "drawing_id": did, **_edge(raw)}


def validate_target(raw: Any) -> Optional[dict]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ConditionError("target must be an object")
    kind = raw.get("type")
    if kind not in TARGET_TYPES:
        raise ConditionError(f"target type must be one of {', '.join(TARGET_TYPES)}")
    key = {"rr": "ratio", "pct": "value", "fixed": "price"}[kind]
    v = raw.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0:
        raise ConditionError(f"target {kind}: '{key}' must be a positive number")
    return {"type": kind, key: float(v)}


def compute_stop(spec: Mapping[str, Any], side: str, entry: float, ctx: EvalContext) -> float:
    """Stop price for a position entered at `entry` on the latest completed bar."""
    long = side == "BUY"
    kind = spec["type"]
    if kind == "fixed":
        stop = spec["price"]
    elif kind == "pct":
        stop = entry * (1 - spec["value"] / 100) if long else entry * (1 + spec["value"] / 100)
    elif kind == "atr":
        value = ind.atr(ctx.high, ctx.low, ctx.close, spec["period"])[-1]
        if not math.isfinite(value):
            raise InvalidRisk(f"not enough history for ATR({spec['period']})")
        stop = entry - spec["mult"] * value if long else entry + spec["mult"] * value
    elif kind == "candle":
        bar = ctx.candles[-1]
        buf = spec["buffer_pct"] / 100
        stop = bar.low * (1 - buf) if long else bar.high * (1 + buf)
    else:
        drawing = ctx.drawings.get(spec["drawing_id"])
        if drawing is None:
            raise InvalidRisk(f"stop line {spec['drawing_id']} no longer exists")
        stop = line_series(drawing, ctx.times, ctx.tf, spec.get("edge"))[-1]
        if stop is None:
            raise InvalidRisk("stop line is degenerate")
    if long and not stop < entry:
        raise InvalidRisk(f"stop {stop:.2f} must be below the entry {entry:.2f} for a BUY")
    if not long and not stop > entry:
        raise InvalidRisk(f"stop {stop:.2f} must be above the entry {entry:.2f} for a SELL")
    return float(stop)


def compute_target(spec: Optional[Mapping[str, Any]], side: str, entry: float, stop: float) -> Optional[float]:
    if spec is None:
        return None
    long = side == "BUY"
    kind = spec["type"]
    if kind == "rr":
        risk = abs(entry - stop)
        target = entry + spec["ratio"] * risk if long else entry - spec["ratio"] * risk
    elif kind == "pct":
        target = entry * (1 + spec["value"] / 100) if long else entry * (1 - spec["value"] / 100)
    else:
        target = spec["price"]
    if long and not target > entry:
        raise InvalidRisk(f"target {target:.2f} must be above the entry {entry:.2f} for a BUY")
    if not long and not target < entry:
        raise InvalidRisk(f"target {target:.2f} must be below the entry {entry:.2f} for a SELL")
    return float(target)


# ------------------------------------------------------------- trailing stops
TRAIL_TYPES = ("candle", "atr", "pct", "indicator", "line")


def validate_trail(raw: Any) -> Optional[dict]:
    """A trailing-stop spec, or None. `timeframe` (optional) is the candle size the trail is evaluated on;
    left out it follows the rule's own timeframe."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ConditionError("trail must be an object")
    kind = raw.get("type")
    if kind not in TRAIL_TYPES:
        raise ConditionError(f"trail type must be one of {', '.join(TRAIL_TYPES)}")

    def number(key: str, default: Optional[float], minimum_exclusive: float = 0) -> float:
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > minimum_exclusive:
            raise ConditionError(f"trail {kind}: '{key}' must be a number above {minimum_exclusive:g}")
        return float(v)

    out: dict = {"type": kind}
    if raw.get("timeframe"):
        out["timeframe"] = validate_timeframe(str(raw["timeframe"]))
    if kind == "candle":
        bars = int(number("bars", 1))
        if not 1 <= bars <= 50:
            raise ConditionError("trail candle: 'bars' must be between 1 and 50")
        buffer = raw.get("buffer_pct", 0)
        if isinstance(buffer, bool) or not isinstance(buffer, (int, float)) or buffer < 0:
            raise ConditionError("trail candle: 'buffer_pct' must be zero or positive")
        out.update(bars=bars, buffer_pct=float(buffer))
    elif kind == "atr":
        out.update(period=int(number("period", 14)), mult=number("mult", 2.0))
    elif kind == "pct":
        out["value"] = number("value", None)
    elif kind == "indicator":
        operand = validate_operand({"kind": "indicator", "name": raw.get("name"), "params": raw.get("params"), "output": raw.get("output")})
        out.update(name=operand["name"], params=operand["params"], output=operand["output"])
    else:
        did = raw.get("drawing_id")
        if not isinstance(did, str) or not did:
            raise ConditionError("trail line needs a drawing_id")
        out["drawing_id"] = did
        out.update(_edge(raw))
    return out


def compute_trail(spec: Mapping[str, Any], side: str, ctx: EvalContext) -> Optional[float]:
    """Where the trailing stop WOULD sit after the latest completed bar, or None when it cannot be known
    (not enough history, a deleted line). Whether it is allowed to move the stop is the caller's call:
    a stop only ever tightens, and never lands on the wrong side of the last close."""
    long = side == "BUY"
    kind = spec["type"]
    last_close = float(ctx.close[-1])
    if kind == "candle":
        n = spec["bars"]
        buf = spec["buffer_pct"] / 100
        value = float(ctx.low[-n:].min()) * (1 - buf) if long else float(ctx.high[-n:].max()) * (1 + buf)
    elif kind == "atr":
        atr = ind.atr(ctx.high, ctx.low, ctx.close, spec["period"])[-1]
        value = last_close - spec["mult"] * atr if long else last_close + spec["mult"] * atr
    elif kind == "pct":
        value = last_close * (1 - spec["value"] / 100) if long else last_close * (1 + spec["value"] / 100)
    elif kind == "indicator":
        value = ctx.series({"kind": "indicator", "name": spec["name"], "params": spec["params"], "output": spec["output"]})[-1]
    else:
        drawing = ctx.drawings.get(spec["drawing_id"])
        if drawing is None:
            return None
        raw = line_series(drawing, ctx.times, ctx.tf, spec.get("edge"))[-1]
        value = np.nan if raw is None else raw
    return float(value) if math.isfinite(value) else None


# ------------------------------------------------------------------ explaining
def explain(cond: Mapping[str, Any], ctx: EvalContext, live: Optional[float] = None) -> dict:
    """How a *validated* condition stands at the latest completed bar, and how far each part is from triggering.

    Leaves: {label, met, a, b, a_live, distance, distance_pct, side}. `distance` is how far `a` must still move (in its
    own units, signed so that > 0 means "not there yet") for the condition to hold, and `distance_pct` is that move as a
    percentage of where `a` is now; for a cross it is measured to the other side of `b`. `a_live` uses the live price when `a` is a price, so the screen can show the move in progress.
    Groups: {"all"|"any", met, children, distance_pct} where all = the farthest unmet child, any = the nearest."""
    kind = cond["type"]
    if kind in ("all", "any"):
        kids = [explain(c, ctx, live) for c in cond["conditions"]]
        met = all(k["met"] for k in kids) if kind == "all" else any(k["met"] for k in kids)
        pending = [k["distance_pct"] for k in kids if not k["met"] and k.get("distance_pct") is not None]
        dist = 0.0 if met else ((max(pending) if kind == "all" else min(pending)) if pending else None)
        return {"type": kind, "met": met, "children": kids, "distance_pct": dist}
    if kind == "time_between":
        r = evaluate(cond, ctx)
        return {"type": kind, "label": f"time {cond['start']}-{cond['end']} IST", "met": r.fired, "distance_pct": 0.0 if r.fired else None}
    if len(ctx.candles) < 2:
        return {"type": kind, "label": "not enough candles yet", "met": False, "distance_pct": None}
    try:
        a, b = ctx.series(cond["a"]), ctx.series(cond["b"])
    except ConditionError as e:
        return {"type": kind, "label": str(e), "met": False, "distance_pct": None}
    a1, b1 = float(a[-1]), float(b[-1])
    la, lb = _label(cond["a"]), _label(cond["b"])
    if not (math.isfinite(a1) and math.isfinite(b1)):
        return {"type": kind, "label": f"{la} vs {lb}: not enough history", "met": False, "distance_pct": None}
    up = (cond.get("direction") == "above") if kind == "cross" else cond["op"] in (">", ">=")
    met = evaluate(cond, ctx).fired
    a_live = live if (live is not None and cond["a"]["kind"] == "close") else None
    word = (f"crosses {cond['direction']}" if kind == "cross" else cond["op"])
    last_gap = (b1 - a1) if up else (a1 - b1)
    if kind == "cross" and not met and last_gap <= 0:
        # already on the far side at the last close without crossing on it: the cross happened earlier; only going back
        # and crossing again would fire, so it is not "0% away"
        return {"type": kind, "label": f"{la} {word} {lb}", "met": False, "a": round(a1, 4), "b": round(b1, 4),
                "a_live": round(a_live, 4) if a_live is not None else None, "side": "above" if up else "below",
                "state": "crossed_earlier", "distance": None, "distance_pct": None, "on_trigger_side": False}
    now = a_live if a_live is not None else a1
    gap = (b1 - now) if up else (now - b1)             # > 0: still to go; <= 0: already on the triggering side
    return {
        "type": kind, "label": f"{la} {word} {lb}", "met": met, "a": round(a1, 4), "b": round(b1, 4),
        "a_live": round(a_live, 4) if a_live is not None else None, "side": "above" if up else "below",
        "distance": round(gap, 4), "distance_pct": 0.0 if met else round(max(gap, 0.0) / abs(now) * 100, 3) if now else None,
        "on_trigger_side": gap <= 0,
    }
