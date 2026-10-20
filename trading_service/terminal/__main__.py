"""Command line for jobs that are not part of the running service.

    python -m trading_service.terminal scan      # the once-a-day screener scan over 1D candles
    python -m trading_service.terminal schema    # create the terminal tables (also done on service start)
    python -m trading_service.terminal sync-screener --strategy 7 [--tolerance 3] [--max-failing 1] [--max-symbols 100]
                                                     [--name "Swing list"] [--filter '{"source":"ratio","metric":"roce_pct","op":">=","value":15}']
    python -m trading_service.terminal sync-screener --all     # refresh every list that is already linked to a strategy

Schedule `scan` after the data-store's 1D OHLCV job finishes (e.g. weekdays 19:00 IST) and `sync-screener --all`
after the strategy-screener has the same day's candles. Exit code 1 means the job failed.
"""
from __future__ import annotations

import argparse
import json
import sys

from .db import get_db


def make_screener_client():
    """The strategy-screener HTTP client (STRATEGY_SCREENER_URL, default http://127.0.0.1:8000). Tests replace this."""
    from .screener_sync import ScreenerClient

    return ScreenerClient()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m trading_service.terminal")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="run the daily screener scan")
    sub.add_parser("schema", help="create the terminal tables")
    s = sub.add_parser("sync-screener", help="put a strategy-screener strategy's matches (and near-misses) on a watchlist")
    s.add_argument("--strategy", type=int, help="strategy id in the strategy-screener")
    s.add_argument("--all", action="store_true", help="refresh every list already linked to a strategy")
    s.add_argument("--tolerance", type=float, default=None, help="near-miss tolerance in %% of the threshold (default 3)")
    s.add_argument("--max-failing", type=int, default=None, help="how many conditions may be missed (default 1)")
    s.add_argument("--max-symbols", type=int, default=None, help="cap on the list size (default 100)")
    s.add_argument("--name", default=None, help="watchlist name (default 'Screen: <strategy name>')")
    s.add_argument("--filter", action="append", default=[], metavar="JSON", help="financial gate; may be repeated")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "sync-screener" and not (args.all or args.strategy is not None):
        parser.error("sync-screener needs --strategy ID or --all")
    db = get_db()
    db.ensure_schema()
    if args.command == "scan":
        from .screener import run_scan

        print(json.dumps(run_scan(db)))
    elif args.command == "sync-screener":
        return _sync(db, args)
    return 0


def _sync(db, args) -> int:
    from . import screener_sync as sync
    from .watchlists import WatchlistRepo

    repo = WatchlistRepo(db)
    client = make_screener_client()
    try:
        if args.all:
            results = sync.re_sync_all(db, repo, client)
            print(json.dumps({"results": results}))
            return 0 if all(r["ok"] for r in results) else 1
        settings = {k: v for k, v in {"tolerance_pct": args.tolerance, "max_failing": args.max_failing, "max_symbols": args.max_symbols}.items() if v is not None}
        try:
            settings["filters"] = [json.loads(f) for f in args.filter]
        except json.JSONDecodeError as e:
            raise sync.SyncError(f"--filter is not valid JSON: {e}") from None
        res = sync.sync_strategy(db, repo, client, args.strategy, name=args.name, **settings)
    except sync.SyncError as e:
        print(f"sync-screener failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps({
        "watchlist": res["watchlist"]["name"], "created": res["created"], "matched": res["matched"], "near": res["near"],
        "added": res["added"], "removed": res["removed"], "kept_for_trading": res["kept_for_trading"],
        "truncated": res["truncated"], "asof": res["asof"],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
