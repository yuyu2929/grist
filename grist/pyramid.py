"""Pyramid: aggregated multi-level coverage, content-addressed tiles, incremental updates.

Structure
---------
- level 0 holds the exact cell integrals produced by the coverage kernel;
- level l+1 is the exact area-weighted aggregation of level l (THEORY.md thm 2);
- every level is cut into T x T tiles; each tile is serialized canonically
  and stored in a content-addressed store keyed by blake2b(tile bytes);
- the pyramid manifest maps (level, tile) -> tile hash.

Incremental update recomputes only the level-0 tiles touched by edited
features, then propagates changed aggregates upward. Unchanged tiles keep
their hash, which gives dedup, diff and integrity for free.

Determinism note: float sums are order-sensitive, so aggregation always
merges a coarse cell's children in the canonical order
(0,0), (1,0), (0,1), (1,1) -- both in full rebuilds and in incremental
parent recomputation. An updated pyramid is therefore byte-identical to a
fresh bake of the same feature set.
"""
from __future__ import annotations

import math
import struct
from hashlib import blake2b
from typing import Iterable, Optional

from .coverage import line_lengths, point_counts, polygon_areas
from .features import Feature, index_features
from .grid import Grid

# CellValue = (points, lines, polys); each part is a tuple of (fid, measure)
# sorted by fid. Empty parts are ().
CellValue = tuple[tuple, tuple, tuple]

TILE_MAGIC = b"GRT1"
_HDR = struct.Struct("<4sIII")    # magic, level, tw, th -- NO tile position:
                                 # position lives in the manifest, so tiles
                                 # with identical content at different
                                 # positions dedup to one blob.
_CNT = struct.Struct("<I")         # cell count
_CELL = struct.Struct("<II")       # cell i, j

_KINDS = ("point", "line", "polygon")


def _merge_into(dst: dict[int, float], src: Iterable[tuple[int, float]]) -> None:
    for fid, v in src:
        dst[fid] = dst.get(fid, 0.0) + v


def _freeze(d: dict[int, float]) -> tuple:
    return tuple(sorted(d.items()))


class Tile:
    """One tile's cells, keyed by TILE-RELATIVE (i, j) in [0, tw) x [0, th).

    Relative keys are what make content addressing useful: two tiles with
    identical content at different grid positions serialize to identical
    bytes and share one blob. Absolute position lives in the manifest.
    """
    __slots__ = ("level", "ti", "tj", "tw", "th", "cells")

    def __init__(self, level: int, ti: int, tj: int, tw: int, th: int,
                 cells: dict[tuple[int, int], CellValue]):
        self.level = level
        self.ti = ti            # manifest position; NOT part of the blob
        self.tj = tj
        self.tw = tw
        self.th = th
        self.cells = cells

    def is_empty(self) -> bool:
        return not self.cells

    def canonical_bytes(self) -> bytes:
        parts = [_HDR.pack(TILE_MAGIC, self.level, self.tw, self.th),
                 _CNT.pack(len(self.cells))]
        for (i, j) in sorted(self.cells):
            pts, lns, polys = self.cells[(i, j)]
            parts.append(_CELL.pack(i, j))
            for lst in (pts, lns, polys):
                if len(lst) > 0xFFFF:
                    raise ValueError("cell mixture exceeds 65535 entries")
                parts.append(struct.pack("<H", len(lst)))
                for fid, v in lst:
                    parts.append(struct.pack("<Id", fid, v))
        return b"".join(parts)

    @classmethod
    def from_bytes(cls, data: bytes) -> "Tile":
        magic, level, tw, th = _HDR.unpack_from(data, 0)
        if magic != TILE_MAGIC:
            raise ValueError("bad tile magic")
        off = _HDR.size
        (n,) = _CNT.unpack_from(data, off)
        off += _CNT.size
        cells: dict[tuple[int, int], CellValue] = {}
        for _ in range(n):
            i, j = _CELL.unpack_from(data, off)
            off += _CELL.size
            lists = []
            for _k in range(3):
                (cnt,) = struct.unpack_from("<H", data, off)
                off += 2
                lst = []
                for _e in range(cnt):
                    fid, v = struct.unpack_from("<Id", data, off)
                    off += 12
                    lst.append((fid, v))
                lists.append(tuple(lst))
            cells[(i, j)] = (lists[0], lists[1], lists[2])
        # position (ti, tj) is recovered from the manifest by the caller
        return cls(level, 0, 0, tw, th, cells)


