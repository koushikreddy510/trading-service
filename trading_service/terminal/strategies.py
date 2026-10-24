"""Strategies: a rule template applied to every symbol of a watchlist.

The backtester already runs symbol-less rule templates over a universe; this does the same live. `apply` keeps one
ordinary rule per watchlist symbol (named "[S] <strategy>"), so entries, stops, exits, paper positions and Slack
messages are exactly what a hand-made rule gives. A symbol that leaves the list, or a disabled strategy, only switches
its rule off: open positions keep being managed (see runtime), and nothing is deleted.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

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

    def apply(self, strategy: Mapping[str, Any], rules: RuleRepo, watchlists: WatchlistRepo, existing: Optional[list] = None) -> dict:
        """`existing`: every rule already stored, when the caller applies many strategies and has read them once."""
        wl = next((w for w in watchlists.list() if w["name"].lower() == strategy["watchlist"].lower()), None)
        if wl is None:
            raise StrategyError(f"watchlist '{strategy['watchlist']}' not found")
        rname, symbols = rule_name(strategy["name"]), list(wl["symbols"])
        have = {r.symbol: r for r in (existing if existing is not None else rules.list()) if r.name == rname}
        fresh = [
            rule_from_payload({**strategy["template"], "symbol": sym, "name": rname, "enabled": strategy["enabled"]}, have[sym].id if sym in have else None)
            for sym in symbols
        ]
        rules.save_many(fresh)                                  # one transaction, not one per symbol
        off = 0
        keep = set(symbols)
        for sym, r in have.items():
            if sym not in keep and r.enabled:
                rules.set_enabled(r.id, False)
                off += 1
        updated = sum(1 for sym in symbols if sym in have)
        return {"strategy": strategy["name"], "symbols": len(symbols), "added": len(symbols) - updated, "updated": updated, "switched_off": off}

    def apply_all(self, rules: RuleRepo, watchlists: WatchlistRepo) -> list[dict]:
        """Every strategy against its watchlist; one that cannot be applied reports its error and the rest still run."""
        existing, out = rules.list(), []      # a strategy only looks at its own rules (by name), so one read serves all
        for s in self.list():
            try:
                out.append(self.apply(s, rules, watchlists, existing))
            except (StrategyError, InvalidRule) as e:
                out.append({"strategy": s["name"], "error": str(e)})
        return out
