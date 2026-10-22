"""Paper positions: opening from a signal, tick-driven stop/target, P&L, persistence."""
from datetime import timedelta

import pytest

from trading_service.terminal.paper import PaperBook
from trading_service.terminal.rules import Signal, SignalRepo, RuleRepo, rule_from_payload
from tests.terminal.helpers import ist
from tests.terminal.test_rules import payload

SYMBOL = "NSE:SBIN-EQ"


@pytest.fixture()
def signal(db):
    """Factory for persisted signals, as the engine stores them (positions reference their signal)."""
    counter = iter(range(1, 1000))

    def make(side="BUY", entry=100.0, stop=98.0, target=104.0, rule_id=None):
        sig = Signal(SYMBOL, "1H", side, ist(2026, 10, 19, 10, 15) + timedelta(minutes=next(counter)), entry, "open",
                     rule_id=rule_id, stop=stop, target=target)
        SignalRepo(db).insert_if_new(sig)
        return sig

    return make


def test_a_long_position_is_stopped_out_when_price_touches_the_stop(db, signal):
    book = PaperBook(db)
    p = book.open_from_signal(signal(), quantity=10)
    assert book.on_tick(SYMBOL, 99.0) == []                     # inside the range: nothing happens
    [closed] = book.on_tick(SYMBOL, 98.0)
    assert (closed.status, closed.exit_reason, closed.exit_price) == ("closed", "stop", 98.0)
    assert closed.realized_pnl == pytest.approx(-20.0)          # (98-100) * 10
    assert book.open_positions() == []


def test_a_long_position_takes_profit_at_the_target(db, signal):
    book = PaperBook(db)
    book.open_from_signal(signal(), quantity=10)
    [closed] = book.on_tick(SYMBOL, 104.5)
    assert closed.exit_reason == "target" and closed.realized_pnl == pytest.approx(45.0)


def test_a_gap_through_the_stop_fills_at_the_gap_price_not_the_stop(db, signal):
    book = PaperBook(db)
    book.open_from_signal(signal(), quantity=10)
    [closed] = book.on_tick(SYMBOL, 95.0)                       # opens well below the 98 stop
    assert closed.exit_price == 95.0 and closed.realized_pnl == pytest.approx(-50.0)


def test_shorts_mirror_longs(db, signal):
    book = PaperBook(db)
    book.open_from_signal(signal("SELL", entry=100, stop=102, target=96), quantity=5)
    assert book.on_tick(SYMBOL, 101.5) == []
    [closed] = book.on_tick(SYMBOL, 102.0)
    assert closed.exit_reason == "stop" and closed.realized_pnl == pytest.approx(-10.0)   # (100-102) * 5
    book.open_from_signal(signal("SELL", entry=100, stop=102, target=96), quantity=5)
    [won] = book.on_tick(SYMBOL, 95.0)
    assert won.exit_reason == "target" and won.realized_pnl == pytest.approx(25.0)


def test_ticks_for_other_symbols_are_ignored_and_would_trigger_is_a_cheap_precheck(db, signal):
    book = PaperBook(db)
    book.open_from_signal(signal(), quantity=1)
    assert book.on_tick("NSE:TCS-EQ", 1.0) == []
    assert book.would_trigger(SYMBOL, 99.0) is False
    assert book.would_trigger(SYMBOL, 97.0) is True
    assert book.would_trigger("NSE:TCS-EQ", 1.0) is False


def test_unrealized_pnl_marks_to_the_last_price(db, signal):
    p = PaperBook(db).open_from_signal(signal(), quantity=10)
    assert p.unrealized(103.0) == pytest.approx(30.0)
    assert p.to_json(mark=103.0)["unrealized_pnl"] == pytest.approx(30.0)


def test_manual_close_uses_the_given_price(db, signal):
    book = PaperBook(db)
    p = book.open_from_signal(signal(), quantity=10)
    closed = book.close_manually(p.id, 101.0)
    assert closed.exit_reason == "manual" and closed.realized_pnl == pytest.approx(10.0)
    assert book.close_manually(p.id, 101.0) is None            # already closed


def test_open_positions_survive_a_restart_and_history_lists_closed_ones(db, signal):
    book = PaperBook(db)
    book.open_from_signal(signal(), quantity=10)
    book.open_from_signal(signal(entry=100, stop=90, target=None), quantity=1)
    book.on_tick(SYMBOL, 98.0)                                  # hits the first position's stop only
    restarted = PaperBook(db)
    assert [p.stop for p in restarted.open_positions()] == [90.0]
    assert [p.exit_reason for p in restarted.history()] == ["stop"]


def test_has_open_for_rule_looks_through_the_signal(db, signal):
    rule = RuleRepo(db).save(rule_from_payload(payload()))
    book = PaperBook(db)
    sig = signal(rule_id=rule.id)
    assert book.has_open_for_rule(rule.id) is False
    p = book.open_from_signal(sig, quantity=1)
    assert book.has_open_for_rule(rule.id) is True
    book.close_manually(p.id, 100.0)
    assert book.has_open_for_rule(rule.id) is False


def test_the_stop_path_is_recorded_for_the_chart(db, signal):
    book = PaperBook(db)
    p = book.open_from_signal(signal(stop=98.0), quantity=10, now=ist(2026, 10, 19, 10, 30))
    book.move_stop(p, 99.0, ist(2026, 10, 19, 11, 0))
    book.move_stop(p, 98.5)                                 # looser: ignored, not recorded
    book.move_stop(p, 99.5, ist(2026, 10, 19, 12, 0))
    (line,) = book.trade_lines(SYMBOL)
    assert [s["stop"] for s in line["stop_path"]] == [98.0, 99.0, 99.5]
    assert line["entry_price"] == 100.0 and line["target"] == 104.0 and line["status"] == "open"
