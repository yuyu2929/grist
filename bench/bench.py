"""Benchmark: Grist vs classic rasterization strategies.

Baselines (all re-implemented here from their defining rules, no external
deps beyond numpy/shapely for the oracle):

  1. center-burn   a cell is "on" iff its center is inside the polygon
                   (GDAL's default CENTER_BURNED rule; biased sampling)
  2. supersample   average of a 16x16 regular subsample grid per cell
                   (QGIS/GDAL ALL_TOUCHED-adjacent practice; converges but
                   never exact, error unbounded below any tolerance for
                   polygons with thin features)
  3. naive-exact   per-cell exact area via shapely intersection with the
                   cell box (the "obvious" exact method; O(cells x rings))
  4. coarse-independent   rasterize each pyramid level independently with
                   center-burn (COG-style pyramids; levels disagree)

Measured:
  - accuracy  vs shapely per-cell area oracle (max/mean error, mass error)
  - speed     wall time on fixed datasets
  - consistency   level-l vs level-0 totals (aggregation drift)
  - incremental    cost of adding one feature (touched tiles vs full bake)
  - storage        unique blob bytes vs naive per-level arrays

Run: python3 bench.py [--quick]
"""
from __future__ import annotations

import os
import math
import random
import sys
import time

import numpy as np
from shapely.geometry import Polygon as ShapelyPolygon, box as shapely_box

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grist import Grid, Pyramid, codec
from grist.coverage import polygon_areas
from grist.features import Feature
from grist.query import dense_matrix

RNG = random.Random(20240607)


# --------------------------------------------------------------------------
# datasets
# --------------------------------------------------------------------------

def make_random_polygons(n: int, extent: float = 100.0, seed: int = 1) -> list[Feature]:
    """Star-shaped random polygons with random concavity and holes."""
    rng = random.Random(seed)
    feats = []
    for i in range(n):
        cx, cy = rng.uniform(0, extent), rng.uniform(0, extent)
        r_out = rng.uniform(2.0, 9.0)
        n_out = rng.randint(5, 14)
        # radial modulation -> concavity
        radii = [r_out * rng.uniform(0.55, 1.0) for _ in range(n_out)]
        ring = [(cx + radii[k] * math.cos(2 * math.pi * k / n_out),
                 cy + radii[k] * math.sin(2 * math.pi * k / n_out)) for k in range(n_out)]
        polys = [[ring]]
        if rng.random() < 0.35 and r_out > 5.0:
            r_in = r_out * 0.35
            n_in = rng.randint(3, 7)
            hole = [(cx + r_in * math.cos(2 * math.pi * k / n_in),
                     cy + r_in * math.sin(2 * math.pi * k / n_in)) for k in range(n_in)]
            polys[0].append(hole)
        feats.append(Feature(i, "polygon", polys=polys, label=f"p{i}"))
    return feats


def make_thin_polygons(n: int, extent: float = 100.0, seed: int = 2) -> list[Feature]:
    """Long thin slivers: the adversarial case for supersampling."""
    rng = random.Random(seed)
    feats = []
    for i in range(n):
        x0, y0 = rng.uniform(0, extent - 20), rng.uniform(5, extent - 5)
        x1 = x0 + rng.uniform(15.0, 25.0)
        w = rng.uniform(0.05, 0.4)          # thinner than one cell in most grids
        s = rng.uniform(-0.15, 0.15) * (x1 - x0)   # constant slope -> valid quad
        ring = [(x0, y0), (x1, y0 + s), (x1, y0 + s + w), (x0, y0 + w)]
        feats.append(Feature(1000 + i, "polygon", polys=[[ring]], label=f"sliver{i}"))
    return feats


def make_circles(n: int, extent: float = 100.0, seed: int = 3, r: float = 6.0) -> list[Feature]:
    rng = random.Random(seed)
    feats = []
    for i in range(n):
        cx, cy = rng.uniform(r, extent - r), rng.uniform(r, extent - r)
        k = 48
        ring = [(cx + r * math.cos(2 * math.pi * t / k),
                 cy + r * math.sin(2 * math.pi * t / k)) for t in range(k)]
        feats.append(Feature(2000 + i, "polygon", polys=[[ring]], label=f"c{i}"))
    return feats


