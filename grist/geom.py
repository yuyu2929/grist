"""Grist geometry primitives: exact measures between vector geometry and axis-aligned rectangles.

Conventions (see docs/THEORY.md):
- Rectangles are half-open: [x0, x1) x [y0, y1). A segment lying exactly on the
  right/top edge belongs to the neighbouring cell; the left/bottom edge belongs
  to this cell. This keeps the partition property: measures over all cells of a
  grid sum exactly to the measure over the grid domain.
- Polygons are ring lists (exterior + holes, GeoJSON convention). Ring
  orientation is irrelevant: coverage uses even-odd (crossing parity), which is
  orientation-independent and handles holes naturally.
"""
from __future__ import annotations

import math
from typing import Sequence

Point = tuple[float, float]
Rect = tuple[float, float, float, float]  # (x0, y0, x1, y1)

# Relative noise floor: coverage fragments below this fraction of a cell area
# (resp. cell edge for lengths) are floating-point noise, not geometry.
NOISE_FRACTION = 1e-12


def seg_rect_length(p: Point, q: Point, rect: Rect) -> float:
    """Exact length of segment pq intersected with the half-open rectangle.

    Liang-Bars style parametric clipping against the four half-spaces.
    Boundary-coincident cases follow the half-open rule: a segment running
    exactly along the right/top edge contributes 0 here (it belongs to the
    neighbour), along the left/bottom edge it contributes fully.
    """
    px, py = p
    qx, qy = q
    x0, y0, x1, y1 = rect
    dx = qx - px
    dy = qy - py
    t0, t1 = 0.0, 1.0
    for delta, origin, lo, hi in ((dx, px, x0, x1), (dy, py, y0, y1)):
        if delta > 0.0:
            a = (lo - origin) / delta
            b = (hi - origin) / delta
        elif delta < 0.0:
            a = (hi - origin) / delta
            b = (lo - origin) / delta
        else:
            # Segment parallel to this axis: half-open containment test.
            if origin < lo or origin >= hi:
                return 0.0
            continue
        if a > t0:
            t0 = a
        if b < t1:
            t1 = b
        if t0 >= t1:
            return 0.0
    if t1 <= t0:
        return 0.0
    return math.hypot(dx, dy) * (t1 - t0)


def crossings_at_y(edges: Sequence[tuple[float, float, float, float, float, float]], yc: float) -> list[float]:
    """X coordinates where edges cross the horizontal line y=yc.

    Each edge is (ymin, ymax, x1, y1, x2, y2). The half-open test
    (y1 <= yc < y2) or (y2 <= yc < y1) counts each crossing exactly once,
    including vertices that lie exactly on yc.
    """
    xs: list[float] = []
    for _ymin, _ymax, x1, y1, x2, y2 in edges:
        if (y1 <= yc < y2) or (y2 <= yc < y1):
            xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
    return xs


def parity_intervals(xs: list[float]) -> list[tuple[float, float]]:
    """Pair sorted crossings into even-odd covered intervals (half-open)."""
    xs.sort()
    if len(xs) % 2:
        # Degenerate input (self-touching ring): drop the unpaired crossing
        # rather than emitting a nonsensical interval.
        xs = xs[:-1]
    return [(xs[i], xs[i + 1]) for i in range(0, len(xs), 2)]


def point_in_rings(pt: Point, rings: Sequence[Sequence[Point]]) -> bool:
    """Even-odd point-in-polygon test (PNPOLY-style), orientation-agnostic.

    Only sound when the query point is not exactly on the boundary. Callers
    that rely on it (strip certification) guarantee that precondition.
    """
    x, y = pt
    inside = False
    for ring in rings:
        n = len(ring)
        if n < 3:
            continue
        for k in range(n):
            x1, y1 = ring[k]
            x2, y2 = ring[(k + 1) % n]
            if (y1 > y) != (y2 > y):
                x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                if x < x_cross:
                    inside = not inside
    return inside


def ring_signed_area(ring: Sequence[Point]) -> float:
    """Shoelace signed area (positive = counter-clockwise). Used for validation."""
    s = 0.0
    n = len(ring)
    for k in range(n):
        x1, y1 = ring[k]
        x2, y2 = ring[(k + 1) % n]
        s += x1 * y2 - x2 * y1
    return 0.5 * s
