"""Strategies: a rule template applied to every symbol of a watchlist.

The backtester already runs symbol-less rule templates over a universe; this does the same live. `apply` keeps one
ordinary rule per watchlist symbol (named "[S] <strategy>"), so entries, stops, exits, paper positions and Slack
messages are exactly what a hand-made rule gives. A symbol that leaves the list, or a disabled strategy, only switches
its rule off: open positions keep being managed (see runtime), and nothing is deleted.
"""
from __future__ import annotations

from typing import Any, Mapping

from .db import Database, jdump, new_id
from .drawings import LOCAL_USER
from .rules import InvalidRule, RuleRepo, rule_from_payload
from .watchlists import WatchlistRepo

PLACEHOLDER = "NSE:TEMPLATE-EQ"


class StrategyError(ValueError):
    pass


def rule_name(name: str) -> str:
    return f"[S] {name}"


class StrategyRepo:
    def __init__(self, db: Database, user: str = LOCAL_USER):
        self.db, self.user = db, user

    def list(self) -> list[dict]:
        return [dict(r) for r in self.db.rows("SELECT id, name, template, watchlist, enabled FROM terminal_strategies ORDER BY created_at, name")]

    def save(self, raw: Mapping[str, Any]) -> dict:
        name, wl = str(raw.get("name") or "").strip()[:100], str(raw.get("watchlist") or "").strip()
        if not name or not wl:
            raise StrategyError("name and watchlist are required")
        try:
            rule_from_payload({**raw.get("template", {}), "symbol": PLACEHOLDER})   # validate the template once
        except InvalidRule as e:
            raise StrategyError(str(e)) from None
        row = self.db.write_rows(
            """INSERT INTO terminal_strategies (id, name, template, watchlist, enabled)
               VALUES (:id, :n, CAST(:t AS JSONB), :w, :e)
               ON CONFLICT (name) DO UPDATE SET template = EXCLUDED.template, watchlist = EXCLUDED.watchlist, enabled = EXCLUDED.enabled
               RETURNING id""",
            id=new_id(), n=name, t=jdump(raw["template"]), w=wl, e=bool(raw.get("enabled", True)))
        return {"id": row[0]["id"], "name": name}

    def delete(self, strategy_id: str) -> bool:
        return self.db.execute("DELETE FROM terminal_strategies WHERE id = :i", i=strategy_id) > 0

    def apply(self, strategy: Mapping[str, Any], rules: RuleRepo, watchlists: WatchlistRepo) -> dict:
        wl = next((w for w in watchlists.list() if w["name"].lower() == strategy["watchlist"].lower()), None)
        if wl is None:
            raise StrategyError(f"watchlist '{strategy['watchlist']}' not found")
        rname, symbols = rule_name(strategy["name"]), list(wl["symbols"])
        have = {r.symbol: r for r in rules.list() if r.name == rname}
        added = updated = 0
        for sym in symbols:
            payload = {**strategy["template"], "symbol": sym, "name": rname, "enabled": strategy["enabled"]}
            rule = rule_from_payload(payload, have[sym].id if sym in have else None)
            rules.save(rule)
            added, updated = (added + 1, updated) if sym not in have else (added, updated + 1)
        off = 0
        for sym, r in have.items():
            if sym not in symbols and r.enabled:
                rules.set_enabled(r.id, False)
                off += 1
        return {"strategy": strategy["name"], "symbols": len(symbols), "added": added, "updated": updated, "switched_off": off}
