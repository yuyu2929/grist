"""Grid definition and cell indexing.

The grid origin is the TOP-LEFT corner (image convention, matching PGM/PNG
export). Row 0 is the top row; y grows downward in row space while GeoJSON
coordinates grow upward, so the mapping is y_row_range = [ytop-(j+1)c, ytop-jc).

All cell containment uses the half-open rule, identical in x and y:
    x0 + i*c <= px < x0 + (i+1)*c   and   ytop - (j+1)*c <= py < ytop - j*c
"""
from __future__ import annotations

import math
from typing import Optional

from .geom import Point, Rect


class Grid:
    def __init__(self, x0: float, ytop: float, cell: float, w: int, h: int):
        if cell <= 0:
            raise ValueError("cell size must be positive")
        if w < 1 or h < 1:
            raise ValueError("grid must have at least one cell")
        self.x0 = float(x0)
        self.ytop = float(ytop)
        self.cell = float(cell)
        self.w = int(w)
        self.h = int(h)

    # -- extents ---------------------------------------------------------
    @property
    def x1(self) -> float:
        return self.x0 + self.w * self.cell

    @property
    def ybot(self) -> float:
        return self.ytop - self.h * self.cell

    def cell_rect(self, i: int, j: int) -> Rect:
        cx0 = self.x0 + i * self.cell
        cy1 = self.ytop - j * self.cell
        return (cx0, cy1 - self.cell, cx0 + self.cell, cy1)

    # -- point membership (half-open) ------------------------------------
    def point_cell(self, x: float, y: float) -> Optional[tuple[int, int]]:
        if not (self.x0 <= x < self.x1 and self.ybot <= y < self.ytop):
            return None
        i = int((x - self.x0) / self.cell)
        j = self.row_of(y)
        # Float guard: a value within 1 ulp of the far edge may round up.
        if i >= self.w:
            i = self.w - 1
        if j >= self.h:
            j = self.h - 1
        return (i, j)

    def row_of(self, y: float) -> int:
        """Row whose half-open y-range [ytop-(j+1)c, ytop-jc) contains y.

        The interval is closed at its LOWER y, so a point exactly on a row's
        top edge belongs to the row above: row = ceil((ytop-y)/c) - 1, i.e.
        floor(t) minus one when y sits on the top edge of that row. The
        boundary test uses the edge value itself (no absolute epsilon, which
        cannot distinguish "on the top edge" from "one ulp below it" when
        j = 0); float noise in t is absorbed because both cases round to a
        row that owns y consistently, preserving the measure partition.
        """
        t = (self.ytop - y) / self.cell
        j = int(math.floor(t))
        if y >= self.ytop - j * self.cell:
            j -= 1
        return j

    def col_of(self, x: float) -> int:
        return int(math.floor((x - self.x0) / self.cell))

    # -- bbox -> candidate cell range -------------------------------------
    def bbox_cells(self, bbox: tuple[float, float, float, float]) -> Optional[tuple[int, int, int, int]]:
        """Cells that can have positive intersection with bbox (bx0,by0,bx1,by1).

        Returns (i0, i1, j0, j1) inclusive, clamped to the grid, or None when
        the bbox misses the grid entirely. The range is conservative (may
        include one extra cell on each side) so that no positive-measure
        intersection is ever missed through float noise.
        """
        bx0, by0, bx1, by1 = bbox
        # Half-open domain: the LEFT and BOTTOM edges are inclusive, the
        # RIGHT and TOP edges exclusive. The miss test must respect that:
        # a bbox ending exactly on x0/ybot still intersects the domain
        # (e.g. a segment lying along the bottom edge), while one starting
        # at x1/ytop is outside. Using <= on the inclusive edges silently
        # dropped boundary-coincident geometry (DEVLOG BUG 19).
        if bx1 < self.x0 or bx0 >= self.x1 or by1 < self.ybot or by0 >= self.ytop:
            return None
        c = self.cell
        eps = 1e-9 * c
        i0 = max(0, int(math.floor((bx0 - self.x0) / c - eps)))
        i1 = min(self.w - 1, int(math.floor((bx1 - self.x0) / c + eps)))
        j0 = max(0, self.row_of(by1 + eps))
        j1 = min(self.h - 1, self.row_of(by0 - eps))
        if i0 > i1 or j0 > j1:
            return None
        return (i0, i1, j0, j1)

    def cell_x_range(self, i: int) -> tuple[float, float]:
        return (self.x0 + i * self.cell, self.x0 + (i + 1) * self.cell)

    def row_y_range(self, j: int) -> tuple[float, float]:
        return (self.ytop - (j + 1) * self.cell, self.ytop - j * self.cell)