class TileStore:
    """Content-addressed blob store (in memory)."""

    def __init__(self):
        self._blobs: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        h = blake2b(data, digest_size=32).hexdigest()
        self._blobs[h] = data
        return h

    def get(self, h: str) -> bytes:
        return self._blobs[h]

    def __contains__(self, h: str) -> bool:
        return h in self._blobs

    def __len__(self) -> int:
        return len(self._blobs)

    def referenced(self, levels: Iterable[dict[tuple[int, int], str]]) -> set[str]:
        ref = set()
        for level in levels:
            ref.update(level.values())
        return ref

    def prune(self, levels: Iterable[dict[tuple[int, int], str]]) -> int:
        """Drop blobs no longer referenced by the manifest; returns count."""
        keep = self.referenced(levels)
        for h in [h for h in self._blobs if h not in keep]:
            del self._blobs[h]
        return len(self._blobs)

    def total_bytes(self) -> int:
        return sum(len(b) for b in self._blobs.values())


class Pyramid:
    def __init__(self, grid: Grid, tile_size: int, levels: list[dict[tuple[int, int], str]],
                 store: TileStore, features: dict[int, Feature]):
        self.grid = grid
        self.tile_size = tile_size
        self.levels = levels              # per level: (ti, tj) -> tile hash
        self.store = store
        self.features = features
        self._tile_cache: dict[str, Tile] = {}

    # -- geometry of levels ------------------------------------------------
    def n_levels(self) -> int:
        return len(self.levels)

    def level_dims(self, level: int) -> tuple[int, int]:
        f = 1 << level
        return (-(-self.grid.w // f), -(-self.grid.h // f))

    def tile_grid(self, level: int) -> tuple[int, int]:
        w, h = self.level_dims(level)
        T = self.tile_size
        return (-(-w // T), -(-h // T))

    def tile_cells(self, level: int, ti: int, tj: int) -> tuple[int, int, int, int]:
        """(i0, i1, j0, j1) inclusive cell range covered by the tile."""
        w, h = self.level_dims(level)
        T = self.tile_size
        return (ti * T, min((ti + 1) * T, w) - 1, tj * T, min((tj + 1) * T, h) - 1)

    def tile_rect(self, level: int, ti: int, tj: int) -> tuple[float, float, float, float]:
        i0, i1, j0, j1 = self.tile_cells(level, ti, tj)
        return (self.grid.x0 + i0 * self.grid.cell,
                self.grid.ytop - (j1 + 1) * self.grid.cell,
                self.grid.x0 + (i1 + 1) * self.grid.cell,
                self.grid.ytop - j0 * self.grid.cell)

    # -- tile access -------------------------------------------------------
    def get_tile(self, h: str) -> Tile:
        t = self._tile_cache.get(h)
        if t is None:
            t = Tile.from_bytes(self.store.get(h))
            self._tile_cache[h] = t
        return t

    def level_cell(self, level: int, i: int, j: int) -> Optional[CellValue]:
        T = self.tile_size
        h = self.levels[level].get((i // T, j // T))
        if h is None:
            return None
        return self.get_tile(h).cells.get((i % T, j % T))

    def cell_value(self, level: int, i: int, j: int) -> Optional[CellValue]:
        return self.level_cell(level, i, j)

    # -- totals -------------------------------------------------------------
    def totals(self) -> dict[str, float]:
        """Sum of all measures at level 0 (conservation check).

        Deterministic BY CONSTRUCTION: every manifest REFERENCE is visited
        (not the set of distinct blob hashes -- content-addressed tiles are
        shared by identical tiles at different positions, and counting each
        blob once would undercount exactly by the dedup win; see DEVLOG
        BUG 20), tiles in sorted hash order and cells in sorted (i, j)
        order, and each kind is summed with math.fsum (exactly rounded,
        hence permutation-independent). Two pyramids with identical content
        therefore report identical totals no matter how they were built --
        fresh bake, parallel band merge, or incremental update -- and no
        matter what the process hash seed is.
        """
        vals: dict[str, list[float]] = {k: [] for k in _KINDS}
        for h in sorted(self.levels[0].values()):
            tile = self.get_tile(h)
            for key in sorted(tile.cells):
                pts, lns, polys = tile.cells[key]
                for k, lst in (("point", pts), ("line", lns), ("polygon", polys)):
                    for fid, v in lst:
                        vals[k].append(v)
        return {k: math.fsum(v) for k, v in vals.items()}

    # -- building ------------------------------------------------------------
    @classmethod
    def from_acc(cls, acc: dict[tuple[int, int], dict[str, dict[int, float]]],
                 grid: Grid, tile_size: int = 16,
                 max_levels: Optional[int] = None,
                 features: Optional[dict[int, Feature]] = None) -> "Pyramid":
        """Build from a merged level-0 accumulator (see bake_parallel)."""
        store = TileStore()
        level0 = _build_level(acc, grid, tile_size, 0, store)
        self = cls(grid, tile_size, [level0], store, dict(features or {}))
        while True:
            top = self.n_levels() - 1
            w, h = self.level_dims(top)
            if max_levels is not None and self.n_levels() >= max_levels:
                break
            if w <= 1 and h <= 1:
                break
            self.levels.append(self._aggregate_level(top))
        return self

    @classmethod
    def bake(cls, features: Iterable[Feature], grid: Grid, tile_size: int = 16,
             max_levels: Optional[int] = None) -> "Pyramid":
        feats = index_features(features)
        store = TileStore()
        acc: dict[tuple[int, int], dict[str, dict[int, float]]] = {}
        for fid in sorted(feats):
            f = feats[fid]
            if f.kind == "point":
                for cell, n in point_counts(f.points, grid).items():
                    _acc_add(acc, cell, "point", fid, float(n))
            elif f.kind == "line":
                for cell, v in line_lengths(f.lines, grid).items():
                    _acc_add(acc, cell, "line", fid, v)
            else:
                for cell, v in polygon_areas(f.polys, grid).items():
                    _acc_add(acc, cell, "polygon", fid, v)
        level0 = _build_level(acc, grid, tile_size, 0, store)
        self = cls(grid, tile_size, [level0], store, feats)
        while True:
            top = self.n_levels() - 1
            w, h = self.level_dims(top)
            if max_levels is not None and self.n_levels() >= max_levels:
                break
            if w <= 1 and h <= 1:
                break
            self.levels.append(self._aggregate_level(top))
        return self

    def _aggregate_level(self, fine_idx: int) -> dict[tuple[int, int], str]:
        """Exact aggregation of level fine_idx into level fine_idx+1.

        Coarse cells are visited in row-major order and each merges its
        children in the canonical order (0,0),(1,0),(0,1),(1,1); this is the
        same order used by incremental parent rebuilds, keeping outputs
        byte-identical.
        """
        level = fine_idx + 1
        w, h = self.level_dims(level)
        fw, fh = self.level_dims(fine_idx)
        acc: dict[tuple[int, int], dict[str, dict[int, float]]] = {}
        for J in range(h):
            for I in range(w):
                d = None
                for ci, cj in ((2 * I, 2 * J), (2 * I + 1, 2 * J), (2 * I, 2 * J + 1), (2 * I + 1, 2 * J + 1)):
                    if ci >= fw or cj >= fh:
                        continue
                    val = self.level_cell(fine_idx, ci, cj)
                    if val is None:
                        continue
                    if d is None:
                        d = {k: {} for k in _KINDS}
                    for k, kind in enumerate(_KINDS):
                        _merge_into(d[kind], val[k])
                if d is not None:
                    acc[(I, J)] = d
        return _build_level(acc, self.grid, self.tile_size, level, self.store)

    # -- incremental update ---------------------------------------------------
    def update(self, removed_fids: Iterable[int] = (), added: Iterable[Feature] = ()) -> dict:
        """Apply edits in place; returns per-level changed-tile report.

        Correctness contract: after update(), the pyramid is byte-identical
        to a fresh bake of the same feature set on the same grid.

        Cost model (v1): only tiles whose bbox intersects an EDITED
        feature's bbox are recomputed ("dirty" tiles). Each feature
        overlapping a dirty tile has its coverage computed exactly once
        and cached for the whole update, so the kernel cost is
        O(sum of overlapping feature bboxes), independent of dataset
        size. Changed tiles propagate upward one level per iteration.
        """
        removed_feats = [self.features[fid] for fid in removed_fids if fid in self.features]
        for fid in removed_fids:
            self.features.pop(fid, None)
        added_feats = list(added)
        for f in added_feats:
            self.features[f.fid] = f

        # 1. dirty level-0 tiles = tiles touched by edited features
        #    (bbox-derived, conservatively expanded by one tile per side)
        tw, th = self.tile_grid(0)
        ct = self.grid.cell * self.tile_size
        dirty: set[tuple[int, int]] = set()
        for f in removed_feats + added_feats:
            b = f.bbox()
            if b is None:
                continue
            ti0 = max(0, int((b[0] - self.grid.x0) / ct) - 1)
            ti1 = min(tw - 1, int((b[2] - self.grid.x0) / ct) + 1)
            tj0 = max(0, int((self.grid.ytop - b[3]) / ct) - 1)
            tj1 = min(th - 1, int((self.grid.ytop - b[1]) / ct) + 1)
            for ti in range(ti0, ti1 + 1):
                for tj in range(tj0, tj1 + 1):
                    dirty.add((ti, tj))
        if not dirty:
            return {"changed": {}, "tiles": {}}
        dirty_rects = [self.tile_rect(0, ti, tj) for (ti, tj) in sorted(dirty)]

        # 2. per-feature coverage cache for features overlapping dirty tiles
        cov: dict[int, tuple[str, dict]] = {}
        for fid in sorted(self.features):
            f = self.features[fid]
            b = f.bbox()
            if b is None:
                continue
            if not any(_bbox_touches_rect(b, r) for r in dirty_rects):
                continue
            cov[fid] = self._feature_coverage(f)

        report = {"changed": {}, "tiles": {}}
        changed_prev: set[tuple[int, int]] = set()
        for lvl in range(self.n_levels()):
            candidates = dirty if lvl == 0 else {(ti // 2, tj // 2) for (ti, tj) in changed_prev}
            changed_now: set[tuple[int, int]] = set()
            for (ti, tj) in sorted(candidates):
                new_tile = self._rebuild_tile0(ti, tj, cov) if lvl == 0 else self._rebuild_parent(lvl, ti, tj)
                old = self.levels[lvl].get((ti, tj))
                if new_tile is None or new_tile.is_empty():
                    if old is not None:
                        self.levels[lvl].pop((ti, tj), None)
                        changed_now.add((ti, tj))
                    continue
                h = self.store.put(new_tile.canonical_bytes())
                if h != old:
                    self.levels[lvl][(ti, tj)] = h
                    changed_now.add((ti, tj))
            report["changed"][lvl] = len(changed_now)
            report["tiles"][lvl] = sorted(changed_now)
            changed_prev = changed_now
            if not changed_prev:
                break
        return report

    def _feature_coverage(self, f: Feature) -> tuple[str, dict]:
        """Exact level-0 coverage of one feature over the whole grid."""
        if f.kind == "point":
            return "point", {c: float(n) for c, n in point_counts(f.points, self.grid).items()}
        if f.kind == "line":
            return "line", dict(line_lengths(f.lines, self.grid))
        return "polygon", dict(polygon_areas(f.polys, self.grid))

    def _rebuild_tile0(self, ti: int, tj: int, cov: dict[int, tuple[str, dict]]) -> Optional[Tile]:
        i0, i1, j0, j1 = self.tile_cells(0, ti, tj)
        acc: dict[tuple[int, int], dict[str, dict[int, float]]] = {}
        for fid in sorted(cov):
            kind, cells = cov[fid]
            for cell, v in cells.items():
                if i0 <= cell[0] <= i1 and j0 <= cell[1] <= j1:
                    _acc_add(acc, (cell[0] - i0, cell[1] - j0), kind, fid, v)
        if not acc:
            return None
        return _tile_from_acc(acc, 0, ti, tj, i1 - i0 + 1, j1 - j0 + 1)

    def _rebuild_parent(self, lvl: int, ti: int, tj: int) -> Optional[Tile]:
        """Rebuild one coarse tile from its children.

        Each coarse cell merges its (up to four) children in the canonical
        order (0,0),(1,0),(0,1),(1,1) -- identical to _aggregate_level --
        even when several children share a tile (iterating a tile's cells in
        sorted order would merge (i,j),(i,j+1),(i+1,j),(i+1,j+1), a different
        float summation order and hence different bytes).
        """
        acc: dict[tuple[int, int], dict[str, dict[int, float]]] = {}
        w, h = self.level_dims(lvl)
        T = self.tile_size
        tw = min(T, w - ti * T)
        th = min(T, h - tj * T)
        for J in range(th):
            for I in range(tw):
                d = None
                for ci, cj in ((2 * (ti * T + I), 2 * (tj * T + J)),
                               (2 * (ti * T + I) + 1, 2 * (tj * T + J)),
                               (2 * (ti * T + I), 2 * (tj * T + J) + 1),
                               (2 * (ti * T + I) + 1, 2 * (tj * T + J) + 1)):
                    h_ = self.levels[lvl - 1].get((ci // T, cj // T))
                    if h_ is None:
                        continue
                    val = self.get_tile(h_).cells.get((ci % T, cj % T))
                    if val is None:
                        continue
                    if d is None:
                        d = {k: {} for k in _KINDS}
                    for k, kind in enumerate(_KINDS):
                        _merge_into(d[kind], val[k])
                if d is not None:
                    acc[(I, J)] = d
        if not acc:
            return None
        return _tile_from_acc(acc, lvl, ti, tj, tw, th)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _bbox_touches_rect(b, r) -> bool:
    """Can a closed bbox possibly contribute to a HALF-OPEN tile rect?

    Inclusive on both sides on purpose.  The tile rect is half-open
    ([x0,x1) x [ybot,ytop)), so a point or a line lying exactly on the tile's
    left/bottom edge belongs to this tile and must be recomputed; a strict
    overlap test (`b[2] > r[0]`) silently dropped it, so an incremental update
    lost boundary-coincident geometry while a fresh bake kept it (DEVLOG
    BUG 21).  Over-including costs a little work; under-including is wrong.
    """
    return (b[2] >= r[0] and b[0] <= r[2] and b[3] >= r[1] and b[1] <= r[3])


def _acc_add(acc, cell, kind, fid, value):
    d = acc.setdefault(cell, {k: {} for k in _KINDS})
    m = d[kind]
    m[fid] = m.get(fid, 0.0) + value


def _tile_from_acc(acc, level, ti, tj, tw, th) -> Tile:
    cells = {}
    for cell, d in acc.items():
        cells[cell] = (_freeze(d["point"]), _freeze(d["line"]), _freeze(d["polygon"]))
    return Tile(level, ti, tj, tw, th, cells)


def _build_level(acc, grid, tile_size, level, store) -> dict[tuple[int, int], str]:
    w, h = _level_dims(grid, level)
    T = tile_size
    buckets: dict[tuple[int, int], dict[tuple[int, int], CellValue]] = {}
    for cell, d in acc.items():
        i, j = cell
        buckets.setdefault((i // T, j // T), {})[(i % T, j % T)] = (
            _freeze(d["point"]), _freeze(d["line"]), _freeze(d["polygon"]))
    tiles: dict[tuple[int, int], str] = {}
    for (ti, tj), cells in buckets.items():
        tw = min(T, w - ti * T)
        th = min(T, h - tj * T)
        tiles[(ti, tj)] = store.put(Tile(level, ti, tj, tw, th, cells).canonical_bytes())
    return tiles


def _level_dims(grid: Grid, level: int) -> tuple[int, int]:
    f = 1 << level
    return (-(-grid.w // f), -(-grid.h // f))
