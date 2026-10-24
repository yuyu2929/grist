"""Bake layer: grid fitting, domain coverage, boundary conservation.

The scenarios here are the BUG 15 / BUG 18 family: the half-open grid
domain [x0, x1) x [ybot, ytop) cannot strictly contain a closed span of
equal width, so a grid that merely touches the data bbox silently drops
boundary-coincident geometry. The default (auto) pad exists so the
default path conserves mass exactly; pad=0 remains the explicit opt-out.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grist.bake import bake_geojson, fit_grid
from grist.features import features_from_geojson


def _fc(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def _feat(fid, geom):
    return {"type": "Feature", "id": fid, "properties": {}, "geometry": geom}


# a road whose far vertex lies exactly on the data bbox's top-right corner
_ROAD = _fc(_feat(0, {"type": "LineString",
                      "coordinates": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]}))
_SQUARE = _fc(_feat(0, {"type": "Polygon",
                        "coordinates": [[[0.0, 0.0], [100.0, 0.0],
                                         [100.0, 100.0], [0.0, 100.0],
                                         [0.0, 0.0]]]}))
_POINT = _fc(_feat(7, {"type": "Point", "coordinates": [3.0, 4.0]}))


def test_auto_pad_conserves_boundary_touching_line():
    """Default bake must not lose the boundary-coincident road (BUG 18)."""
    feats = features_from_geojson(_ROAD)
    pyr = bake_geojson(_ROAD, cell=1.0)            # pad=None -> auto
    assert pyr.totals()["line"] == feats[0].measure_total() == 20.0


def test_pad_zero_is_the_documented_opt_out():
    """pad=0 keeps the tight domain; boundary-coincident parts fall outside."""
    feats = features_from_geojson(_ROAD)
    pyr = bake_geojson(_ROAD, cell=1.0, pad=0.0)
    # the vertical segment at x == x1 is excluded, the horizontal one at
    # y == ybot is included (half-open: bottom/left edges belong to the domain)
    assert abs(pyr.totals()["line"] - 10.0) < 1e-12


def test_auto_pad_conserves_polygon_area():
    feats = features_from_geojson(_SQUARE)
    pyr = bake_geojson(_SQUARE, cell_count=128)
    assert abs(pyr.totals()["polygon"] - feats[0].measure_total()) < 1e-9


def test_exact_multiple_extent_gets_covering_cell():
    """Extent 100 with cell 0.5 needs 201 cells: 200 half-open cells of
    total width 100 cannot strictly contain a closed span of width 100.
    The extra row/column is where the boundary-coincident data lives."""
    feats = features_from_geojson(_SQUARE)
    grid = fit_grid(feats, cell=0.5)
    assert grid.w == 201 and grid.h == 201
    assert grid.x0 + grid.w * grid.cell > 100.0     # right edge strictly past data
    assert grid.ytop > 100.0                        # top edge strictly past data
    assert grid.x0 <= 0.0 and grid.ytop - grid.h * grid.cell <= 0.0


def test_tight_domain_opt_out_keeps_cell_count():
    grid = fit_grid(features_from_geojson(_SQUARE), cell=0.5, pad=0.0)
    assert (grid.x0, grid.ytop, grid.w, grid.h) == (0.0, 100.0, 200, 200)


def test_degenerate_point_dataset_is_counted():
    """A single point used to produce a zero-cell grid (and, in
    cell-count mode, a zero cell size). Auto-pad makes it strictly
    interior, in both fitting modes."""
    for kwargs in ({"cell": 0.5}, {"cell_count": 16}):
        pyr = bake_geojson(_POINT, **kwargs)
        assert pyr.totals()["point"] == 1.0, kwargs


def test_auto_pad_is_relative_and_bounded():
    """Auto pad scales with the extent (1e-9 relative) but never vanishes
    (1e-12 absolute floor for degenerate extents)."""
    big = fit_grid(features_from_geojson(_SQUARE), cell=0.5)
    small = fit_grid(features_from_geojson(_POINT), cell=0.5)
    assert abs(big.x0 + 1e-7) < 1e-20           # 1e-9 x extent 100
    assert abs(small.x0 - (3.0 - 1e-12)) < 1e-18  # absolute floor, zero extent


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