# --------------------------------------------------------------------------
# baselines
# --------------------------------------------------------------------------

def baseline_center_burn(feats: list[Feature], grid: Grid) -> np.ndarray:
    """Cell is on iff its center lies inside any polygon (even-odd over rings)."""
    from grist.geom import point_in_rings
    out = np.zeros((grid.h, grid.w), dtype=float)
    cell_area = grid.cell ** 2
    for j in range(grid.h):
        yc = grid.ytop - (j + 0.5) * grid.cell
        for i in range(grid.w):
            xc = grid.x0 + (i + 0.5) * grid.cell
            for f in feats:
                if f.kind != "polygon":
                    continue
                if any(point_in_rings((xc, yc), poly) for poly in f.polys):
                    out[j, i] += cell_area
                    break
    return out


def baseline_supersample(feats: list[Feature], grid: Grid, ss: int = 16) -> np.ndarray:
    """Average of ss x ss subsamples per cell (area estimate)."""
    from shapely.geometry import Point
    shapes = []
    for f in feats:
        if f.kind != "polygon":
            continue
        for poly in f.polys:
            shapes.append(ShapelyPolygon(poly[0], poly[1:]))
    if not shapes:
        return np.zeros((grid.h, grid.w), dtype=float)
    from shapely import STRtree
    tree = STRtree(shapes)
    cell_area = grid.cell ** 2
    out = np.zeros((grid.h, grid.w), dtype=float)
    for j in range(grid.h):
        y0 = grid.ytop - (j + 1) * grid.cell
        for i in range(grid.w):
            x0 = grid.x0 + i * grid.cell
            hits = 0
            for sy in range(ss):
                y = y0 + (sy + 0.5) * grid.cell / ss
                for sx in range(ss):
                    x = x0 + (sx + 0.5) * grid.cell / ss
                    p = Point(x, y)
                    for idx in tree.query(p):
                        if shapes[idx].contains(p):
                            hits += 1
                            break
            out[j, i] = hits * cell_area / (ss * ss)
    return out


def baseline_naive_exact(feats: list[Feature], grid: Grid) -> np.ndarray:
    """Exact per-cell area via shapely intersection (the obvious exact way)."""
    shapes = []
    for f in feats:
        if f.kind != "polygon":
            continue
        for poly in f.polys:
            shapes.append(ShapelyPolygon(poly[0], poly[1:]))
    out = np.zeros((grid.h, grid.w), dtype=float)
    for j in range(grid.h):
        y1 = grid.ytop - j * grid.cell
        for i in range(grid.w):
            x0 = grid.x0 + i * grid.cell
            b = shapely_box(x0, y1 - grid.cell, x0 + grid.cell, y1)
            a = 0.0
            for s in shapes:
                if s.intersects(b):
                    a += s.intersection(b).area
            out[j, i] = a
    return out


def oracle_exact(feats: list[Feature], grid: Grid) -> np.ndarray:
    """Same as naive_exact (shapely is the trusted oracle)."""
    return baseline_naive_exact(feats, grid)


# --------------------------------------------------------------------------
# grist side
# --------------------------------------------------------------------------

def grist_matrix(feats: list[Feature], grid: Grid, tile_size: int = 16) -> np.ndarray:
    pyr = Pyramid.bake(feats, grid, tile_size=tile_size)
    m = dense_matrix(pyr, level=0, kind="polygon")
    return np.array(m) * (grid.cell ** 2), pyr


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def err_stats(name: str, got: np.ndarray, ref: np.ndarray, cell_area: float) -> dict:
    diff = got - ref
    max_abs = float(np.abs(diff).max())
    mean_abs = float(np.abs(diff).mean())
    mass_err = float(abs(diff.sum()))          # total area error
    return {"name": name, "max_cell_err": max_abs / cell_area,
            "mean_cell_err": mean_abs / cell_area, "mass_err": mass_err,
            "neg": float((diff < -1e-12).sum()), "pos": float((diff > 1e-12).sum())}


