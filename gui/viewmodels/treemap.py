# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Treemap geometry and colour for the "Sektoren" page.  Pure Python (no Qt), so it is tested without a display.

``squarify`` is the usual squarified treemap (Bruls, Huizing, van Wijk): items are placed in rows along the shorter side of the free rectangle so that the
tiles stay close to squares.  The result covers the rectangle exactly (up to float rounding): tile areas are proportional to the values, tiles never overlap,
and a value of zero or less gets no tile (it is never drawn with a negative or invented area).

``layout`` is two-level: the groups (sectors) are placed in the rectangle, then the positions of each group inside the group's inner rectangle (below a
caption strip and a small inset).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

Rect = tuple[float, float, float, float]  # x, y, width, height

NEUTRAL = "#b4bcc6"  # no performance available
GAIN, LOSS = (31, 140, 76), (192, 57, 43)
MID = (122, 130, 138)  # neutral grey the colours fade from (zero change)
SATURATION_PCT = 3.0  # a daily change of +-3 % (or more) has the full colour


@dataclass
class Tile:
    key: str
    value: float
    rect: Rect
    children: list["Tile"] = field(default_factory=list)
    caption_height: float = 0.0


def _worst(row: Sequence[float], side: float) -> float:
    total = sum(row)
    if total <= 0 or side <= 0:
        return float("inf")
    return max(max(side * side * v / (total * total), total * total / (side * side * v)) for v in row)


def squarify(values: Sequence[float], rect: Rect) -> list[Optional[Rect]]:
    """One rectangle per value (same order); ``None`` for a value <= 0 or when the rectangle has no area."""
    x, y, w, h = rect
    result: list[Optional[Rect]] = [None] * len(values)
    order = sorted((i for i, v in enumerate(values) if v is not None and v > 0), key=lambda i: -values[i])
    total = sum(values[i] for i in order)
    if not order or total <= 0 or w <= 0 or h <= 0:
        return result
    scale = w * h / total  # area per unit of value
    areas = {i: values[i] * scale for i in order}
    remaining = list(order)
    while remaining:
        side = min(w, h)
        row = [remaining[0]]
        rest = remaining[1:]
        while rest and _worst([areas[i] for i in row + [rest[0]]], side) <= _worst([areas[i] for i in row], side):
            row.append(rest.pop(0))
        remaining = rest
        row_area = sum(areas[i] for i in row)
        if w >= h:  # lay the row as a column on the left
            col_w = row_area / h if h > 0 else 0.0
            cy = y
            for i in row:
                ih = areas[i] / col_w if col_w > 0 else 0.0
                result[i] = (x, cy, col_w, ih)
                cy += ih
            x, w = x + col_w, w - col_w
        else:  # as a row at the top
            row_h = row_area / w if w > 0 else 0.0
            cx = x
            for i in row:
                iw = areas[i] / row_h if row_h > 0 else 0.0
                result[i] = (cx, y, iw, row_h)
                cx += iw
            y, h = y + row_h, h - row_h
    return result


def inset(rect: Rect, pad: float, caption: float) -> Rect:
    """The inner rectangle of a group: ``pad`` on every side and ``caption`` high strip on top; never negative."""
    x, y, w, h = rect
    return (x + pad, y + pad + caption, max(0.0, w - 2 * pad), max(0.0, h - 2 * pad - caption))


def layout(groups: Sequence[tuple[str, float, Sequence[tuple[str, float]]]], rect: Rect, *, pad: float = 2.0, caption: float = 18.0) -> list[Tile]:
    """``groups`` = ``[(group key, group value, [(position key, position value), ...]), ...]`` -> tiles with their children (groups with no area are skipped).

    The caption strip is only reserved when the group tile is high enough for it (otherwise the positions use the whole inner rectangle).
    """
    rects = squarify([g[1] for g in groups], rect)
    tiles: list[Tile] = []
    for (key, value, members), group_rect in zip(groups, rects):
        if group_rect is None:
            continue
        strip = caption if group_rect[3] >= caption * 2.5 and group_rect[2] >= 40 else 0.0
        tile = Tile(key, value, group_rect, caption_height=strip)
        inner = inset(group_rect, pad if min(group_rect[2], group_rect[3]) > pad * 4 else 0.0, strip)
        child_rects = squarify([m[1] for m in members], inner)
        tile.children = [Tile(m[0], m[1], r) for m, r in zip(members, child_rects) if r is not None]
        tiles.append(tile)
    return tiles


def performance_color(change_pct: Optional[float]) -> str:
    """``#rrggbb``: green for a gain, red for a loss, fading to grey at 0 %; a missing change is the neutral grey (never green or red)."""
    if change_pct is None or change_pct != change_pct:
        return NEUTRAL
    share = max(-1.0, min(1.0, change_pct / SATURATION_PCT))
    target = GAIN if share >= 0 else LOSS
    strength = abs(share)
    rgb = [round(m + (t - m) * strength) for m, t in zip(MID, target)]
    return "#{:02x}{:02x}{:02x}".format(*rgb)
