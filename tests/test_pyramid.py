"""Pyramid: aggregation consistency, determinism, incremental update."""
import os
import math
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grist import Grid, Pyramid, codec
from grist.features import Feature


def square_feats():
    return [
        Feature(0, "polygon", polys=[[[(0, 0), (10, 0), (10, 10), (0, 10)]]]),
        Feature(1, "polygon", polys=[[[(2, 2), (8, 2), (8, 8), (2, 8)],
                                      [(4, 4), (6, 4), (6, 6), (4, 6)]]]),
        Feature(2, "line", lines=[[(0, 0), (10, 10)]]),
        Feature(3, "point", points=[(3.5, 7.25)]),
    ]


def mixed_feats(n=40, extent=50.0, seed=5):
    rng = random.Random(seed)
    feats = []
    for i in range(n):
        cx, cy = rng.uniform(5, extent - 5), rng.uniform(5, extent - 5)
        r = rng.uniform(1.0, 5.0)
        k = rng.randint(3, 10)
        ring = [(cx + r * math.cos(2 * math.pi * t / k),
                 cy + r * math.sin(2 * math.pi * t / k)) for t in range(k)]
        kind_roll = rng.random()
        if kind_roll < 0.6:
            feats.append(Feature(i, "polygon", polys=[[ring]]))
        elif kind_roll < 0.8:
            feats.append(Feature(i, "line", lines=[[(cx, cy), (cx + r, cy + r)]]))
        else:
            feats.append(Feature(i, "point", points=[(cx, cy)]))
    return feats


def test_bake_conservation():
    feats = square_feats()
    pyr = Pyramid.bake(feats, Grid(0.0, 10.0, 1.0, 10, 10))
    t = pyr.totals()
    assert abs(t["point"] - 1.0) < 1e-12
    assert abs(t["line"] - 10 * math.sqrt(2)) < 1e-9
    assert abs(t["polygon"] - (100 + 32)) < 1e-9


def test_aggregation_consistency():
    """Level totals are level-invariant: a coarse cell's measure is the
    exact measure inside it, so summing per kind is conserved."""
    feats = mixed_feats(30, extent=40.0)
    grid = Grid(0.0, 40.0, 1.0, 40, 40)
    pyr = Pyramid.bake(feats, grid)
    base = pyr.totals()
    for lvl in range(1, pyr.n_levels()):
        vals = {"point": [], "line": [], "polygon": []}
        for h in sorted(pyr.levels[lvl].values()):   # every reference: identical
            for key in sorted(pyr.get_tile(h).cells):   # tiles share blobs
                cell = pyr.get_tile(h).cells[key]
                for k, kind in enumerate(("point", "line", "polygon")):
                    vals[kind].extend(v for _f, v in cell[k])
        tot = {kind: math.fsum(v) for kind, v in vals.items()}
        for kind in tot:
            assert abs(tot[kind] - base[kind]) < 1e-6 * max(1.0, base[kind]), \
                f"level {lvl} {kind}: {tot[kind]} vs {base[kind]}"


def test_determinism():
    feats = mixed_feats(25, extent=30.0, seed=11)
    grid = Grid(0.0, 30.0, 0.75, 40, 40)
    a = Pyramid.bake(feats, grid)
    b = Pyramid.bake(list(reversed(feats)), grid)   # input order must not matter
    assert codec.serialize(a) == codec.serialize(b)


def test_totals_deterministic_under_permuted_construction():
    """totals() must not depend on construction path or dict/set order.

    Regression (DEVLOG BUG 16): totals() used to iterate the level-0 tile
    SET and each tile's cells in insertion order. A fresh bake (cells
    inserted in fid-sorted feature order) and a parallel band merge (cells
    inserted in band-merge order) hold identical content but different
    insertion orders, so their totals could differ by 1 ulp -- and whether
    they did depended on the process hash seed, making the suite flaky.

    The crafted values below make the old failure deterministic: naive
    left-to-right summation yields 0.0 or 1.0 depending on cell order,
    while the fixed implementation (sorted iteration + math.fsum) yields
    exactly 1.0 for both.
    """
    grid = Grid(0.0, 4.0, 1.0, 4, 4)
    base = {(0, 0): {"point": {}, "line": {}, "polygon": {0: 1.0}},
            (1, 0): {"point": {}, "line": {}, "polygon": {0: 1e16}},
            (2, 0): {"point": {}, "line": {}, "polygon": {0: -1e16}}}
    a = Pyramid.from_acc(dict(base), grid, tile_size=4)
    b = Pyramid.from_acc({k: base[k] for k in reversed(list(base))},
                         grid, tile_size=4)
    assert codec.serialize(a) == codec.serialize(b)
    assert a.totals() == b.totals() == {"point": 0.0, "line": 0.0,
                                        "polygon": 1.0}


def test_update_add_equals_fresh():
    feats = mixed_feats(20, extent=30.0, seed=3)
    grid = Grid(0.0, 30.0, 1.0, 30, 30)
    pyr = Pyramid.bake(feats, grid)
    edit = Feature(999, "polygon", polys=[[[(5, 5), (8, 5), (8, 8), (5, 8)]]])
    pyr.update(removed_fids=(), added=[edit])
    fresh = Pyramid.bake(feats + [edit], grid)
    assert codec.serialize(pyr) == codec.serialize(fresh)


