"""Bake: GeoJSON -> exact-coverage pyramid.

Public API:
    bake_features(features, grid, tile_size, max_levels) -> Pyramid
    fit_grid(features, cell=None, cell_count=None, pad=None) -> Grid
    bake_geojson(path_or_dict, ...) -> Pyramid
    bake(path, out, ...) -> Pyramid (convenience, also saves .grist)

Grid fitting: when the caller gives a target cell count, the grid is the
smallest axis-aligned grid of at least that many cells that covers all
feature bboxes (padded). Cell size is then (width or height) / count --
never a rounded "nice" number, because rounding would break the exactness
argument: every coordinate is either exactly representable or safely
interior to a cell, and rounding risks landing a boundary exactly on a
cell edge in a way that changes ownership. Deterministic, no snapping.
"""
from __future__ import annotations

import json
import math
from typing import Iterable, Optional

from .features import Feature, load_features
from .grid import Grid
from .pyramid import Pyramid

DEFAULT_TILE = 16


def fit_grid(features: Iterable[Feature], cell: Optional[float] = None,
             cell_count: Optional[int] = None, pad: Optional[float] = None) -> Grid:
    """Fit a grid over the features' bounding box.

    Domain semantics: the grid domain is half-open,
    [x0, x1) x [ybot, ytop). A closed span of width W cannot be strictly
    contained in a half-open interval of width W, so a domain that merely
    TOUCHES the data bbox necessarily excludes boundary-coincident
    geometry (e.g. a road whose vertex is y == ytop loses its length;
    DEVLOG BUG 15).

    Therefore `pad=None` (the default) auto-pads the bbox by
    1e-9 of the extent (floor 1e-12), which makes the domain strictly
    cover the data at a cost of ~1e-9 relative width. Conservation then
    holds exactly and `grist verify` passes on the default path. Note
    this can add one row/column when the extent is an exact multiple of
    the cell size -- that cell is not spurious: it is the only place the
    boundary-coincident geometry can live.

    `pad=0.0` is the explicit opt-out: the domain then coincides with the
    data bbox and boundary-coincident geometry contributes nothing.
    """
    feats = list(features)
    boxes = [f.bbox() for f in feats if f.bbox() is not None]
    if not boxes:
        if cell is None:
            cell = 1.0
        return Grid(0.0, cell, cell, 1, 1)
    rx0 = min(b[0] for b in boxes)
    ry0 = min(b[1] for b in boxes)
    rx1 = max(b[2] for b in boxes)
    ry1 = max(b[3] for b in boxes)
    if pad is None:
        pad = max(1e-9 * max(rx1 - rx0, ry1 - ry0), 1e-12)
    bx0 = rx0 - pad
    by0 = ry0 - pad
    bx1 = rx1 + pad
    by1 = ry1 + pad
    if bx1 <= bx0 or by1 <= by0:
        if cell is None:
            cell = 1.0
        return Grid(bx0, by1, cell, 1, 1)
    if cell is None:
        if cell_count is None:
            cell_count = 512
        cell_count = max(1, int(cell_count))
        # choose the larger axis, then the smaller axis follows its count
        cell = max(bx1 - bx0, by1 - by0) / cell_count
    if not cell or cell <= 0:      # degenerate extent with explicit pad=0
        cell = 1.0
    w = int(math.ceil((bx1 - bx0) / cell))
    h = int(math.ceil((by1 - by0) / cell))
    w = max(1, w)
    h = max(1, h)
    return Grid(bx0, by1, cell, w, h)


def bake_features(features: Iterable[Feature], grid: Grid, tile_size: int = DEFAULT_TILE,
                  max_levels: Optional[int] = None) -> Pyramid:
    return Pyramid.bake(features, grid, tile_size=tile_size, max_levels=max_levels)


def bake_geojson(data: dict, cell: Optional[float] = None, cell_count: Optional[int] = None,
                 tile_size: int = DEFAULT_TILE, pad: Optional[float] = None,
                 max_levels: Optional[int] = None) -> Pyramid:
    from .features import features_from_geojson
    feats = features_from_geojson(data)
    if not feats:
        raise ValueError("no supported geometry features in input")
    grid = fit_grid(feats, cell=cell, cell_count=cell_count, pad=pad)
    return bake_features(feats, grid, tile_size=tile_size, max_levels=max_levels)


def bake(path: str, cell: Optional[float] = None, cell_count: Optional[int] = None,
         tile_size: int = DEFAULT_TILE, pad: Optional[float] = None,
         max_levels: Optional[int] = None) -> tuple[Pyramid, Grid]:
    """Load a GeoJSON file and bake the pyramid (in memory; save separately).

    Returns (pyramid, grid); the grid is also available as ``pyramid.grid``.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    pyr = bake_geojson(data, cell=cell, cell_count=cell_count, tile_size=tile_size,
                       pad=pad, max_levels=max_levels)
    return pyr, pyr.grid
