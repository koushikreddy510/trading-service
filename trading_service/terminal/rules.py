"""Entry rules: model, validation and storage."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .conditions import (
    ConditionError,
    referenced_refs,
    validate_condition,
    validate_stop,
    validate_target,
    validate_trail,
)
from .db import Database, jdump, new_id
from .drawings import LOCAL_USER
from .timeframes import to_epoch, validate_timeframe


class InvalidRule(ValueError):
    pass


@dataclass
class Rule:
    symbol: str
    timeframe: str
    side: str
    condition: dict
    stop: dict
    quantity: int
    target: Optional[dict] = None
    name: str = ""
    enabled: bool = True
    once: bool = False
    id: str = field(default_factory=new_id)
    # Exit plan (see schema.sql). A trail without its own timeframe follows the rule's; an exit condition is
    # checked on `exit_timeframe` (default: the rule's) and closes the position at that candle's close.
    trail: Optional[dict] = None
    exit_condition: Optional[dict] = None
    exit_timeframe: Optional[str] = None
    exit_condition_2: Optional[dict] = None
    exit_timeframe_2: Optional[str] = None
    # Confidence-scored entry: optional indicator + up to 3 quantity levels.
    # entry_levels = [{qty, max_dist_pct?}, ...] sorted tightest-first.
    # Last entry is the catch-all (no max_dist_pct).
    entry_score_indicator: Optional[dict] = None
    entry_levels: Optional[list] = None
    # Live execution: None = paper only; {"account": "flattrade", "product": "I"|"F"|"M"} = mirror the paper position
    # with real orders on that account (live_mirror.py). The account's own live switch must also be on.
    execution: Optional[dict] = None

    def score_quantity(self, close: float, indicator_val: float) -> int:
        """Return the entry quantity based on distance of close from indicator_val.

        Walks entry_levels from tightest to widest; returns the qty of the first
        level whose max_dist_pct >= actual distance (or the catch-all last level).
        Falls back to self.quantity when scoring is not configured.
        """
        if not self.entry_score_indicator or not self.entry_levels:
            return self.quantity
        dist_pct = abs(close - indicator_val) / indicator_val * 100 if indicator_val else None
        if dist_pct is None:
            return self.quantity
        for level in self.entry_levels:
            max_d = level.get("max_dist_pct")
            if max_d is None or dist_pct <= max_d:
                return int(level["qty"])
        return int(self.entry_levels[-1]["qty"])

    def to_json(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "timeframe": self.timeframe, "name": self.name,
            "side": self.side, "condition": self.condition, "stop": self.stop, "target": self.target,
            "quantity": self.quantity, "enabled": self.enabled, "once": self.once,
            "trail": self.trail, "exit_condition": self.exit_condition, "exit_timeframe": self.exit_timeframe,
            "exit_condition_2": self.exit_condition_2, "exit_timeframe_2": self.exit_timeframe_2,
            "entry_score_indicator": self.entry_score_indicator, "entry_levels": self.entry_levels,
            "execution": self.execution,
        }

    def exit_plan(self) -> dict:
        """What a position opened by this rule carries: timeframes resolved now, so later edits cannot change it."""
        trail = {**self.trail, "timeframe": self.trail.get("timeframe") or self.timeframe} if self.trail else None
        return {
            "trail": trail,
            "exit_condition": self.exit_condition,
            "exit_timeframe": self.exit_timeframe or self.timeframe,
            "exit_condition_2": self.exit_condition_2,
            "exit_timeframe_2": self.exit_timeframe_2 or (self.timeframe if self.exit_condition_2 else None),
        }


def _validate_entry_indicator(raw: Any) -> Optional[dict]:
    if not raw:
        return None
    if not isinstance(raw, dict) or "name" not in raw:
        raise InvalidRule("entry_score_indicator must be an object with a 'name'")
    # Reuse indicator validation via a throw-away indicator operand
    validate_condition({"type": "compare", "op": ">",
                        "a": {"kind": "indicator", "name": raw["name"], "params": raw.get("params", {}),
                              "output": raw.get("output", "value")},
                        "b": {"kind": "value", "value": 0}})
    return {"name": raw["name"], "params": raw.get("params", {}), "output": raw.get("output", "value")}


def _validate_entry_levels(raw: Any, rule_qty: int) -> Optional[list]:
    if not raw:
        return None
    if not isinstance(raw, list) or not (1 <= len(raw) <= 3):
        raise InvalidRule("entry_levels must be a list of 1–3 items")
    levels = []
    for item in raw:
        if not isinstance(item, dict) or "qty" not in item:
            raise InvalidRule("each entry level must have a 'qty'")
        qty = item["qty"]
        if not isinstance(qty, (int, float)) or qty <= 0:
            raise InvalidRule("entry level 'qty' must be a positive number")
        level: dict[str, Any] = {"qty": int(qty)}
        if "max_dist_pct" in item:
            d = item["max_dist_pct"]
            if not isinstance(d, (int, float)) or d <= 0:
                raise InvalidRule("entry level 'max_dist_pct' must be positive")
            level["max_dist_pct"] = float(d)
        levels.append(level)
    # Last level must be the catch-all (no max_dist_pct)
    if "max_dist_pct" in levels[-1]:
        raise InvalidRule("the last entry level must not have 'max_dist_pct' (it is the catch-all)")
    return levels


LIVE_ACCOUNTS = ("flattrade",)
LIVE_PRODUCTS = ("I", "F", "M")      # Intraday, MTF (stocks), NRML (derivatives); never delivery


def _validate_execution(raw: Any) -> Optional[dict]:
    if not raw:
        return None
    if not isinstance(raw, Mapping):
        raise InvalidRule("execution must be an object like {account, product}")
    account, product = str(raw.get("account") or ""), str(raw.get("product") or "")
    if account not in LIVE_ACCOUNTS:
        raise InvalidRule(f"execution account must be one of {', '.join(LIVE_ACCOUNTS)}")
    if product not in LIVE_PRODUCTS:
        raise InvalidRule("execution product must be I (Intraday), F (MTF) or M (NRML)")
    return {"account": account, "product": product}


def rule_from_payload(raw: Mapping[str, Any], rule_id: Optional[str] = None) -> Rule:
    """Validate an API payload into a Rule (raises InvalidRule with a readable message)."""
    try:
        symbol = raw.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise InvalidRule("symbol is required")
        timeframe = validate_timeframe(str(raw.get("timeframe", "")))
        side = str(raw.get("side", "")).upper()
        if side not in ("BUY", "SELL"):
            raise InvalidRule("side must be BUY or SELL")
        qty = raw.get("quantity")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise InvalidRule("quantity must be a positive whole number")
        rule = Rule(
            symbol=symbol.strip(),
            timeframe=timeframe,
            side=side,
            condition=validate_condition(raw.get("condition")),
            stop=validate_stop(raw.get("stop")),
            target=validate_target(raw.get("target")),
            quantity=qty,
            name=str(raw.get("name") or "")[:120],
            enabled=bool(raw.get("enabled", True)),
            once=bool(raw.get("once", False)),
            trail=validate_trail(raw.get("trail")),
            exit_condition=validate_condition(raw["exit_condition"]) if raw.get("exit_condition") else None,
            exit_timeframe=validate_timeframe(str(raw["exit_timeframe"])) if raw.get("exit_timeframe") else None,
            exit_condition_2=validate_condition(raw["exit_condition_2"]) if raw.get("exit_condition_2") else None,
            exit_timeframe_2=validate_timeframe(str(raw["exit_timeframe_2"])) if raw.get("exit_timeframe_2") else None,
            entry_score_indicator=_validate_entry_indicator(raw.get("entry_score_indicator")),
            entry_levels=_validate_entry_levels(raw.get("entry_levels"), qty),
            execution=_validate_execution(raw.get("execution")),
        )
    except (ConditionError, ValueError) as e:
        raise InvalidRule(str(e)) from None
    if rule_id:
        rule.id = rule_id
    return rule


def referenced_refs_of(rule: Rule) -> set[tuple[str, Optional[str]]]:
    """(drawing id, rectangle edge) for everything the rule reads: entry, stop, trail and exit."""
    refs = referenced_refs(rule.condition)
    if rule.stop.get("type") == "line":
        refs.add((rule.stop["drawing_id"], rule.stop.get("edge")))
    if rule.trail and rule.trail.get("type") == "line":
        refs.add((rule.trail["drawing_id"], rule.trail.get("edge")))
    if rule.exit_condition:
        refs |= referenced_refs(rule.exit_condition)
    return refs


def referenced_ids(rule: Rule) -> set[str]:
    """Every drawing id the rule's condition or stop depends on."""
    return {i for i, _ in referenced_refs_of(rule)}


