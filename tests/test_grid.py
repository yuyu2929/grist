"""Grid: half-open cell ownership, row/col mapping, bbox expansion."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grist.grid import Grid


def make_grid():
    # 4x3 grid, cell size 10, origin top-left at (0, 30)
    return Grid(0.0, 30.0, 10.0, 4, 3)


def test_point_cell_center():
    g = make_grid()
    assert g.point_cell(5.0, 25.0) == (0, 0)
    assert g.point_cell(15.0, 15.0) == (1, 1)
    assert g.point_cell(35.0, 5.0) == (3, 2)


def test_point_cell_outside():
    g = make_grid()
    assert g.point_cell(-0.1, 15.0) is None
    assert g.point_cell(40.0, 15.0) is None
    assert g.point_cell(15.0, 30.1) is None
    assert g.point_cell(15.0, -0.1) is None


def test_point_cell_corner_ownership():
    g = make_grid()
    # x on a vertical boundary -> the cell to the right (half-open [x0, x1))
    assert g.point_cell(10.0, 25.0) == (1, 0)
    # y on a horizontal boundary -> the row ABOVE (row range closed at bottom)
    assert g.point_cell(5.0, 20.0) == (0, 0)
    assert g.point_cell(5.0, 10.0) == (0, 1)


def test_row_of_exact_boundaries():
    g = make_grid()
    # y == ytop is the top boundary: outside every row (half-open discipline)
    assert g.row_of(30.0) == -1
    # a row owns its BOTTOM edge
    assert g.row_of(20.0) == 0
    assert g.row_of(19.999) == 1
    assert g.row_of(10.0) == 1
    assert g.row_of(0.0) == 2


def test_col_of_exact_boundaries():
    g = make_grid()
    assert g.col_of(0.0) == 0
    assert g.col_of(10.0) == 1
    assert g.col_of(39.999) == 3


def test_cell_rect_roundtrip():
    g = make_grid()
    x0, y0, x1, y1 = g.cell_rect(2, 1)
    assert (x0, y0, x1, y1) == (20.0, 10.0, 30.0, 20.0)


def test_bbox_cells_cover():
    g = make_grid()
    assert g.bbox_cells((5.0, 5.0, 25.0, 25.0)) == (0, 2, 0, 2)


def test_bbox_cells_miss():
    g = make_grid()
    assert g.bbox_cells((100.0, 100.0, 200.0, 200.0)) is None
    assert g.bbox_cells((-50.0, -50.0, -10.0, -10.0)) is None


def test_bbox_cells_conservative_at_edges():
    g = make_grid()
    # bbox exactly on a cell edge must still cover the touching cells
    i0, i1, j0, j1 = g.bbox_cells((10.0, 10.0, 20.0, 20.0))
    assert i0 <= 1 <= i1 and j0 <= 1 <= j1


def test_level_dims_ceil():
    g = Grid(0.0, 10.0, 1.0, 5, 5)
    assert g.w == 5 and g.h == 5


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