def fmt_err(d: dict) -> str:
    return (f"{d['name']:20s} max_cell={d['max_cell_err']:.4f} "
            f"mean_cell={d['mean_cell_err']:.5f} mass={d['mass_err']:.3e} "
            f"(cells off: {int(d['neg'])} under / {int(d['pos'])} over)")


def run_dataset(title: str, feats: list[Feature], grid: Grid, quick: bool = False):
    print(f"\n=== {title}: {len(feats)} features, grid {grid.w}x{grid.h} "
          f"(cell {grid.cell:.4f}) ===")
    cell_area = grid.cell ** 2
    ref = oracle_exact(feats, grid)
    ref_mass = float(ref.sum())

    # accuracy
    t0 = time.perf_counter()
    g_mat, pyr = grist_matrix(feats, grid)
    t_grist = time.perf_counter() - t0
    e_grist = err_stats("grist", g_mat, ref, cell_area)

    t0 = time.perf_counter()
    cb = baseline_center_burn(feats, grid)
    t_cb = time.perf_counter() - t0
    e_cb = err_stats("center-burn", cb, ref, cell_area)

    ss = 16
    t0 = time.perf_counter()
    sup = baseline_supersample(feats, grid, ss=ss)
    t_sup = time.perf_counter() - t0
    e_sup = err_stats(f"supersample{ss}", sup, ref, cell_area)

    if not quick:
        t0 = time.perf_counter()
        naive = baseline_naive_exact(feats, grid)
        t_naive = time.perf_counter() - t0
        e_naive = err_stats("naive-exact", naive, ref, cell_area)
    else:
        t_naive = e_naive = None

    print(fmt_err(e_grist))
    print(fmt_err(e_cb))
    print(fmt_err(e_sup))
    if e_naive:
        print(fmt_err(e_naive))

    # speed
    print(f"\nspeed: grist={t_grist*1000:.1f}ms  center-burn={t_cb*1000:.1f}ms  "
          f"supersample{ss}={t_sup*1000:.1f}ms"
          + (f"  naive-exact={t_naive*1000:.1f}ms" if t_naive else ""))
    print(f"mass: ref={ref_mass:.6f} grist={float(g_mat.sum()):.6f}")

    # multi-level consistency (Grist: level totals == level-0 totals)
    lvl_tot = pyr.totals()["polygon"]
    drift = abs(lvl_tot - ref_mass) / max(1.0, ref_mass)
    print(f"aggregation drift (level totals vs exact mass): {drift:.3e}")

    # coarse-independent baseline: center-burn at level 2 vs level 0
    f2 = 4
    g2 = Grid(grid.x0, grid.ytop, grid.cell * f2, grid.w // f2, grid.h // f2)
    cb2 = baseline_center_burn(feats, g2)
    mass0 = float(cb.sum())
    mass2 = float(cb2.sum())
    print(f"coarse-independent center-burn mass drift: "
          f"{abs(mass2 - mass0) / max(1e-12, mass0):.3%} "
          f"(level0={mass0:.3f} level2={mass2:.3f})")

    # incremental cost: add one feature, measure touched tiles
    new_feat = make_random_polygons(1, extent=100.0, seed=99)[0]
    new_feat = Feature(9001, "polygon", polys=new_feat.polys)
    t0 = time.perf_counter()
    report = pyr.update(removed_fids=(), added=[new_feat])
    t_inc = time.perf_counter() - t0
    t0 = time.perf_counter()
    fresh = Pyramid.bake(list(feats) + [new_feat], grid, tile_size=16)
    t_full = time.perf_counter() - t0
    print(f"incremental add-1-feature: {t_inc*1000:.1f}ms "
          f"(tiles changed per level: {report['changed']}) "
          f"vs full rebake {t_full*1000:.1f}ms  "
          f"speedup {t_full / max(t_inc, 1e-9):.1f}x")
    print(f"update == fresh bake (bytes): "
          f"{codec.serialize(pyr) == codec.serialize(fresh)}")

    # storage: grist blobs vs naive per-level dense arrays. Grist cells
    # carry the full mixture (fid ownership + measure), which a dense
    # float array per kind cannot represent; the honest comparison is
    # 3 arrays (point/line/polygon) x all levels.
    naive_bytes = 0
    for lvl in range(pyr.n_levels()):
        w, h = pyr.level_dims(lvl)
        naive_bytes += w * h * 8
    print(f"storage: grist blobs={pyr.store.total_bytes()}B "
          f"unique={len(pyr.store)} tiles={sum(len(l) for l in pyr.levels)} "
          f"vs dense-array levels={naive_bytes}B (1 kind) / {3*naive_bytes}B (mixture-equiv)")
    return e_grist, e_cb, e_sup


def run_sparse_edit(n: int = 600, extent: float = 400.0, grid_n: int = 400):
    """Incremental update at scale: one small edit in a large dataset.

    This is where content-addressed tiles + touched-tile propagation pay
    off: the edit recomputes a handful of tiles, not the dataset.
    """
    print(f"\n=== sparse edit at scale: {n} small polygons, "
          f"{grid_n}x{grid_n} grid ===")
    feats = make_random_polygons(n, extent=extent, seed=21)
    grid = Grid(0.0, extent, extent / grid_n, grid_n, grid_n)
    t0 = time.perf_counter()
    pyr = Pyramid.bake(feats, grid, tile_size=16)
    t_bake = time.perf_counter() - t0
    print(f"initial bake: {t_bake*1000:.1f}ms "
          f"({len(pyr.store)} unique tiles, {pyr.store.total_bytes()//1024} KiB)")

    # a small edit in one corner
    rng = random.Random(22)
    edit = Feature(99999, "polygon",
                   polys=[[[(5.0, 5.0), (7.0, 5.0), (7.0, 7.0), (5.0, 7.0)]]],
                   label="edit")
    t0 = time.perf_counter()
    report = pyr.update(removed_fids=(), added=[edit])
    t_inc = time.perf_counter() - t0
    t0 = time.perf_counter()
    fresh = Pyramid.bake(list(feats) + [edit], grid, tile_size=16)
    t_full = time.perf_counter() - t0
    print(f"incremental edit: {t_inc*1000:.1f}ms, tiles changed "
          f"{report['changed']}")
    print(f"full rebake     : {t_full*1000:.1f}ms  -> speedup "
          f"{t_full / max(t_inc, 1e-9):.1f}x")
    print(f"update == fresh bake (bytes): "
          f"{codec.serialize(pyr) == codec.serialize(fresh)}")
    # dedup: identical tiles across levels share blobs
    naive = sum(pyr.level_dims(l)[0] * pyr.level_dims(l)[1] * 8
                for l in range(pyr.n_levels()))
    print(f"storage: {pyr.store.total_bytes()//1024} KiB grist blobs vs "
          f"{naive//1024} KiB dense arrays (1 kind) / {3*naive//1024} KiB "
          f"(mixture-equivalent: 3 arrays x all levels)")


def main():
    quick = "--quick" in sys.argv
    print("Grist benchmark -- exact coverage rasterization")
    print("=" * 72)

    feats = make_random_polygons(40, extent=100.0, seed=11)
    grid = Grid(0.0, 100.0, 1.0, 100, 100)
    run_dataset("random polygons w/ holes (100x100 grid)", feats, grid, quick)

    feats = make_circles(20, extent=100.0, seed=12)
    grid = Grid(0.0, 100.0, 0.5, 200, 200)
    run_dataset("circles (200x200 grid)", feats, grid, quick)

    feats = make_thin_polygons(15, extent=100.0, seed=13)
    grid = Grid(0.0, 100.0, 1.0, 100, 100)
    run_dataset("thin slivers (adversarial for sampling)", feats, grid, quick)

    run_sparse_edit()

    print("\n" + "=" * 72)
    print("done.")


if __name__ == "__main__":
    main()
