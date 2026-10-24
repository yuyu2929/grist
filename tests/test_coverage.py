"""Coverage kernel: exactness vs shapely oracle (fuzz + targeted cases).

The oracle computes the true area/length of (feature INTERSECT cell) with
shapely; Grist must match to floating-point noise on every cell, and must
never over- or under-count mass in aggregate.
"""
import os
import math
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from shapely.geometry import Polygon as ShapelyPolygon, LineString, Point, box
except ImportError:  # optional test dependency
    # The library is dependency-free; this file needs a second engine to act
    # as an independent oracle. Skip cleanly instead of dying at import time
    # (the first thing anyone does is `python3 tests/run_all.py` on a clean
    # machine, which has no shapely).
    print("SKIP test_coverage.py: shapely is not installed "
          "(pip install shapely, or `pip install -e '.[test]'`)")
    raise SystemExit(0)

from grist.coverage import line_lengths, point_counts, polygon_areas
from grist.features import Feature
from grist.grid import Grid

TOL = 1e-9


def oracle_cell_area(polys, rect):
    b = box(*rect)
    total = 0.0
    for rings in polys:
        s = ShapelyPolygon(rings[0], rings[1:])
        if s.is_valid and s.intersects(b):
            total += s.intersection(b).area
    return total


def check_polygon(polys, grid, label=""):
    got = polygon_areas(polys, grid)
    # oracle over the polygon's bbox cells (conservative)
    xs = [p[0] for rings in polys for r in rings for p in r]
    ys = [p[1] for rings in polys for r in rings for p in r]
    bc = grid.bbox_cells((min(xs), min(ys), max(xs), max(ys)))
    if bc is None:
        assert not got, f"{label}: coverage outside grid should be empty"
        return
    i0, i1, j0, j1 = bc
    worst = 0.0
    mass_got = sum(got.values())
    mass_ref = 0.0
    for j in range(j0, j1 + 1):
        for i in range(i0, i1 + 1):
            ref = oracle_cell_area(polys, grid.cell_rect(i, j))
            mass_ref += ref
            v = got.get((i, j), 0.0)
            worst = max(worst, abs(v - ref))
            assert abs(v - ref) < TOL, \
                f"{label}: cell {(i, j)} got {v!r} ref {ref!r}"
    assert abs(mass_got - mass_ref) < 1e-6 * max(1.0, mass_ref), \
        f"{label}: mass {mass_got} vs {mass_ref}"


def test_axis_aligned_square():
    g = Grid(0.0, 10.0, 1.0, 10, 10)
    polys = [[[(0, 0), (10, 0), (10, 10), (0, 10)]]]
    got = polygon_areas(polys, g)
    assert len(got) == 100
    assert all(abs(v - 1.0) < TOL for v in got.values())


def test_square_with_hole_even_odd():
    g = Grid(0.0, 10.0, 1.0, 10, 10)
    polys = [[[(2, 2), (8, 2), (8, 8), (2, 8)], [(4, 4), (6, 4), (6, 6), (4, 6)]]]
    got = polygon_areas(polys, g)
    # hole cells must be absent; ring cells full; outer cells full
    assert (5, 5) not in got or got[(5, 5)] == 0.0
    assert abs(sum(got.values()) - 32.0) < TOL


def test_island_in_hole_is_material():
    # even-odd: island inside a hole counts as area
    g = Grid(0.0, 10.0, 1.0, 10, 10)
    polys = [[[(2, 2), (8, 2), (8, 8), (2, 8)],
              [(4, 4), (6, 4), (6, 6), (4, 6)],
              [(4.5, 4.5), (5.5, 4.5), (5.5, 5.5), (4.5, 5.5)]]]
    got = polygon_areas(polys, g)
    assert abs(sum(got.values()) - 33.0) < TOL


def test_boundary_alignment_half_open():
    g = Grid(0.0, 10.0, 1.0, 10, 10)
    # polygon whose right edge lies exactly on a grid line
    polys = [[[(0, 0), (5, 0), (5, 10), (0, 10)]]]
    got = polygon_areas(polys, g)
    assert set(got) == {(i, j) for i in range(5) for j in range(10)}
    assert all(abs(v - 1.0) < TOL for v in got.values())


def test_partial_cell_exact():
    g = Grid(0.0, 1.0, 1.0, 1, 1)
    polys = [[[(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75)]]]
    got = polygon_areas(polys, g)
    assert abs(got[(0, 0)] - 0.25) < TOL


def test_rotated_square():
    g = Grid(0.0, 10.0, 1.0, 10, 10)
    r = 3.0
    ring = [(5 + r * math.cos(math.radians(a)), 5 + r * math.sin(math.radians(a)))
            for a in range(0, 360, 30)]
    check_polygon([[ring]], g, "rotated square")