def _row_to_rule(r: dict) -> Rule:
    return Rule(
        id=r["id"], symbol=r["symbol"], timeframe=r["timeframe"], name=r["name"], side=r["side"],
        condition=r["condition"], stop=r["stop"], target=r["target"], quantity=r["quantity"],
        enabled=r["enabled"], once=r["once"], trail=r["trail"], exit_condition=r["exit_condition"],
        exit_timeframe=r["exit_timeframe"], exit_condition_2=r.get("exit_condition_2"),
        exit_timeframe_2=r.get("exit_timeframe_2"),
        entry_score_indicator=r.get("entry_score_indicator"),
        entry_levels=r.get("entry_levels"),
        execution=r.get("execution"),
    )


_SAVE_SQL = """
INSERT INTO terminal_rules
    (id, user_id, symbol, timeframe, name, side, condition, stop, target, quantity, enabled, once,
     trail, exit_condition, exit_timeframe, exit_condition_2, exit_timeframe_2,
     entry_score_indicator, entry_levels, execution)
VALUES (:id, :u, :symbol, :tf, :name, :side, CAST(:cond AS JSONB), CAST(:stop AS JSONB),
        CAST(:target AS JSONB), :qty, :enabled, :once,
        CAST(:trail AS JSONB), CAST(:exit AS JSONB), :exit_tf,
        CAST(:exit2 AS JSONB), :exit_tf2,
        CAST(:esi AS JSONB), CAST(:el AS JSONB), CAST(:exe AS JSONB))
ON CONFLICT (id) DO UPDATE SET
    symbol = EXCLUDED.symbol, timeframe = EXCLUDED.timeframe, name = EXCLUDED.name,
    side = EXCLUDED.side, condition = EXCLUDED.condition, stop = EXCLUDED.stop,
    target = EXCLUDED.target, quantity = EXCLUDED.quantity, enabled = EXCLUDED.enabled,
    once = EXCLUDED.once, trail = EXCLUDED.trail, exit_condition = EXCLUDED.exit_condition,
    exit_timeframe = EXCLUDED.exit_timeframe, exit_condition_2 = EXCLUDED.exit_condition_2,
    exit_timeframe_2 = EXCLUDED.exit_timeframe_2,
    entry_score_indicator = EXCLUDED.entry_score_indicator,
    entry_levels = EXCLUDED.entry_levels, execution = EXCLUDED.execution, updated_at = now()
WHERE terminal_rules.user_id = EXCLUDED.user_id
"""


