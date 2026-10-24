"""Coverage kernel: exact cell-integral measures for points, lines and polygons.

Polygon kernel: SCCI (Strip Certification & Clipping Integration), sweep form
(see docs/THEORY.md). Per row strip, swept BOTTOM-UP:

  - maintain the ACTIVE edge set (edges whose y-range intersects the strip);
  - empty active set -> certified strip: the covered x-set is constant across
    the strip, so one parity test fills the whole bbox row span exactly;
  - otherwise the strip is clipped once and every candidate cell is either
    "clean" (no fragment edge enters it -> parity test) or exactly clipped
    (Sutherland-Hodgman in x + signed shoelace on depth-normalized rings).

The yc-sampling shortcut for uncertified strips is WRONG for sloped edges:
the covered x-set varies with y inside the strip (DEVLOG.md bug #2). Only
certified strips admit point evaluation; everything else is clipped exactly.
"""
from __future__ import annotations

import math
from typing import Sequence

from .geom import NOISE_FRACTION, Point, crossings_at_y, parity_intervals, point_in_rings, ring_signed_area
from .grid import Grid

Polygon = list[list[Point]]      # one polygon: [exterior, hole, ...]
Polyline = list[Point]

# Edge tuple: (ymin, ymax, x1, y1, x2, y2)
Edge = tuple[float, float, float, float, float, float]


# --------------------------------------------------------------------------
# points and lines (exact by construction)
# --------------------------------------------------------------------------

def point_counts(points: Sequence[Point], grid: Grid) -> dict[tuple[int, int], int]:
    """Exact per-cell point counts (half-open rule). Out-of-domain points are
    skipped; callers report them separately."""
    out: dict[tuple[int, int], int] = {}
    for pt in points:
        c = grid.point_cell(pt[0], pt[1])
        if c is not None:
            out[c] = out.get(c, 0) + 1
    return out


def line_lengths(polylines: Sequence[Polyline], grid: Grid) -> dict[tuple[int, int], float]:
    """Exact per-cell line length (segment-rectangle parametric clipping)."""
    from .geom import seg_rect_length

    out: dict[tuple[int, int], float] = {}
    noise = NOISE_FRACTION * grid.cell
    for line in polylines:
        for k in range(len(line) - 1):
            p = line[k]
            q = line[k + 1]
            if p == q:
                continue
            bc = grid.bbox_cells((min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1])))
            if bc is None:
                continue
            i0, i1, j0, j1 = bc
            for j in range(j0, j1 + 1):
                for i in range(i0, i1 + 1):
                    L = seg_rect_length(p, q, grid.cell_rect(i, j))
                    if L > noise:
                        key = (i, j)
                        out[key] = out.get(key, 0.0) + L
    return out


# --------------------------------------------------------------------------
# polygon kernel
# --------------------------------------------------------------------------

def polygon_areas(polys: Sequence[Polygon], grid: Grid) -> dict[tuple[int, int], float]:
    """Exact per-cell polygon area via SCCI. `polys` are ring lists of one
    logical feature (MultiPolygon parts share the caller's fid)."""
    out: dict[tuple[int, int], float] = {}
    for rings in polys:
        _one_polygon(rings, grid, out)
    return out


def _one_polygon(rings: Sequence[Sequence[Point]], grid: Grid, out: dict[tuple[int, int], float]) -> None:
    rings = _normalize_rings(rings)
    if not rings:
        return
    xs_all = [p[0] for r in rings for p in r]
    ys_all = [p[1] for r in rings for p in r]
    bc = grid.bbox_cells((min(xs_all), min(ys_all), max(xs_all), max(ys_all)))
    if bc is None:
        return
    i0, i1, j0, j1 = bc

    # Sweep edge list (orientation is irrelevant here).
    edges: list[Edge] = []
    for ring in rings:
        n = len(ring)
        for k in range(n):
            x1, y1 = ring[k]
            x2, y2 = ring[(k + 1) % n]
            if y1 != y2:
                edges.append((y1 if y1 < y2 else y2, y1 if y1 > y2 else y2, x1, y1, x2, y2))
    edges.sort(key=lambda e: e[0])

    c = grid.cell
    cell_area = c * c
    noise = NOISE_FRACTION * cell_area
    xmid = 0.5 * (min(xs_all) + max(xs_all))
    n_edges = len(edges)
    ptr = 0
    active: list[Edge] = []

    # Rows are swept BOTTOM-UP: only then are both sweep conditions monotone
    # (yb grows -> edges enter; ya grows -> edges leave for good). Sweeping
    # top-down evicts bottom edges permanently (DEVLOG.md bug #1).
    for j in range(j1, j0 - 1, -1):
        ya = grid.ytop - (j + 1) * c
        yb = grid.ytop - j * c
        while ptr < n_edges and edges[ptr][0] <= yb:
            active.append(edges[ptr])
            ptr += 1
        if active:
            active = [e for e in active if e[1] >= ya]
        yc = 0.5 * (ya + yb)

        if not active:
            # Certified strip: covered x-set is constant across the strip;
            # if the bbox centre is inside, the whole bbox row is covered
            # (THEORY.md lemma 1).
            if point_in_rings((xmid, yc), rings):
                for i in range(i0, i1 + 1):
                    key = (i, j)
                    out[key] = out.get(key, 0.0) + cell_area
            continue

        # Uncertified strip: clip once, then decide per cell.
        frags = [_clip_strip(ring, ya, yb) for ring in rings]
        frags = [f for f in frags if len(f) >= 3]
        if not frags:
            continue
        frag_edges: list[tuple[Point, Point]] = []
        fxs: list[float] = []
        for f in frags:
            n = len(f)
            for k in range(n):
                p, q = f[k], f[(k + 1) % n]
                fxs.append(p[0])
                if p[1] == q[1] and (abs(p[1] - ya) <= 1e-12 * c or abs(p[1] - yb) <= 1e-12 * c):
                    continue  # bridge along the strip boundary: cannot split a cell
                frag_edges.append((p, q))
        if not frag_edges:
            # Only boundary bridges survive: the strip interior is uniform.
            if point_in_rings((xmid, yc), frags):
                for i in range(i0, i1 + 1):
                    key = (i, j)
                    out[key] = out.get(key, 0.0) + cell_area
            continue
        ia = max(i0, grid.col_of(min(fxs)))
        ib = min(i1, grid.col_of(max(fxs)))
        for i in range(ia, ib + 1):
            cx0, cx1 = grid.cell_x_range(i)
            cell_rect = (cx0, ya, cx1, yb)
            if not any(_seg_hits_rect(p, q, cell_rect) for (p, q) in frag_edges):
                # Clean cell: no fragment edge enters it; parity decides.
                if point_in_rings((0.5 * (cx0 + cx1), yc), frags):
                    key = (i, j)
                    out[key] = out.get(key, 0.0) + cell_area
                continue
            area = 0.0
            for f in frags:
                clipped = _clip_x(f, cx0, cx1)
                if len(clipped) >= 3:
                    area += ring_signed_area(clipped)
            # Float-noise guard only; valid inputs never exceed the cell.
            area = min(cell_area, max(0.0, area))
            if area > noise:
                key = (i, j)
                out[key] = out.get(key, 0.0) + area


