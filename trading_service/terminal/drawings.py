"""Chart drawings (trend lines, rays, horizontal lines) and their price maths.

A drawing is stored per *symbol* as (unix time, price) points, so it shows on every
timeframe. To evaluate a line against candles we convert its anchor times into
**bar-index space** for the timeframe being evaluated, exactly as the chart draws
it. This matters for intraday equities: between Monday 15:30 and Tuesday 09:15
there are 17.75 hours of wall-clock time but zero bars, so a line that is straight
in *time* is not straight on the chart. Rules therefore use the same straight
line the trader sees.

Trend lines are treated as extending to the right when used by a rule: a breakout
rule needs the line's value on the newest bar, which lies beyond the second anchor.
"""
from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .db import Database, jdump, new_id
from .timeframes import tf_seconds

KINDS = ("trend_line", "ray", "horizontal_line", "rectangle", "text")
EDGES = ("top", "bottom")  # a rectangle is a zone: a rule reads one of its two horizontal edges as a level
LOCAL_USER = "local"


class InvalidDrawing(ValueError):
    pass


@dataclass
class Drawing:
    symbol: str
    kind: str
    points: list[dict]  # [{"time": epoch_seconds, "price": float}, ...]
    id: str = field(default_factory=new_id)
    style: dict = field(default_factory=dict)
    label: str = ""

    def to_json(self) -> dict:
        return {"id": self.id, "symbol": self.symbol, "kind": self.kind, "points": self.points, "style": self.style, "label": self.label}


def validate_drawing(d: Drawing) -> Drawing:
    if d.kind not in KINDS:
        raise InvalidDrawing(f"kind must be one of {', '.join(KINDS)}")
    if not d.symbol:
        raise InvalidDrawing("symbol is required")
    needed = 1 if d.kind in ("horizontal_line", "text") else 2
    if len(d.points) < needed:
        raise InvalidDrawing(f"{d.kind} needs {needed} point(s)")
    cleaned = []
    for p in d.points[:needed]:
        try:
            t, price = int(p["time"]), float(p["price"])
        except (KeyError, TypeError, ValueError):
            raise InvalidDrawing("each point needs a numeric time and price") from None
        if t <= 0 or price <= 0:
            raise InvalidDrawing("point time and price must be positive")
        cleaned.append({"time": t, "price": price})
    if needed == 2 and cleaned[0]["time"] == cleaned[1]["time"]:
        raise InvalidDrawing("the two points of a line must be at different times")
    d.points = cleaned
    if not isinstance(d.style, dict):
        raise InvalidDrawing("style must be an object")
    return d


# ---------------------------------------------------------------- price maths
def anchor_index(times: Sequence[int], t: float, step: float) -> float:
    """Map a time onto the bar axis of `times` (ascending bar-open epochs).

    Inside the series the position is interpolated between the surrounding bars
    (exactly the bar index when `t` is a bar open). Outside it extrapolates using
    the nominal bar length `step`.
    """
    n = len(times)
    if n == 0:
        raise ValueError("no bars")
    if t <= times[0]:
        return (t - times[0]) / step
    if t >= times[-1]:
        return (n - 1) + (t - times[-1]) / step
    i = bisect_right(times, t) - 1
    return i + (t - times[i]) / (times[i + 1] - times[i])


def line_value_at(drawing: Drawing, times: Sequence[int], index: float, step: float, edge: Optional[str] = None) -> Optional[float]:
    """Price of the drawing at bar `index` (may be beyond the last bar).

    A rectangle has a price only for the `edge` asked for (a flat level); a text note never has one."""
    if drawing.kind == "horizontal_line":
        return float(drawing.points[0]["price"])
    if drawing.kind == "rectangle":
        prices = [p["price"] for p in drawing.points]
        return {"top": max(prices), "bottom": min(prices)}.get(edge)
    if drawing.kind == "text":
        return None
    p1, p2 = drawing.points[0], drawing.points[1]
    i1 = anchor_index(times, p1["time"], step)
    i2 = anchor_index(times, p2["time"], step)
    if i1 == i2:
        return None
    slope = (p2["price"] - p1["price"]) / (i2 - i1)
    return p1["price"] + slope * (index - i1)


def line_series(drawing: Drawing, times: Sequence[int], tf: str, edge: Optional[str] = None) -> list[Optional[float]]:
    """The drawing's price on every bar of `times` for timeframe `tf`."""
    step = tf_seconds(tf)
    return [line_value_at(drawing, times, i, step, edge) for i in range(len(times))]


# ----------------------------------------------------------------- repository
def _row_to_drawing(r: dict) -> Drawing:
    points = r["points"] if isinstance(r["points"], list) else json.loads(r["points"])
    style = r["style"] if isinstance(r["style"], dict) else json.loads(r["style"])
    return Drawing(id=r["id"], symbol=r["symbol"], kind=r["kind"], points=points, style=style, label=r["label"])


class DrawingRepo:
    def __init__(self, db: Database, user: str = LOCAL_USER):
        self.db = db
        self.user = user

    def list(self, symbol: Optional[str] = None) -> list[Drawing]:
        if symbol:
            rows = self.db.rows(
                "SELECT * FROM terminal_drawings WHERE user_id = :u AND symbol = :s ORDER BY created_at, id",
                u=self.user, s=symbol,
            )
        else:
            rows = self.db.rows("SELECT * FROM terminal_drawings WHERE user_id = :u ORDER BY created_at, id", u=self.user)
        return [_row_to_drawing(r) for r in rows]

    def get(self, drawing_id: str) -> Optional[Drawing]:
        r = self.db.row("SELECT * FROM terminal_drawings WHERE id = :i AND user_id = :u", i=drawing_id, u=self.user)
        return _row_to_drawing(r) if r else None

    def save(self, drawing: Drawing) -> Drawing:
        """Create or replace (upsert by id)."""
        validate_drawing(drawing)
        self.db.execute(
            """
            INSERT INTO terminal_drawings (id, user_id, symbol, kind, points, style, label)
            VALUES (:id, :u, :symbol, :kind, CAST(:points AS JSONB), CAST(:style AS JSONB), :label)
            ON CONFLICT (id) DO UPDATE SET
                symbol = EXCLUDED.symbol, kind = EXCLUDED.kind, points = EXCLUDED.points,
                style = EXCLUDED.style, label = EXCLUDED.label, updated_at = now()
            WHERE terminal_drawings.user_id = EXCLUDED.user_id
            """,
            id=drawing.id, u=self.user, symbol=drawing.symbol, kind=drawing.kind,
            points=jdump(drawing.points), style=jdump(drawing.style), label=drawing.label,
        )
        return drawing

    def delete(self, drawing_id: str) -> bool:
        return self.db.execute("DELETE FROM terminal_drawings WHERE id = :i AND user_id = :u", i=drawing_id, u=self.user) > 0
