"""The golden vectors the frontend replays must match what the Python code produces today.

If this fails you changed an indicator, a bucket rule or the line maths. Regenerate with
`python scripts/make_golden_fixtures.py --also <web>/src/workspace/lib/golden.json`
and update the TypeScript twin so the browser keeps matching the backend.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import make_golden_fixtures as golden  # noqa: E402
from trading_service.terminal.indicators import SPECS  # noqa: E402


def test_the_committed_golden_file_is_current():
    committed = json.loads(golden.DEFAULT_OUT.read_text())
    assert committed == json.loads(json.dumps(golden.build())), "golden.json is stale - regenerate it (see module docstring)"


def test_the_fixture_covers_every_indicator_and_timeframe():
    data = json.loads(golden.DEFAULT_OUT.read_text())
    assert {c["name"] for c in data["indicators"]} == set(SPECS)
    assert {tf for row in data["timeframes"] for tf in row["buckets"]} == {"1m", "5m", "15m", "30m", "1H", "2H", "4H", "1D", "1W"}