def test_concave_star():
    rng = random.Random(42)
    for t in range(5):
        cx, cy = rng.uniform(3, 7), rng.uniform(3, 7)
        n = rng.randint(5, 11)
        ring = [(cx + rng.uniform(1.0, 3.0) * math.cos(2 * math.pi * k / n),
                 cy + rng.uniform(1.0, 3.0) * math.sin(2 * math.pi * k / n))
                for k in range(n)]
        check_polygon([[ring]], Grid(0.0, 10.0, 0.5, 20, 20), f"star{t}")


def test_multi_part():
    g = Grid(0.0, 10.0, 1.0, 10, 10)
    polys = [[[(0, 0), (2, 0), (2, 2), (0, 2)]],
             [[(5, 5), (9, 5), (9, 9), (5, 9)]]]
    got = polygon_areas(polys, g)
    assert abs(sum(got.values()) - 20.0) < TOL


def test_thin_sliver():
    g = Grid(0.0, 10.0, 1.0, 10, 10)
    polys = [[[(1, 5), (9, 5.5), (9, 5.55), (1, 5.05)]]]
    check_polygon(polys, g, "sliver")


def test_fuzz_random_polygons():
    rng = random.Random(1234)
    for t in range(12):
        n = rng.randint(3, 12)
        cx, cy = rng.uniform(3, 7), rng.uniform(3, 7)
        # star-shaped from the center: always simple, so the shapely oracle
        # and Grist's even-odd rule describe the same region
        ring = [(cx + rng.uniform(1.0, 4.0) * math.cos(2 * math.pi * k / n),
                 cy + rng.uniform(1.0, 4.0) * math.sin(2 * math.pi * k / n))
                for k in range(n)]
        check_polygon([[ring]], Grid(0.0, 10.0, 0.5, 20, 20), f"fuzz{t}")


def test_fuzz_with_holes():
    rng = random.Random(99)
    for t in range(8):
        cx, cy = rng.uniform(3, 7), rng.uniform(3, 7)
        n_out, n_in = rng.randint(4, 9), rng.randint(3, 6)
        outer = [(cx + rng.uniform(2, 4) * math.cos(2 * math.pi * k / n_out),
                  cy + rng.uniform(2, 4) * math.sin(2 * math.pi * k / n_out))
                 for k in range(n_out)]
        hole = [(cx + rng.uniform(0.5, 1.2) * math.cos(2 * math.pi * k / n_in),
                 cy + rng.uniform(0.5, 1.2) * math.sin(2 * math.pi * k / n_in))
                for k in range(n_in)]
        check_polygon([[outer, hole]], Grid(0.0, 10.0, 0.5, 20, 20), f"hole{t}")


def test_point_counts_exact():
    g = Grid(0.0, 10.0, 1.0, 10, 10)
    f = Feature(0, "point", points=[(0.5, 9.5), (0.5, 9.5), (9.5, 0.5), (100, 100)])
    got = point_counts(f.points, g)
    assert got == {(0, 0): 2, (9, 9): 1}


def test_point_counts_boundary():
    g = Grid(0.0, 10.0, 1.0, 10, 10)
    # y=5.0 is the bottom edge of row 4 ([5,6)) -> row 4 owns it
    f = Feature(0, "point", points=[(1.0, 5.0), (5.0, 5.0)])
    got = point_counts(f.points, g)
    assert got == {(1, 4): 1, (5, 4): 1}


def test_line_lengths_exact():
    g = Grid(0.0, 10.0, 1.0, 10, 10)
    f = Feature(0, "line", lines=[[(0.5, 5.5), (9.5, 5.5)]])
    got = line_lengths(f.lines, g)
    # x from 0.5 to 9.5: two half cells at the ends, eight full cells between
    assert len(got) == 10
    assert abs(got[(0, 4)] - 0.5) < TOL and abs(got[(9, 4)] - 0.5) < TOL
    assert all(abs(v - 1.0) < TOL for (i, j), v in got.items() if 1 <= i <= 8)
    assert abs(sum(got.values()) - 9.0) < TOL


def test_line_lengths_diagonal():
    g = Grid(0.0, 10.0, 1.0, 10, 10)
    f = Feature(0, "line", lines=[[(0.5, 9.5), (9.5, 0.5)]])
    got = line_lengths(f.lines, g)
    assert abs(sum(got.values()) - 9.0 * math.sqrt(2)) < 1e-9


def test_line_lengths_fuzz():
    rng = random.Random(7)
    for t in range(10):
        a = (rng.uniform(0, 10), rng.uniform(0, 10))
        b = (rng.uniform(0, 10), rng.uniform(0, 10))
        f = Feature(0, "line", lines=[[a, b]])
        got = line_lengths(f.lines, Grid(0.0, 10.0, 1.0, 10, 10))
        seg = LineString([a, b])
        ref = 0.0
        for j in range(10):
            for i in range(10):
                b_x = box(*Grid(0.0, 10.0, 1.0, 10, 10).cell_rect(i, j))
                if seg.intersects(b_x):
                    ref += seg.intersection(b_x).length
        assert abs(sum(got.values()) - ref) < 1e-9, \
            f"line fuzz {t}: {sum(got.values())} vs {ref}"


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