class RuleRepo:
    def __init__(self, db: Database, user: str = LOCAL_USER):
        self.db = db
        self.user = user

    def list(self, symbol: Optional[str] = None, timeframe: Optional[str] = None, enabled_only: bool = False) -> list[Rule]:
        sql = "SELECT * FROM terminal_rules WHERE user_id = :u"
        params: dict[str, Any] = {"u": self.user}
        if symbol:
            sql += " AND symbol = :s"
            params["s"] = symbol
        if timeframe:
            sql += " AND timeframe = :tf"
            params["tf"] = timeframe
        if enabled_only:
            sql += " AND enabled"
        sql += " ORDER BY created_at, id"
        return [_row_to_rule(r) for r in self.db.rows(sql, **params)]

    def get(self, rule_id: str) -> Optional[Rule]:
        r = self.db.row("SELECT * FROM terminal_rules WHERE id = :i AND user_id = :u", i=rule_id, u=self.user)
        return _row_to_rule(r) if r else None

    def save(self, rule: Rule) -> Rule:
        self.db.execute(_SAVE_SQL, **self._params(rule))
        return rule

    def save_many(self, rules: list[Rule]) -> None:
        """Insert or update many rules in one transaction (a strategy applying itself to a whole watchlist)."""
        if rules:
            with self.db.tx() as conn:
                conn.execute(text(_SAVE_SQL), [self._params(r) for r in rules])

    def _params(self, rule: Rule) -> dict:
        return dict(
            id=rule.id, u=self.user, symbol=rule.symbol, tf=rule.timeframe, name=rule.name, side=rule.side,
            cond=jdump(rule.condition), stop=jdump(rule.stop),
            target=jdump(rule.target) if rule.target is not None else None,
            qty=rule.quantity, enabled=rule.enabled, once=rule.once,
            trail=jdump(rule.trail) if rule.trail else None,
            exit=jdump(rule.exit_condition) if rule.exit_condition else None, exit_tf=rule.exit_timeframe,
            exit2=jdump(rule.exit_condition_2) if rule.exit_condition_2 else None,
            exit_tf2=rule.exit_timeframe_2,
            esi=jdump(rule.entry_score_indicator) if rule.entry_score_indicator else None,
            el=jdump(rule.entry_levels) if rule.entry_levels else None,
            exe=jdump(rule.execution) if rule.execution else None,
        )

    def set_enabled(self, rule_id: str, enabled: bool) -> bool:
        return self.db.execute(
            "UPDATE terminal_rules SET enabled = :e, updated_at = now() WHERE id = :i AND user_id = :u",
            e=enabled, i=rule_id, u=self.user,
        ) > 0

    def delete(self, rule_id: str) -> bool:
        return self.db.execute("DELETE FROM terminal_rules WHERE id = :i AND user_id = :u", i=rule_id, u=self.user) > 0


