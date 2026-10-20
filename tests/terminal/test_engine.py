"""End to end: a drawn line + a rule + candles  ->  a signal, a paper position, and no repainting.

Scenario used throughout: a horizontal resistance line at 100 on SBIN 1H. The rule is
'BUY when the close crosses above the line; stop at the signal candle's low; target 2R'.
"""
from datetime import datetime

import pytest

from trading_service.terminal.drawings import Drawing, DrawingRepo
from trading_service.terminal.engine import RuleEngine
from trading_service.terminal.paper import PaperBook
from trading_service.terminal.rules import RuleRepo, SignalRepo, rule_from_payload
from tests.terminal.helpers import hourly_bars, ist
from tests.terminal.test_rules import payload

SYMBOL = "NSE:SBIN-EQ"
# Mon 09:15 ... : bars 0..3 are Mon 09:15, 10:15, 11:15, 12:15. The close crosses 100 on bar 3 (12:15 bar).
CLOSES = [98.0, 99.0, 100.0, 101.0]
BAR3_START = ist(2026, 10, 19, 12, 15)
BAR3_END = ist(2026, 10, 19, 13, 15)


class Harness:
    def __init__(self, db, closes=CLOSES, rule_over=None):
        self.db = db
        self.bars = hourly_bars(closes)
        self.now = BAR3_END                      # the last bar has just completed
        DrawingRepo(db).save(Drawing(SYMBOL, "horizontal_line", [{"time": 1, "price": 100.0}], id="res"))
        self.rule = RuleRepo(db).save(rule_from_payload(payload(**(rule_over or {}))))
        self.book = PaperBook(db)
        self.engine = RuleEngine(db, lambda s, tf, n: self.bars, self.book, clock=lambda: self.now)

    def close_bar(self, start=BAR3_START):
        return self.engine.on_candle_close(SYMBOL, "1H", start)


def test_a_breakout_on_a_completed_candle_creates_a_signal_and_a_paper_position(db):
    h = Harness(db)
    events = h.close_bar()
    assert [e["type"] for e in events] == ["signal", "position"]
    sig = events[0]["signal"]
    assert (sig["side"], sig["status"], sig["entry"]) == ("BUY", "open", 101.0)
    assert sig["stop"] == 99.5                                # the signal candle's low (open 100, close 101, spread 0.5)
    assert sig["target"] == pytest.approx(101.0 + 2 * (101.0 - 99.5))
    assert sig["reason"] == "close crossed above line (101.00 vs 100.00)"
    pos = events[1]["position"]
    assert (pos["quantity"], pos["entry_price"], pos["stop"], pos["status"]) == (10, 101.0, 99.5, "open")
    assert len(h.book.open_positions()) == 1


def test_nothing_fires_while_the_breakout_candle_is_still_forming(db):
    h = Harness(db)
    h.now = ist(2026, 10, 19, 12, 40)                          # 25 minutes into the 12:15 bar
    assert h.close_bar() == []                                  # the forming bar is dropped; the last completed close is 100
    assert SignalRepo(db).list(SYMBOL) == []
    h.now = BAR3_END                                            # the bar completes
    assert [e["type"] for e in h.close_bar()] == ["signal", "position"]


def test_the_same_closed_bar_never_fires_twice(db):
    h = Harness(db)
    assert len(h.close_bar()) == 2
    assert h.close_bar() == []                                  # replayed close event: no duplicate
    assert len(SignalRepo(db).list(SYMBOL)) == 1 and len(h.book.open_positions()) == 1


def test_a_stale_close_event_for_a_bar_we_do_not_have_yet_is_ignored(db):
    h = Harness(db)
    h.bars = h.bars[:2]                                         # history lags: newest completed bar is 10:15
    assert h.close_bar(BAR3_START) == []


def test_rules_for_other_symbols_timeframes_or_disabled_rules_are_not_evaluated(db):
    h = Harness(db)
    assert h.engine.on_candle_close("NSE:TCS-EQ", "1H", BAR3_START) == []
    assert h.engine.on_candle_close(SYMBOL, "1D", BAR3_START) == []
    RuleRepo(db).set_enabled(h.rule.id, False)
    assert h.close_bar() == []


def test_a_rule_that_is_marked_once_disables_itself_after_it_fires(db):
    h = Harness(db, rule_over={"once": True})
    assert len(h.close_bar()) == 2
    assert RuleRepo(db).get(h.rule.id).enabled is False


def test_a_second_breakout_while_still_in_the_trade_is_recorded_as_rejected(db):
    # Breaks out on bar 3 (enter), dips back under the line, then breaks out again on bar 7 (Tue 09:15).
    h = Harness(db, closes=[98.0, 99.0, 100.0, 101.0, 99.0, 98.0, 100.0, 102.0])
    h.engine.candles = lambda s, tf, n: h.bars[:4]               # history as of the first breakout
    assert len(h.close_bar()) == 2
    h.engine.candles = lambda s, tf, n: h.bars                   # history as of the second
    h.now = ist(2026, 10, 20, 10, 15)
    events = h.engine.on_candle_close(SYMBOL, "1H", ist(2026, 10, 20, 9, 15))
    assert [e["type"] for e in events] == ["signal"]
    assert events[0]["signal"]["status"] == "rejected"
    assert events[0]["signal"]["reason"] == "already in a position from this rule"
    assert len(h.book.open_positions()) == 1


def test_a_stop_on_the_wrong_side_makes_a_rejected_signal_and_no_position(db):
    h = Harness(db, rule_over={"stop": {"type": "fixed", "price": 105.0}})   # above a BUY entry of 101
    [event] = h.close_bar()
    assert event["signal"]["status"] == "rejected" and "must be below the entry" in event["signal"]["reason"]
    assert h.book.open_positions() == []


def test_a_rule_pointing_at_a_deleted_line_is_skipped_quietly(db):
    h = Harness(db)
    DrawingRepo(db).delete("res")
    assert h.close_bar() == []
    assert SignalRepo(db).list(SYMBOL) == []


def test_sell_rules_get_a_stop_above_the_entry(db):
    h = Harness(db, closes=[102.0, 101.0, 100.0, 99.0], rule_over={
        "side": "SELL",
        "condition": {"type": "cross", "direction": "below", "a": {"kind": "close"}, "b": {"kind": "line", "drawing_id": "res"}},
    })
    [sig_event, _pos] = h.close_bar()
    sig = sig_event["signal"]
    assert sig["side"] == "SELL" and sig["entry"] == 99.0
    assert sig["stop"] == 100.5 and sig["target"] == pytest.approx(99.0 - 2 * 1.5)   # the bar's high; 2R below
