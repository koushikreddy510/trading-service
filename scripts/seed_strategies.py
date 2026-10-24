#!/usr/bin/env python3
"""Seed the screener filters and the live strategies (rule template x watchlist). Safe to re-run.

Run on the server (talks to the screener on :8000 and trading-service on :8001, no auth on localhost):
    python3 scripts/seed_strategies.py [--dry-run]

Filter (1D, strategy-screener) picks the stocks; the rule template decides when to enter and how to manage the trade.
Higher timeframes (1H/2H/4H) reuse the 15m template: no time-of-day window, trail and exits on the same timeframe.
"""
import copy
import json
import sys
import urllib.request
from pathlib import Path

SCREENER, TRADING = "http://127.0.0.1:8000", "http://127.0.0.1:8001"
T = json.loads((Path(__file__).parent.parent / "deploy" / "strategy_templates.json").read_text())
DRY = "--dry-run" in sys.argv


def call(method, url, body=None):
    if DRY and method != "GET":
        print("DRY", method, url, json.dumps(body)[:100])
        return {"id": 0}
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read() or b"{}")


def st(direction, period, mult):
    return {"indicator_type": "supertrend", "params": {"period": period, "multiplier": mult}, "lookback_days": 1,
            "operator": "==", "threshold": {"value": direction}}


def ema_vs_close(n, op):
    return {"indicator_type": "ema", "params": {"length": n}, "lookback_days": 1, "operator": op, "threshold": {"field": "close"}}


def rsi(op, v):
    return {"indicator_type": "rsi", "params": {"length": 14}, "lookback_days": 1, "operator": op, "threshold": {"value": v}}


FILTERS = {
    "Dual Supertrend Bullish (5,2 + 10,3)": ("Both supertrends bullish on the daily candle", [st("bullish", 5, 2.0), st("bullish", 10, 3.0)]),
    "Dual Supertrend Bearish (5,2 + 10,3)": ("Both supertrends bearish on the daily candle", [st("bearish", 5, 2.0), st("bearish", 10, 3.0)]),
    "EMA Golden Cross Momentum": ("Close above EMA 50 and 200, RSI above 50", [ema_vs_close(50, "<"), ema_vs_close(200, "<"), rsi(">", 50)]),
    "EMA Death Cross Momentum": ("Close below EMA 50 and 200, RSI below 50", [ema_vs_close(50, ">"), ema_vs_close(200, ">"), rsi("<", 50)]),
}


def variant(key, tf):
    t = copy.deepcopy(T[key])
    if tf == "15m":
        return t
    t["timeframe"] = t["exit_timeframe"] = tf
    if t.get("exit_timeframe_2"):
        t["exit_timeframe_2"] = tf
    t["trail"]["timeframe"] = tf
    t["condition"] = next(c for c in t["condition"]["conditions"] if c["type"] == "any")   # drop the 09:30-14:45 window
    return t


STRATEGIES = []
for tf in ("15m", "1H", "2H", "4H"):
    STRATEGIES.append((f"Dual Supertrend long {tf}", variant("st_long", tf), "Dual Supertrend Bullish (5,2 + 10,3)"))
    STRATEGIES.append((f"Dual Supertrend short {tf}", variant("st_short", tf), "Dual Supertrend Bearish (5,2 + 10,3)"))
STRATEGIES += [("Golden cross 1D", T["golden"], "EMA Golden Cross Momentum"), ("Death cross 1D", T["death"], "EMA Death Cross Momentum")]

have = {s["name"]: s["id"] for s in call("GET", f"{SCREENER}/strategies/")}
ids = {}
for name, (desc, conds) in FILTERS.items():
    ids[name] = have.get(name) or call("POST", f"{SCREENER}/strategies/", {"name": name, "description": desc, "market_type": "stocks", "timeframe": "1D", "conditions": conds})["id"]
    res = call("POST", f"{TRADING}/terminal/screener-sync/apply", {"strategy_id": ids[name], "tolerance_pct": 0, "max_failing": 0, "max_symbols": 40})
    print("filter", name, "->", {k: res.get(k) for k in ("watchlist", "matched", "added") if k in res})
for name, tpl, flt in STRATEGIES:
    out = call("POST", f"{TRADING}/terminal/strategies", {"name": name, "template": tpl, "watchlist": f"Screen: {flt}"})
    print("strategy", name, out)
print(call("POST", f"{TRADING}/terminal/strategies/apply"))
