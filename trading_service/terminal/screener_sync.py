"""Turn a strategy-screener strategy into a watchlist: "everything that qualifies, plus what is nearly there".

The strategy-screener repo answers a yes/no question per stock ("do ALL conditions hold on the latest
1D candle?"). A stock that misses one condition by half a percent is just as interesting to watch, so this
module adds a tolerance on top, WITHOUT changing the screener:

    strategy-screener  GET /strategies/{id}   -> the conditions (indicator, operator, threshold)
                       GET /run/{id}          -> every scanned stock with its latest indicator values + matched flag
                                   |
                                   v   (this module)
        for each unmatched stock, measure how far each failing condition is from its threshold:
              gap % = |threshold - value| / |threshold| * 100
        keep it when every failing condition is within `tolerance_pct`  (and at most `max_failing` of them fail)
                                   |
                                   v
        optional financial gates (latest quarter / latest year / ratios, from the data-store tables)
                                   |
                                   v
        a managed watchlist:  matched stocks first, then near-misses ordered by how close they are

Nothing here talks to the broker or places orders. See `docs/` in the repo root for how it fits together.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .db import Database, jdump
from .financials import FIELDS, RATIOS, get_financials
from .watchlists import MAX_ITEMS, MAX_NAME, WatchlistError, WatchlistRepo

log = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_TOLERANCE_PCT = 3.0
DEFAULT_MAX_FAILING = 1
DEFAULT_MAX_SYMBOLS = 100       # a browser may stream at most 200 symbols, so a synced list stays well under that
PAGE_SIZE = 50                  # the largest page the screener's /run accepts
MAX_PAGES = 400                 # 20,000 stocks: a runaway guard, not a real limit
LEVEL_OPS = {">", ">=", "<", "<="}
CROSS_OPS = {"cross_above", "cross_below"}
# What "1%" means when the threshold is 0 (so a % OF the threshold is undefined). Bollinger %B is a fraction of the band,
# RSI / stochastic live on a 0-100 scale; anything else (MACD, ATR...) is in price units, so the close is the scale.
ZERO_THRESHOLD_SCALE = {"bollinger": 1.0, "rsi": 100.0, "stochastic": 100.0}
FILTER_OPS = {">", ">=", "<", "<="}
FILTER_SOURCES = {"ratio": RATIOS, "quarter": FIELDS, "year": FIELDS}
NON_METRICS = {"period", "period_end_date"}


class SyncError(ValueError):
    """Bad settings, or the strategy / list the request names does not work."""


class ScreenerUnavailable(SyncError):
    """The strategy-screener could not be reached or answered with an error (the API reports 502)."""


# ======================================================================= screener client
class ScreenerClient:
    """Minimal HTTP client for the strategy-screener API. Tests swap in a fake with the same `get`."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 120.0, session: Any = None):
        self.base_url = (base_url or os.getenv("STRATEGY_SCREENER_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout
        self._session = session

    def get(self, path: str, **params: Any) -> Any:
        return self._call("get", path, **params)

    def post(self, path: str, **params: Any) -> Any:
        return self._call("post", path, **params)

    def _call(self, method: str, path: str, **params: Any) -> Any:
        import requests

        session = self._session or requests
        try:
            resp = getattr(session, method)(f"{self.base_url}{path}", params={k: v for k, v in params.items() if v is not None}, timeout=self.timeout)
        except Exception as e:  # connection refused, timeout, DNS...
            raise ScreenerUnavailable(f"strategy-screener is not reachable at {self.base_url}: {e}") from None
        if resp.status_code == 404:
            raise SyncError(f"strategy-screener has no {path} (404) - is the strategy id right?")
        if resp.status_code != 200:
            raise ScreenerUnavailable(f"strategy-screener answered HTTP {resp.status_code} for {path}")
        try:
            return resp.json()
        except ValueError:
            raise ScreenerUnavailable(f"strategy-screener answered something that is not JSON for {path}") from None


def list_strategies(client: Any) -> list[dict]:
    """Strategies with a readable one-line description of each condition."""
    rows = client.get("/strategies/")
    return [_strategy_summary(s) for s in rows or []]


def fetch_strategy(client: Any, strategy_id: int) -> dict:
    s = client.get(f"/strategies/{int(strategy_id)}")
    if not isinstance(s, dict) or "conditions" not in s:
        raise SyncError(f"strategy {strategy_id} came back without conditions")
    return s


def _strategy_summary(s: dict) -> dict:
    return {
        "id": s["id"], "name": s.get("name", ""), "description": s.get("description") or "",
        "is_active": s.get("is_active", True), "market_type": s.get("market_type", "stocks"),
        "timeframe": s.get("timeframe", "1D"),
        "conditions": [describe_condition(c) for c in s.get("conditions", [])],
    }


def fetch_entries(client: Any, strategy_id: int, page_size: int = PAGE_SIZE, max_pages: int = MAX_PAGES) -> tuple[list[dict], dict]:
    """Every scanned stock (matched or not) of the screener's latest run, paging through /run.

    `matched_only=false` is what makes the unmatched stocks, with their indicator values, available."""
    entries: list[dict] = []
    total = None
    page = 1
    while page <= max_pages:
        body = client.get(f"/run/{int(strategy_id)}", page=page, page_size=page_size, matched_only="false")
        if not isinstance(body, dict) or "matches" not in body:
            raise SyncError("strategy-screener /run answered an unexpected shape (no 'matches')")
        batch = body["matches"] or []
        entries.extend(batch)
        total = body.get("total", total)
        if not batch or (total is not None and page * page_size >= total):
            break
        page += 1
    meta = {"pages": page, "total_scanned": body.get("total_scanned", len(entries)), "total_matched": body.get("total_matched")}
    return entries, meta


# ===================================================================== condition maths
def indicator_label(cond: dict) -> str:
    """The key the screener files an indicator's latest value under (screener_engine.compute_indicator_values)."""
    label = str(cond["indicator_type"]).upper()
    params = cond.get("params") or {}
    if params:
        label += "(" + ", ".join(f"{v}" for v in params.values()) + ")"
    return label


def _indicator_value(ind: dict, cond: dict, suffix: str = "") -> Any:
    """The screener's latest value for this condition's indicator. Looks the label up exactly; if the parameter order
    differs (the DB may store JSON keys in another order than the screener built the label from), matches on the same
    indicator with the same parameter values in any order."""
    label = indicator_label(cond)
    if label + suffix in ind:
        return ind[label + suffix]
    head = str(cond["indicator_type"]).upper()
    want = sorted(f"{v}" for v in (cond.get("params") or {}).values())
    for key, value in ind.items():
        if suffix and not key.endswith(suffix):
            continue
        base = key[: -len(suffix)] if suffix else key
        if base.endswith(")") and base.startswith(head + "(") and sorted(base[len(head) + 1:-1].split(", ")) == want:
            return value
    return None


def describe_condition(cond: dict) -> str:
    th = cond.get("threshold") or {}
    right = th["value"] if "value" in th else (th.get("field") or "its own value")
    return f"{indicator_label(cond)} {cond['operator'].replace('_', ' ')} {right}"


@dataclass
class Gap:
    """One condition measured against one stock."""
    condition: str
    passed: bool
    gap_pct: Optional[float] = None   # how far from passing, in % of the threshold; None = cannot be measured
    note: str = ""


def _number(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None   # NaN -> None


def measure(cond: dict, entry: dict) -> Gap:
    """Does this stock pass this condition on the latest candle, and if not, how far off is it?

    Level conditions (> >= < <=) are measured exactly. `cross_above`/`cross_below` need the previous candle,
    which /run does not return, so they can be judged only from the level: below the line for cross_above
    (or above it for cross_below) counts as "about to cross"; already on the far side counts as failed
    (it crossed earlier, so it is not a fresh cross). `==` on supertrend / parabolic SAR compares direction
    and has no distance."""
    name = describe_condition(cond)
    op = cond["operator"]
    th = cond.get("threshold") or {}
    ind = entry.get("indicators") or {}
    label = indicator_label(cond)

    if op == "==":
        have = _indicator_value(ind, cond, "_dir")
        if have is None or "value" not in th:
            return Gap(name, False, None, "direction not available")
        ok = str(have) == str(th["value"])
        return Gap(name, ok, 0.0 if ok else None, "" if ok else f"direction is {have}")

    latest = _number(_indicator_value(ind, cond))
    if latest is None:
        return Gap(name, False, None, f"no {label} value")
    if "value" in th:
        target = _number(th["value"])
    elif "field" in th:
        target = _number(entry.get(th["field"]))
    else:
        target = None
    if target is None:
        return Gap(name, False, None, "no threshold to compare with")

    # Which side of the threshold passes?
    wants_above = op in (">", ">=") or op == "cross_above"
    if op in LEVEL_OPS:
        passed = {">": latest > target, ">=": latest >= target, "<": latest < target, "<=": latest <= target}[op]
    elif op in CROSS_OPS:
        passed = False        # cannot be confirmed from one snapshot; see the docstring
    else:
        return Gap(name, False, None, f"operator {op!r} is not measurable")
    if passed:
        return Gap(name, True, 0.0)

    wrong_side = latest <= target if wants_above else latest >= target
    if op in CROSS_OPS and not wrong_side:
        return Gap(name, False, None, "already past the line (crossed earlier)")
    scale = abs(target) or ZERO_THRESHOLD_SCALE.get(str(cond["indicator_type"]).lower()) or abs(_number(entry.get("close")) or 0.0)
    if not scale:
        return Gap(name, False, None, "no scale to measure the distance")
    gap = abs(target - latest) / scale * 100
    word = "below" if wants_above else "above"
    return Gap(name, False, round(gap, 2), f"{label} {latest:g} is {gap:.1f}% {word} {target:g}")


@dataclass
class Fit:
    status: str                       # "matched" | "near" | "out"
    gap_pct: Optional[float]          # worst failing gap (0 for matched)
    failing: list[Gap] = field(default_factory=list)
    reason: str = ""


def classify(entry: dict, conditions: list[dict], tolerance_pct: float, max_failing: int) -> Fit:
    """matched (the screener says so) / near (a few conditions missed, each by <= tolerance) / out."""
    if entry.get("matched"):
        return Fit("matched", 0.0, [], "meets every condition")
    gaps = [measure(c, entry) for c in conditions]
    failing = [g for g in gaps if not g.passed]
    if len(failing) > max_failing or any(g.gap_pct is None or g.gap_pct > tolerance_pct for g in failing):
        return Fit("out", None, failing)
    worst = max((g.gap_pct or 0.0 for g in failing), default=0.0)
    reason = "; ".join(g.note for g in failing) or "meets every condition on today's values"
    return Fit("near", worst, failing, reason)


# ============================================================================ the plan
@dataclass
class Candidate:
    symbol: str
    status: str
    gap_pct: float
    reason: str
    close: Optional[float] = None
    change_pct: Optional[float] = None
    company: str = ""
    sector: str = ""
    market_cap: float = 0.0
    notes: list[str] = field(default_factory=list)   # e.g. "no financials on file"


@dataclass
class SyncPlan:
    strategy_id: int
    strategy_name: str
    conditions: list[str]
    tolerance_pct: float
    max_failing: int
    max_symbols: int
    filters: list[dict]
    asof: Optional[str]
    scanned: int
    matched_total: int
    near_total: int
    rejected_by_financials: int
    truncated: bool
    candidates: list[Candidate]

    def to_json(self) -> dict:
        return asdict(self)

    @property
    def symbols(self) -> list[str]:
        return [c.symbol for c in self.candidates]


def clean_settings(tolerance_pct: Any = DEFAULT_TOLERANCE_PCT, max_failing: Any = DEFAULT_MAX_FAILING,
                   max_symbols: Any = DEFAULT_MAX_SYMBOLS, filters: Any = None) -> tuple[float, int, int, list[dict]]:
    try:
        tol, fails, cap = float(tolerance_pct), int(max_failing), int(max_symbols)
    except (TypeError, ValueError):
        raise SyncError("tolerance_pct, max_failing and max_symbols must be numbers") from None
    if not 0 <= tol <= 50:
        raise SyncError("tolerance_pct must be between 0 and 50")
    if not 0 <= fails <= 10:
        raise SyncError("max_failing must be between 0 and 10")
    if not 1 <= cap <= MAX_ITEMS:
        raise SyncError(f"max_symbols must be between 1 and {MAX_ITEMS}")
    return tol, fails, cap, validate_filters(filters)


def validate_filters(filters: Any) -> list[dict]:
    out: list[dict] = []
    for i, f in enumerate(filters or []):
        if not isinstance(f, dict):
            raise SyncError(f"financial filter {i + 1} must be an object")
        source, metric, op = f.get("source"), f.get("metric"), f.get("op")
        if source not in FILTER_SOURCES:
            raise SyncError(f"financial filter {i + 1}: source must be one of {sorted(FILTER_SOURCES)}")
        if metric not in FILTER_SOURCES[source] or metric in NON_METRICS:
            raise SyncError(f"financial filter {i + 1}: {metric!r} is not a {source} metric")
        if op not in FILTER_OPS:
            raise SyncError(f"financial filter {i + 1}: op must be one of {sorted(FILTER_OPS)}")
        value = _number(f.get("value"))
        if value is None:
            raise SyncError(f"financial filter {i + 1}: value must be a number")
        missing = f.get("on_missing", "keep")
        if missing not in ("keep", "drop"):
            raise SyncError(f"financial filter {i + 1}: on_missing must be 'keep' or 'drop'")
        out.append({"source": source, "metric": metric, "op": op, "value": value, "on_missing": missing})
    return out


def apply_filters(fin: dict, filters: list[dict]) -> tuple[bool, list[str]]:
    """(keep?, notes). `fin` is financials.get_financials(). A metric with no data follows the filter's
    on_missing ('keep' by default, so a stock the scraper has not covered yet is not silently lost)."""
    notes: list[str] = []
    for f in filters:
        if f["source"] == "ratio":
            have = (fin.get("ratios") or {}).get(f["metric"])
        else:
            rows = fin.get("quarterly" if f["source"] == "quarter" else "annual") or []
            have = rows[-1].get(f["metric"]) if rows else None   # rows are oldest first
        have = _number(have)
        label = f"{f['source']} {f['metric']} {f['op']} {f['value']:g}"
        if have is None:
            if f["on_missing"] == "drop":
                return False, [f"{label}: no data"]
            notes.append(f"{label}: no data, kept")
            continue
        ok = {">": have > f["value"], ">=": have >= f["value"], "<": have < f["value"], "<=": have <= f["value"]}[f["op"]]
        if not ok:
            return False, [f"{label}: is {have:g}"]
    return True, notes


def build_plan(
    client: Any,
    strategy_id: int,
    tolerance_pct: Any = DEFAULT_TOLERANCE_PCT,
    max_failing: Any = DEFAULT_MAX_FAILING,
    max_symbols: Any = DEFAULT_MAX_SYMBOLS,
    filters: Any = None,
    financials: Optional[Callable[[str], dict]] = None,
) -> SyncPlan:
    """Ask the screener, measure the near-misses, apply financial gates. Reads only; writes nothing.

    `financials(symbol)` returns what `financials.get_financials` returns; it is needed only when `filters`
    is non-empty."""
    tol, fails, cap, flt = clean_settings(tolerance_pct, max_failing, max_symbols, filters)
    if flt and financials is None:
        raise SyncError("financial filters need a financials lookup")
    strategy = fetch_strategy(client, strategy_id)
    conditions = strategy.get("conditions") or []
    if not conditions:
        raise SyncError(f"strategy {strategy_id} has no conditions, so it would select every stock")
    if strategy.get("market_type", "stocks") != "stocks":
        raise SyncError(f"strategy {strategy_id} is for {strategy['market_type']}, the terminal watches equities")
    entries, meta = fetch_entries(client, strategy_id)

    found: list[Candidate] = []
    for e in entries:
        symbol = e.get("symbol")
        if not symbol:
            continue
        fit = classify(e, conditions, tol, fails)
        if fit.status == "out":
            continue
        found.append(Candidate(
            symbol=str(symbol).upper(), status=fit.status, gap_pct=fit.gap_pct or 0.0, reason=fit.reason,
            close=e.get("close"), change_pct=e.get("change_pct"), company=e.get("company_name") or "",
            sector=e.get("sector") or "", market_cap=float(e.get("market_cap") or 0),
        ))
    # matched first (largest companies first), then the near-misses, closest first
    found.sort(key=lambda c: (0, -c.market_cap, c.symbol) if c.status == "matched" else (1, c.gap_pct, -c.market_cap, c.symbol))

    rejected = 0
    if flt:
        kept: list[Candidate] = []
        for c in found:
            ok, notes = apply_filters(financials(c.symbol), flt)  # type: ignore[misc]
            if ok:
                c.notes = notes
                kept.append(c)
            else:
                rejected += 1
        found = kept

    matched_total = sum(1 for c in found if c.status == "matched")
    asof = max((str(e.get("date"))[:10] for e in entries if e.get("date")), default=None)
    return SyncPlan(
        strategy_id=int(strategy_id), strategy_name=strategy.get("name", ""), conditions=[describe_condition(c) for c in conditions],
        tolerance_pct=tol, max_failing=fails, max_symbols=cap, filters=flt, asof=asof,
        scanned=len(entries), matched_total=matched_total, near_total=len(found) - matched_total,
        rejected_by_financials=rejected, truncated=len(found) > cap, candidates=found[:cap],
    )


# ============================================================================ the write
def _name_for(plan: SyncPlan) -> str:
    return f"Screen: {plan.strategy_name or plan.strategy_id}"[:MAX_NAME]


def link_for_strategy(db: Database, strategy_id: int, user: str) -> Optional[dict]:
    return db.row(
        "SELECT l.* FROM terminal_screener_links l JOIN terminal_watchlists w ON w.id = l.watchlist_id "
        "WHERE l.strategy_id = :s AND w.user_id = :u",
        s=int(strategy_id), u=user,
    )


def protected_symbols(db: Database) -> set[str]:
    """Symbols a sync must never take off a list: they have an open paper position or an enabled rule."""
    rows = db.rows(
        "SELECT symbol FROM terminal_paper_positions WHERE status = 'open' "
        "UNION SELECT symbol FROM terminal_rules WHERE enabled"
    )
    return {r["symbol"] for r in rows}


def apply_plan(db: Database, repo: WatchlistRepo, plan: SyncPlan, name: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """Make the managed watchlist match the plan.

    * first run: creates the list (`Screen: <strategy>`, or `name`) and links it to the strategy;
    * later runs: adds new qualifiers, takes off stocks that no longer qualify, refreshes the reason / gap;
    * symbols the user added by hand are never removed, and neither are symbols with an open paper position
      or an enabled rule (they stay on the list and are reported as `kept_for_trading`)."""
    now = now or datetime.now(timezone.utc)
    link = link_for_strategy(db, plan.strategy_id, repo.user)
    if link:
        wl = repo.get(link["watchlist_id"])
    else:
        wl = None
    if wl is None:
        wanted_name = (name or _name_for(plan)).strip()
        try:
            wl = repo.create(wanted_name)
        except WatchlistError as e:
            raise SyncError(f"{e}; choose another name for the synced watchlist") from None
    wid = wl["id"]
    on_list = set(wl["symbols"])
    old = {r["symbol"]: r for r in db.rows("SELECT symbol, managed FROM terminal_screener_members WHERE watchlist_id = :w", w=wid)}
    new_symbols = plan.symbols
    new_set = set(new_symbols)

    to_add = [s for s in new_symbols if s not in on_list]
    if len(on_list) + len(to_add) > MAX_ITEMS:
        raise SyncError(f"the list would exceed {MAX_ITEMS} symbols; lower max_symbols or remove some manual symbols")

    guard = protected_symbols(db)
    gone = [s for s, r in old.items() if r["managed"] and s not in new_set]
    removed, kept_for_trading = [], []
    for s in gone:
        if s in guard:
            kept_for_trading.append(s)
        elif repo.remove(wid, s):
            removed.append(s)

    added = repo.add(wid, to_add) if to_add else []
    added_set = set(added)
    for c in plan.candidates:
        managed = c.symbol in added_set or bool(old.get(c.symbol, {}).get("managed"))
        db.execute(
            "INSERT INTO terminal_screener_members (watchlist_id, symbol, status, gap_pct, reason, managed, updated_at) "
            "VALUES (:w, :s, :st, :g, :r, :m, :t) "
            "ON CONFLICT (watchlist_id, symbol) DO UPDATE SET status = :st, gap_pct = :g, reason = :r, managed = :m, updated_at = :t",
            w=wid, s=c.symbol, st=c.status, g=c.gap_pct, r=c.reason, m=managed, t=now,
        )
    # forget members that left the list (and symbols kept only because they are being traded become plain manual ones)
    db.execute(
        "DELETE FROM terminal_screener_members WHERE watchlist_id = :w AND NOT (symbol = ANY(:keep))", w=wid, keep=list(new_set)
    )
    repo.reorder(wid, new_symbols)
    db.execute(
        "INSERT INTO terminal_screener_links (watchlist_id, strategy_id, strategy_name, tolerance_pct, max_failing, max_symbols, "
        "filters, last_run_at, last_asof, matched_count, near_count) "
        "VALUES (:w, :sid, :sn, :tol, :mf, :ms, CAST(:f AS JSONB), :t, :asof, :mc, :nc) "
        "ON CONFLICT (watchlist_id) DO UPDATE SET strategy_name = :sn, tolerance_pct = :tol, max_failing = :mf, max_symbols = :ms, "
        "filters = CAST(:f AS JSONB), last_run_at = :t, last_asof = :asof, matched_count = :mc, near_count = :nc",
        w=wid, sid=plan.strategy_id, sn=plan.strategy_name, tol=plan.tolerance_pct, mf=plan.max_failing, ms=plan.max_symbols,
        f=jdump(plan.filters), t=now, asof=plan.asof, mc=plan.matched_total, nc=plan.near_total,
    )
    return {
        "watchlist": repo.get(wid), "created": link is None, "added": added, "removed": removed,
        "kept_for_trading": kept_for_trading, "matched": plan.matched_total, "near": plan.near_total,
        "truncated": plan.truncated, "asof": plan.asof,
    }


def sync_strategy(db: Database, repo: WatchlistRepo, client: Any, strategy_id: int, name: Optional[str] = None, **settings: Any) -> dict:
    """build_plan + apply_plan in one call (what the CLI and the 'Sync' button use)."""
    plan = build_plan(client, strategy_id, financials=lambda s: get_financials(db, s), **settings)
    result = apply_plan(db, repo, plan, name=name)
    result["plan"] = plan.to_json()
    return result


def re_sync_all(db: Database, repo: WatchlistRepo, client: Any) -> list[dict]:
    """Refresh every linked list with the settings it was created with (the scheduled daily job)."""
    out = []
    for link in db.rows("SELECT l.strategy_id, l.tolerance_pct, l.max_failing, l.max_symbols, l.filters FROM terminal_screener_links l "
                        "JOIN terminal_watchlists w ON w.id = l.watchlist_id WHERE w.user_id = :u", u=repo.user):
        try:
            res = sync_strategy(db, repo, client, link["strategy_id"], tolerance_pct=link["tolerance_pct"], max_failing=link["max_failing"],
                                max_symbols=link["max_symbols"], filters=link["filters"])
            out.append({"strategy_id": link["strategy_id"], "ok": True, "added": len(res["added"]), "removed": len(res["removed"]),
                        "matched": res["matched"], "near": res["near"]})
        except SyncError as e:
            log.warning("sync of strategy %s failed: %s", link["strategy_id"], e)
            out.append({"strategy_id": link["strategy_id"], "ok": False, "error": str(e)})
    return out


# ============================================================================ reading back
def list_links(db: Database, user: str) -> list[dict]:
    links = db.rows(
        "SELECT l.*, w.name AS watchlist_name FROM terminal_screener_links l JOIN terminal_watchlists w ON w.id = l.watchlist_id "
        "WHERE w.user_id = :u ORDER BY w.position, w.name", u=user,
    )
    out = []
    for l in links:
        members = db.rows(
            "SELECT symbol, status, gap_pct, reason, managed FROM terminal_screener_members WHERE watchlist_id = :w "
            "ORDER BY (status = 'matched') DESC, gap_pct NULLS LAST, symbol", w=l["watchlist_id"],
        )
        out.append({
            "watchlist_id": l["watchlist_id"], "watchlist_name": l["watchlist_name"], "strategy_id": l["strategy_id"],
            "strategy_name": l["strategy_name"], "tolerance_pct": l["tolerance_pct"], "max_failing": l["max_failing"],
            "max_symbols": l["max_symbols"], "filters": l["filters"],
            "last_run_at": l["last_run_at"].isoformat() if l["last_run_at"] else None, "last_asof": l["last_asof"],
            "matched_count": l["matched_count"], "near_count": l["near_count"], "members": members,
        })
    return out


def unlink(db: Database, watchlist_id: str, user: str) -> bool:
    """Stop syncing a list. The list and its symbols stay; it simply becomes an ordinary watchlist."""
    ok = db.execute(
        "DELETE FROM terminal_screener_links WHERE watchlist_id = :w AND watchlist_id IN (SELECT id FROM terminal_watchlists WHERE user_id = :u)",
        w=watchlist_id, u=user,
    ) > 0
    if ok:
        db.execute("DELETE FROM terminal_screener_members WHERE watchlist_id = :w", w=watchlist_id)
    return ok
