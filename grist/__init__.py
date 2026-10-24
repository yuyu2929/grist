"""Grist: exact vector-to-raster translation.

Point / line / polygon -> grid cells with cell-integral semantics
(THEORY.md): a cell's value is the average of the feature's coverage
function over the cell. Points are counted exactly, lines contribute
their exact clipped length, polygons their exact clipped area fraction.

The core library is MIT-licensed and unrestricted. Implemented enterprise
conveniences (parallel bake, AES-GCM sealed containers, JSONL audit log) live
behind license gates in grist.enterprise; remote tile storage is planned and
is never granted by a license.
"""
from .grid import Grid
from .features import Feature, features_from_geojson, load_features
from .pyramid import Pyramid, Tile, TileStore
from . import codec

__version__ = "0.1.1"

__all__ = [
    "Grid", "Feature", "Pyramid", "Tile", "TileStore", "codec",
    "features_from_geojson", "load_features",
    "bake_features", "bake_geojson", "fit_grid",
    "dense_matrix", "dominant_matrix", "point_query", "pgm", "csv_cells",
    "__version__",
]


def __getattr__(name: str):
    # lazy imports so `import grist` stays cheap
    if name in ("bake_features", "bake_geojson", "fit_grid"):
        from . import bake as _bake
        return getattr(_bake, name)
    if name in ("dense_matrix", "dominant_matrix", "point_query", "pgm", "csv_cells"):
        from . import query as _q
        return getattr(_q, name)
    raise AttributeError(name)
