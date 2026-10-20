"""Drawings: validation, storage, and how a line's price is computed per bar."""
import pytest

from trading_service.terminal.drawings import (
    Drawing, DrawingRepo, InvalidDrawing, anchor_index, line_series, line_value_at, validate_drawing,
)
from trading_service.terminal.timeframes import to_epoch
from tests.terminal.helpers import hourly_bars

SYMBOL = "NSE:SBIN-EQ"


def pt(t, price):
    return {"time": t, "price": price}


# ------------------------------------------------------------------ validation
def test_horizontal_line_needs_one_point_and_trend_line_needs_two_distinct_times():
    validate_drawing(Drawing(SYMBOL, "horizontal_line", [pt(1, 100)]))
    validate_drawing(Drawing(SYMBOL, "trend_line", [pt(1, 100), pt(2, 101)]))
    with pytest.raises(InvalidDrawing, match="needs 2"):
        validate_drawing(Drawing(SYMBOL, "trend_line", [pt(1, 100)]))
    with pytest.raises(InvalidDrawing, match="different times"):
        validate_drawing(Drawing(SYMBOL, "ray", [pt(5, 100), pt(5, 101)]))


@pytest.mark.parametrize("kind, points, message", [
    ("fibonacci", [pt(1, 1)], "kind must be"),
    ("horizontal_line", [pt(0, 100)], "positive"),
    ("horizontal_line", [pt(1, -5)], "positive"),
    ("horizontal_line", [{"time": "x", "price": 1}], "numeric"),
])
def test_invalid_drawings_are_rejected(kind, points, message):
    with pytest.raises(InvalidDrawing, match=message):
        validate_drawing(Drawing(SYMBOL, kind, points))


def test_a_rectangle_needs_two_corners_at_different_times_and_a_text_note_one_point():
    validate_drawing(Drawing(SYMBOL, "rectangle", [pt(1, 100), pt(5, 110)]))
    validate_drawing(Drawing(SYMBOL, "text", [pt(1, 100)], label="gap fill"))
    with pytest.raises(InvalidDrawing, match="needs 2"):
        validate_drawing(Drawing(SYMBOL, "rectangle", [pt(1, 100)]))
    with pytest.raises(InvalidDrawing, match="different times"):
        validate_drawing(Drawing(SYMBOL, "rectangle", [pt(5, 100), pt(5, 110)]))
    with pytest.raises(InvalidDrawing, match="needs 1"):
        validate_drawing(Drawing(SYMBOL, "text", []))


# --------------------------------------------------------------------- storage
def test_drawings_round_trip_and_are_scoped_to_their_symbol(db):
    repo = DrawingRepo(db)
    a = repo.save(Drawing(SYMBOL, "trend_line", [pt(100, 10.5), pt(200, 12)], style={"color": "#f7b955"}, label="resistance"))
    repo.save(Drawing("NSE:TCS-EQ", "horizontal_line", [pt(100, 3500)]))
    mine = repo.list(SYMBOL)
    assert [d.id for d in mine] == [a.id]
    assert mine[0].points == [pt(100, 10.5), pt(200, 12.0)]
    assert mine[0].style == {"color": "#f7b955"} and mine[0].label == "resistance"
    assert len(repo.list()) == 2


def test_saving_an_existing_id_replaces_it_and_delete_reports_whether_it_existed(db):
    repo = DrawingRepo(db)
    d = repo.save(Drawing(SYMBOL, "horizontal_line", [pt(1, 100)]))
    d.points = [pt(1, 105)]
    repo.save(d)
    assert repo.get(d.id).points == [pt(1, 105.0)]
    assert repo.delete(d.id) is True
    assert repo.delete(d.id) is False
    assert repo.get(d.id) is None


def test_another_user_cannot_overwrite_or_delete_my_drawing(db):
    mine = DrawingRepo(db, user="alice").save(Drawing(SYMBOL, "horizontal_line", [pt(1, 100)]))
    bob = DrawingRepo(db, user="bob")
    assert bob.get(mine.id) is None and bob.delete(mine.id) is False
    bob.save(Drawing(SYMBOL, "horizontal_line", [pt(1, 999)], id=mine.id))  # same id, different user: ignored
    assert DrawingRepo(db, user="alice").get(mine.id).points == [pt(1, 100.0)]


# ------------------------------------------------------------------- the maths
def hourly_times(n):
    return [to_epoch(b.time) for b in hourly_bars([100.0] * n)]


def test_horizontal_line_has_the_same_price_everywhere():
    d = Drawing(SYMBOL, "horizontal_line", [pt(1, 123.5)])
    assert line_series(d, hourly_times(5), "1H") == [123.5] * 5


def test_a_rectangle_is_a_pair_of_flat_levels_whichever_corner_was_clicked_first():
    times = hourly_times(5)
    zone = Drawing(SYMBOL, "rectangle", [pt(times[3], 98.0), pt(times[1], 104.0)])   # right-hand, lower corner first
    assert line_series(zone, times, "1H", "top") == [104.0] * 5
    assert line_series(zone, times, "1H", "bottom") == [98.0] * 5


def test_a_rectangle_without_an_edge_and_a_text_note_have_no_price():
    times = hourly_times(3)
    assert line_series(Drawing(SYMBOL, "rectangle", [pt(1, 98.0), pt(2, 104.0)]), times, "1H") == [None] * 3
    assert line_series(Drawing(SYMBOL, "text", [pt(1, 98.0)]), times, "1H", "top") == [None] * 3


def test_anchor_on_a_bar_open_maps_to_that_bar_index_and_extrapolates_beyond_the_ends():
    times = hourly_times(10)
    assert anchor_index(times, times[3], 3600) == 3
    assert anchor_index(times, times[-1] + 7200, 3600) == pytest.approx(9 + 2)      # two bars past the end
    assert anchor_index(times, times[0] - 3600, 3600) == pytest.approx(-1)          # one bar before the start
    assert anchor_index(times, (times[2] + times[3]) // 2, 3600) == pytest.approx(2.5)


def test_trend_line_is_straight_in_bar_space_across_the_overnight_gap():
    # 7 bars per day. Anchors: Mon 10:15 (bar 1, 100) and Tue 10:15 (bar 8, 107) -> +1 per BAR.
    times = hourly_times(20)
    d = Drawing(SYMBOL, "trend_line", [pt(times[1], 100.0), pt(times[8], 107.0)])
    values = line_series(d, times, "1H")
    assert values[1] == pytest.approx(100.0) and values[8] == pytest.approx(107.0)
    assert values[7] == pytest.approx(106.0)   # Tue 09:15, the first bar after the gap: just one step on
    # In wall-clock time the same line would put Tue 09:15 at 100 + 7*(23/24) = 106.71 - not what the chart shows.
    assert values[15] == pytest.approx(114.0)  # extends to the right as a ray


def test_a_line_whose_anchors_fall_on_the_same_bar_has_no_value():
    times = hourly_times(5)
    d = Drawing(SYMBOL, "trend_line", [pt(times[2], 100.0), pt(times[2] + 60, 101.0)])
    # 60s apart inside a 1H bar interpolates to a tiny but non-zero slope; truly identical indices give None.
    assert line_value_at(d, times, 4, 3600) is not None
    d2 = Drawing(SYMBOL, "trend_line", [pt(times[2] + 10, 100.0), pt(times[2] + 10, 101.0)])
    assert line_value_at(d2, times, 4, 3600) is None