@dataclass
class Signal:
    symbol: str
    timeframe: str
    side: str
    bar_time: datetime
    entry: float
    status: str  # 'open' | 'rejected'
    rule_id: Optional[str] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    reason: str = ""
    id: str = field(default_factory=new_id)

    def to_json(self) -> dict:
        return {
            "id": self.id, "rule_id": self.rule_id, "symbol": self.symbol, "timeframe": self.timeframe,
            "side": self.side, "bar_time": to_epoch(self.bar_time), "entry": self.entry, "stop": self.stop,
            "target": self.target, "status": self.status, "reason": self.reason,
        }


class SignalRepo:
    def __init__(self, db: Database):
        self.db = db

    def insert_if_new(self, s: Signal) -> bool:
        """Record a signal. Returns False (and writes nothing) if this rule already
        fired on this bar - the UNIQUE (rule_id, bar_time) guard against double entries."""
        rows = self.db.write_rows(
            """
            INSERT INTO terminal_signals (id, rule_id, symbol, timeframe, side, bar_time, entry, stop, target, status, reason)
            VALUES (:id, :rule, :symbol, :tf, :side, :bar, :entry, :stop, :target, :status, :reason)
            ON CONFLICT (rule_id, bar_time) DO NOTHING
            RETURNING id
            """,
            id=s.id, rule=s.rule_id, symbol=s.symbol, tf=s.timeframe, side=s.side, bar=s.bar_time,
            entry=s.entry, stop=s.stop, target=s.target, status=s.status, reason=s.reason,
        )
        return bool(rows)

    def list(self, symbol: Optional[str] = None, limit: int = 100) -> list[Signal]:
        sql = "SELECT * FROM terminal_signals"
        params: dict[str, Any] = {"n": max(1, min(limit, 1000))}
        if symbol:
            sql += " WHERE symbol = :s"
            params["s"] = symbol
        sql += " ORDER BY bar_time DESC, created_at DESC LIMIT :n"
        return [
            Signal(id=r["id"], rule_id=r["rule_id"], symbol=r["symbol"], timeframe=r["timeframe"], side=r["side"],
                   bar_time=r["bar_time"], entry=r["entry"], stop=r["stop"], target=r["target"],
                   status=r["status"], reason=r["reason"])
            for r in self.db.rows(sql, **params)
        ]