def test_update_remove_equals_fresh():
    feats = mixed_feats(20, extent=30.0, seed=4)
    grid = Grid(0.0, 30.0, 1.0, 30, 30)
    pyr = Pyramid.bake(feats, grid)
    pyr.update(removed_fids=[3, 7], added=())
    fresh = Pyramid.bake([f for f in feats if f.fid not in (3, 7)], grid)
    assert codec.serialize(pyr) == codec.serialize(fresh)


def test_update_creates_empty_tiles():
    """A new feature landing in a previously empty tile must create it."""
    grid = Grid(0.0, 40.0, 1.0, 40, 40)
    pyr = Pyramid.bake([Feature(0, "polygon",
                                polys=[[[(0, 30), (4, 30), (4, 34), (0, 34)]]])], grid)
    before = set(pyr.levels[0])
    edit = Feature(1, "polygon", polys=[[[(35, 35), (39, 35), (39, 39), (35, 39)]]])
    pyr.update(removed_fids=(), added=[edit])
    after = set(pyr.levels[0])
    assert len(after) > len(before), "new tile should appear"
    fresh = Pyramid.bake([Feature(0, "polygon",
                                  polys=[[[(0, 30), (4, 30), (4, 34), (0, 34)]]]),
                          edit], grid)
    assert codec.serialize(pyr) == codec.serialize(fresh)


def test_update_removes_emptied_tiles():
    grid = Grid(0.0, 40.0, 1.0, 40, 40)
    f0 = Feature(0, "polygon", polys=[[[(0, 30), (4, 30), (4, 34), (0, 34)]]])
    f1 = Feature(1, "polygon", polys=[[[(35, 35), (39, 35), (39, 39), (35, 39)]]])
    pyr = Pyramid.bake([f0, f1], grid)
    before = len(pyr.levels[0])
    pyr.update(removed_fids=[1], added=())
    after = len(pyr.levels[0])
    assert after < before, "emptied tile should be dropped"
    assert pyr.totals()["polygon"] == 16.0


def test_update_noop():
    feats = square_feats()
    grid = Grid(0.0, 10.0, 1.0, 10, 10)
    pyr = Pyramid.bake(feats, grid)
    before = codec.serialize(pyr)
    pyr.update()
    assert codec.serialize(pyr) == before


def test_content_addressed_dedup():
    """Identical tile CONTENT (same fid, same relative cells) shares a blob.

    Realistic case: one MultiPolygon feature with repeated parts (e.g. a
    building footprint mask) filling two tiles identically.
    """
    grid = Grid(0.0, 40.0, 1.0, 40, 40)
    # one feature, two identical tile-aligned 8x8 squares
    part = [[(0, 32), (8, 32), (8, 40), (0, 40)]]
    part2 = [[(16, 24), (24, 24), (24, 32), (16, 32)]]
    f = Feature(0, "polygon", polys=[part, part2])
    pyr = Pyramid.bake([f], grid, tile_size=8)
    n_tiles = sum(len(l) for l in pyr.levels)
    assert len(pyr.store) < n_tiles, "identical tiles should dedup"
    # and the two level-0 tiles really are byte-identical
    h0 = pyr.levels[0][(0, 0)]
    h1 = pyr.levels[0][(2, 1)]
    assert h0 == h1


def test_totals_counts_shared_blobs_per_reference():
    """Content-addressed dedup must not corrupt conservation (BUG 20).

    A large uniform polygon produces many IDENTICAL interior tiles that
    share one blob. totals() must sum per manifest REFERENCE, not per
    distinct blob hash: counting each blob once undercounts by exactly
    the dedup win -- the storage optimization silently eating the
    conservation check. (A 128x128 grid over a 100x100 square shares
    5 interior tiles -> a 781.25 mass deficit if summed per blob.)
    """
    ring = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    feats = [Feature(0, "polygon", polys=[[ring]])]
    cell = (100.0 + 2e-7) / 128
    grid = Grid(-1e-7, 100.0 + 1e-7, cell, 128, 128)
    pyr = Pyramid.bake(feats, grid, tile_size=16)
    refs = list(pyr.levels[0].values())
    assert len(refs) > len(set(refs)), "fixture must produce shared tiles"
    assert abs(pyr.totals()["polygon"] - 10000.0) < 1e-6


def test_level_dims_halving():
    grid = Grid(0.0, 10.0, 1.0, 10, 10)
    pyr = Pyramid.bake(square_feats(), grid)
    assert pyr.level_dims(0) == (10, 10)
    assert pyr.level_dims(1) == (5, 5)
    assert pyr.level_dims(2) == (3, 3)     # ceil, not floor


def test_max_levels_cap():
    grid = Grid(0.0, 64.0, 1.0, 64, 64)
    pyr = Pyramid.bake(square_feats(), grid, max_levels=2)
    assert pyr.n_levels() == 2


def test_tile_rect_matches_cells():
    grid = Grid(0.0, 32.0, 1.0, 32, 32)
    pyr = Pyramid.bake(square_feats(), grid, tile_size=8)
    x0, y0, x1, y1 = pyr.tile_rect(0, 1, 2)
    assert (x0, y0, x1, y1) == (8.0, 8.0, 16.0, 16.0)


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
