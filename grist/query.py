"""Query and export.

Read-side policies live here, not in the pyramid: the pyramid stores the
exact coverage mixture per cell (fid -> measure), and *interpretation*
(pick the winner, sum by label, threshold) is a read strategy. Keeping
policies out of the store is what makes the same pyramid serve different
consumers without re-baking.

Exports:
    dense_matrix(pyr, level, kind)      -> list[list[float]] coverage
    dominant_matrix(pyr, level)         -> fid per cell (or -1)
    pgm(pyr, level, kind, path)         -> P5/P2 grayscale image
    csv_cells(pyr, path)                -> per-cell CSV of level 0
    point_query(pyr, x, y, level)       -> CellValue + owner info

All exports are row-major top-down (image convention), matching PGM.
"""
from __future__ import annotations

import csv
import math
from typing import Optional

from .pyramid import CellValue, Pyramid

#: "who wins" strategies for a coverage mixture cell
#:
#:   top  -> (winning fid, that feature's measure)  "who owns this cell"
#:   max  -> (-1, largest single-feature measure)   "how much does the leader cover"
#:   sum  -> (-1, total measure over all features)  "total coverage, no owner"
POLICIES = ("sum", "max", "top")


def cell_measure(cell: Optional[CellValue], kind: str) -> float:
    """Total measure of one kind in a cell (ignores feature identity)."""
    if cell is None:
        return 0.0
    idx = ("point", "line", "polygon").index(kind)
    return sum(v for _fid, v in cell[idx])


def cell_owner(cell: Optional[CellValue], kind: str, policy: str = "top") -> tuple[int, float]:
    """Resolve a coverage mixture into (fid, measure) under *policy*.

    "top": the feature with the largest measure (ties -> smallest fid) - the
    answer to "who owns this cell".  "max": the largest single measure with no
    owner (fid -1).  "sum": the total over all features with no owner (fid -1).

    The policy argument used to be accepted and ignored, so "sum" and "max"
    silently behaved like "top" even though POLICIES advertises all three.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown cell policy {policy!r}; expected one of {POLICIES}")
    if cell is None:
        return (-1, 0.0)
    idx = ("point", "line", "polygon").index(kind)
    entries = sorted(cell[idx])
    if not entries:
        return (-1, 0.0)
    if policy == "sum":
        return (-1, math.fsum(v for _fid, v in entries))
    best_fid, best_val = -1, 0.0
    for fid, v in entries:                 # sorted by fid: ties keep the smaller
        if v > best_val:
            best_fid, best_val = fid, v
    if policy == "max":
        return (-1, best_val)
    return (best_fid, best_val)


def point_query(pyr: Pyramid, x: float, y: float, level: int = 0) -> dict:
    """Everything known about the cell containing (x, y) at a given level."""
    cell_ij = pyr.grid.point_cell(x, y)
    if cell_ij is None:
        return {"inside": False, "cell": None, "value": None}
    i, j = cell_ij
    f = 1 << level
    ci, cj = i // f, j // f
    val = pyr.level_cell(level, ci, cj)
    owners = {}
    for k, kind in enumerate(("point", "line", "polygon")):
        if val is None:
            owners[kind] = None
            continue
        owners[kind] = {"sum": sum(v for _f, v in val[k]),
                        "top": cell_owner(val, kind)}
    return {"inside": True, "cell": (i, j), "level_cell": (ci, cj), "value": val,
            "owners": owners}


def dense_matrix(pyr: Pyramid, level: int = 0, kind: str = "polygon") -> list[list[float]]:
    w, h = pyr.level_dims(level)
    c = pyr.grid.cell * (1 << level)
    frac = c * c if c > 0 else 1.0
    out = []
    for j in range(h):
        row = []
        for i in range(w):
            v = pyr.level_cell(level, i, j)
            row.append(cell_measure(v, kind) / frac)
        out.append(row)
    return out


def dominant_matrix(pyr: Pyramid, level: int = 0) -> list[list[int]]:
    """Fid with largest total measure per cell (-1 for empty cells)."""
    w, h = pyr.level_dims(level)
    out = []
    for j in range(h):
        row = []
        for i in range(w):
            v = pyr.level_cell(level, i, j)
            best = (-1, -1.0)
            if v is not None:
                scores: dict[int, float] = {}
                for part in v:
                    for fid, m in part:
                        scores[fid] = scores.get(fid, 0.0) + m
                for fid in sorted(scores):
                    if scores[fid] > best[1]:
                        best = (fid, scores[fid])
            row.append(best[0])
        out.append(row)
    return out


def _quantize(m: float, lo: float, hi: float) -> int:
    if hi <= lo:
        return 0 if m <= lo else 255
    t = (m - lo) / (hi - lo)
    return max(0, min(255, int(math.floor(t * 255.0 + 0.5))))


def pgm(pyr: Pyramid, level: int = 0, kind: str = "polygon", path: str = "out.pgm",
        lo: Optional[float] = None, hi: Optional[float] = None) -> None:
    """Write a binary PGM (P5). White = 0 coverage, black = hi coverage."""
    w, h = pyr.level_dims(level)
    if lo is None:
        lo = 0.0
    if hi is None:
        hi = 1.0
    header = f"P5\n{w} {h}\n255\n".encode("ascii")
    body = bytearray()
    for j in range(h):
        for i in range(w):
            v = pyr.level_cell(level, i, j)
            frac = cell_measure(v, kind) / (pyr.grid.cell * (1 << level)) ** 2
            body.append(255 - _quantize(frac, lo, hi))
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(bytes(body))


def csv_cells(pyr: Pyramid, path: str, level: int = 0, kinds=("point", "line", "polygon")) -> None:
    """One row per non-empty level-0 cell: i,j + per-kind totals + owners."""
    T = pyr.tile_size
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["i", "j", "x", "y", *kinds, "owners"])
        for (ti, tj) in sorted(pyr.levels[level]):
            tile = pyr.get_tile(pyr.levels[level][(ti, tj)])
            for (ri, rj) in sorted(tile.cells):
                i, j = ti * T + ri, tj * T + rj
                val = tile.cells[(ri, rj)]
                x, y = pyr.grid.cell_rect(i, j)[0], pyr.grid.cell_rect(i, j)[3]
                sums = [cell_measure(val, k) for k in kinds]
                owners = []
                for k, kind in enumerate(("point", "line", "polygon")):
                    fid, m = cell_owner(val, kind)
                    if fid >= 0:
                        owners.append(f"{kind}:{fid}={m:.6g}")
                wr.writerow([i, j, f"{x:.6g}", f"{y:.6g}", *[f"{s:.6g}" for s in sums],
                             "|".join(owners)])