# --------------------------------------------------------------------------
# ring orientation normalization
# --------------------------------------------------------------------------

def _normalize_rings(rings: Sequence[Sequence[Point]]) -> list[list[Point]]:
    """Orient rings so that nesting depth d has sign (-1)^d: exteriors become
    counter-clockwise, holes clockwise, holes-in-holes counter-clockwise...

    With consistent orientation, the even-odd (xor) area of the ring set
    equals the plain signed shoelace sum of the (clipped) rings -- no
    orientation-dependent surprises from GeoJSON input.
    """
    rings = [list(r) for r in rings if len(r) >= 3 and abs(ring_signed_area(r)) > 0.0]
    if not rings:
        return []
    if len(rings) == 1:
        r = rings[0]
        if ring_signed_area(r) < 0:
            r = r[::-1]
        return [r]
    bboxes = []
    for r in rings:
        xs = [p[0] for p in r]
        ys = [p[1] for p in r]
        bboxes.append((min(xs), min(ys), max(xs), max(ys)))
    out: list[list[Point]] = []
    for i, ri in enumerate(rings):
        px, py = ri[0]
        depth = 0
        for j, rj in enumerate(rings):
            if i == j:
                continue
            bx0, by0, bx1, by1 = bboxes[j]
            if bx0 <= px <= bx1 and by0 <= py <= by1 and point_in_rings((px, py), [rj]):
                depth += 1
        r = ri
        if (depth % 2 == 0) != (ring_signed_area(r) > 0):
            r = r[::-1]
        out.append(r)
    return out


# --------------------------------------------------------------------------
# clipping primitives
# --------------------------------------------------------------------------

def _clip_halfplane(ring: Sequence[Point], axis: int, value: float, keep_ge: bool) -> list[Point]:
    """Sutherland-Hodgman clip of a closed ring against one half-plane."""
    if not ring:
        return []
    out: list[Point] = []
    prev = ring[-1]
    for cur in ring:
        pv, cv = prev[axis], cur[axis]
        prev_in = (pv >= value) if keep_ge else (pv <= value)
        cur_in = (cv >= value) if keep_ge else (cv <= value)
        if cur_in:
            if not prev_in:
                t = (value - pv) / (cv - pv)
                out.append((prev[0] + t * (cur[0] - prev[0]), prev[1] + t * (cur[1] - prev[1])))
            out.append(cur)
        elif prev_in:
            t = (value - pv) / (cv - pv)
            out.append((prev[0] + t * (cur[0] - prev[0]), prev[1] + t * (cur[1] - prev[1])))
        prev = cur
    return out


def _clip_strip(ring: Sequence[Point], ya: float, yb: float) -> list[Point]:
    r = _clip_halfplane(ring, 1, ya, True)
    return _clip_halfplane(r, 1, yb, False)


def _clip_x(ring: Sequence[Point], cx0: float, cx1: float) -> list[Point]:
    r = _clip_halfplane(ring, 0, cx0, True)
    return _clip_halfplane(r, 0, cx1, False)


def _seg_hits_rect(p: Point, q: Point, rect: tuple[float, float, float, float]) -> bool:
    """Conservative test: does segment pq intersect the closed rectangle?

    Used only to decide whether a cell needs exact clipping, so false
    positives merely cost time.
    """
    x0, y0, x1, y1 = rect
    px, py = p
    qx, qy = q
    if max(px, qx) < x0 or min(px, qx) > x1 or max(py, qy) < y0 or min(py, qy) > y1:
        return False
    # Liang-Bars style rejection: segment vs each edge line.
    dx, dy = qx - px, qy - py
    t0, t1 = 0.0, 1.0
    for delta, origin, lo, hi in ((dx, px, x0, x1), (dy, py, y0, y1)):
        if delta > 0:
            a, b = (lo - origin) / delta, (hi - origin) / delta
        elif delta < 0:
            a, b = (hi - origin) / delta, (lo - origin) / delta
        else:
            if origin < lo or origin > hi:
                return False
            continue
        if a > t0:
            t0 = a
        if b < t1:
            t1 = b
        if t0 > t1:
            return False
    return True
