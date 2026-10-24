"""Geometry primitives: half-open clipping, crossings, parity, signed area."""
import os
import math
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grist.geom import (NOISE_FRACTION, crossings_at_y, parity_intervals,
                        point_in_rings, ring_signed_area, seg_rect_length)


def test_seg_rect_length_full_inside():
    # horizontal segment fully inside a 1x1 cell at origin
    assert abs(seg_rect_length((0.25, 0.5), (0.75, 0.5), (0.0, 0.0, 1.0, 1.0)) - 0.5) < 1e-12


def test_seg_rect_length_clipped():
    # segment crossing the cell: only the inside part counts
    assert abs(seg_rect_length((-1.0, 0.5), (2.0, 0.5), (0.0, 0.0, 1.0, 1.0)) - 1.0) < 1e-12


def test_seg_rect_length_diagonal():
    # 45-degree diagonal through unit square -> sqrt(2)
    assert abs(seg_rect_length((0.0, 0.0), (1.0, 1.0), (0.0, 0.0, 1.0, 1.0))
               - math.sqrt(2)) < 1e-12


def test_seg_rect_length_miss():
    assert seg_rect_length((5.0, 5.0), (6.0, 6.0), (0.0, 0.0, 1.0, 1.0)) == 0.0


def test_seg_rect_length_half_open_x():
    # segment ending exactly at the right edge: half-open rule excludes it
    assert seg_rect_length((0.0, 0.5), (1.0, 0.5), (0.0, 0.0, 1.0, 1.0)) == 1.0
    # a segment lying exactly ON the right edge has zero interior length
    assert seg_rect_length((1.0, 0.2), (1.0, 0.8), (0.0, 0.0, 1.0, 1.0)) == 0.0


def _edges(ring):
    """Build kernel-style edge tuples (ymin, ymax, x1, y1, x2, y2)."""
    out = []
    n = len(ring)
    for k in range(n):
        x1, y1 = ring[k]
        x2, y2 = ring[(k + 1) % n]
        if y1 != y2:
            out.append((min(y1, y2), max(y1, y2), x1, y1, x2, y2))
    return out


def test_crossings_half_open_rule():
    square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    xs = crossings_at_y(_edges(square), 0.5)
    assert len(xs) == 2
    assert abs(min(xs)) < 1e-12 and abs(max(xs) - 1.0) < 1e-12


def test_crossings_at_vertex_y():
    # scanline exactly at a vertex y: each edge counted once, no duplicates
    diamond = [(0.5, 0.0), (1.0, 0.5), (0.5, 1.0), (0.0, 0.5)]
    xs = crossings_at_y(_edges(diamond), 0.5)
    assert sorted(xs) == [0.0, 1.0]


def test_parity_intervals_simple():
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    iv = parity_intervals(crossings_at_y(_edges(square), 1.0))
    assert len(iv) == 1
    assert abs(iv[0][0]) < 1e-12 and abs(iv[0][1] - 2.0) < 1e-12


def test_parity_intervals_with_hole():
    outer = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
    hole = [(1.0, 1.0), (3.0, 1.0), (3.0, 3.0), (1.0, 3.0)]
    iv = parity_intervals(crossings_at_y(_edges(outer), 2.0))
    assert iv == [(0.0, 4.0)]
    iv_h = parity_intervals(crossings_at_y(_edges(hole), 2.0))
    assert iv_h == [(1.0, 3.0)]


def test_parity_intervals_odd_defensive():
    # an unpaired crossing is dropped rather than emitting a bogus interval
    iv = parity_intervals([0.0, 1.0, 2.0])
    assert iv == [(0.0, 1.0)]


def test_point_in_rings_basic():
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    assert point_in_rings((1.0, 1.0), [square])
    assert not point_in_rings((3.0, 1.0), [square])


def test_point_in_rings_boundary_half_open():
    # point exactly on the right edge is outside (half-open discipline)
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    assert not point_in_rings((2.0, 1.0), [square])


def test_ring_signed_area():
    ccw = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    assert abs(ring_signed_area(ccw) - 4.0) < 1e-12
    cw = list(reversed(ccw))
    assert abs(ring_signed_area(cw) + 4.0) < 1e-12


def test_noise_fraction_reasonable():
    assert 0.0 < NOISE_FRACTION < 1e-6


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"[ok] {name}")
            except AssertionError as e:
                fails += 1
                print(f"[FAIL] {name}: {e}")
    print("ALL OK" if not fails else f"{fails} FAILURES")
    sys.exit(1 if fails else 0)
