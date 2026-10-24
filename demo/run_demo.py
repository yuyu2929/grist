"""Grist end-to-end demo.

Run:  python3 demo/run_demo.py
Needs: nothing but the standard library (numpy/shapely only for the
accuracy cross-check section, which is skipped when unavailable).

Shows:
  1. bake the synthetic city (GeoJSON -> exact pyramid)
  2. conservation: cell integrals sum to the exact feature measures
  3. point/cell query with mixture readout
  4. exports: dense matrix, PGM image, per-cell CSV
  5. multi-level consistency (aggregation drift)
  6. incremental update == fresh bake, byte-for-byte
  7. optional: accuracy cross-check vs shapely
"""
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import grist
from grist import codec
from grist.bake import bake_geojson
from grist.features import features_from_geojson
from grist.query import cell_measure, csv_cells, dense_matrix, pgm, point_query


def hr(title):
    print("\n" + "=" * 66)
    print(title)
    print("=" * 66)


def main():
    # 0. data
    city_path = os.path.join(HERE, "city.geojson")
    if not os.path.exists(city_path):
        print("generating demo data...")
        import make_city
        make_city.main()
    with open(city_path, encoding="utf-8") as fh:
        data = json.load(fh)
    feats = features_from_geojson(data)
    print(f"loaded {len(feats)} features "
          f"({sum(1 for f in feats if f.kind == 'polygon')} polygons, "
          f"{sum(1 for f in feats if f.kind == 'line')} lines, "
          f"{sum(1 for f in feats if f.kind == 'point')} points)")

    # 1. bake
    hr("1. bake (cell-integral semantics, exact)")
    t0 = time.perf_counter()
    # pad keeps boundary-coincident geometry (roads ending exactly on
    # the bbox edge) strictly inside the half-open grid domain
    pyr = grist.bake_geojson(data, cell_count=256, tile_size=16, pad=1e-4)
    t_bake = time.perf_counter() - t0
    g = pyr.grid
    print(f"grid {g.w}x{g.h} (cell {g.cell:.4f}), {pyr.n_levels()} levels, "
          f"{sum(len(l) for l in pyr.levels)} tiles, baked in {t_bake*1000:.0f} ms")

    # 2. conservation
    hr("2. conservation: cells sum to exact measures")
    t = pyr.totals()
    expect = {"point": 0.0, "line": 0.0, "polygon": 0.0}
    for f in feats:
        if f.kind in expect:
            expect[f.kind] += f.measure_total()
    for k in ("point", "line", "polygon"):
        print(f"  {k:7s} cells={t[k]:12.6f}  exact={expect[k]:12.6f}  "
              f"err={abs(t[k]-expect[k]):.2e}")

    # 3. query
    hr("3. query a point (mixture readout)")
    r = point_query(pyr, 50.0, 50.0)
    if r["inside"]:
        print(f"cell {r['cell']} at (50, 50):")
        val = r["value"]
        if val is None:
            print("  empty")
        else:
            for k, kind in enumerate(("point", "line", "polygon")):
                if not val[k]:
                    continue
                fid, m = r["owners"][kind]["top"]
                print(f"  {kind:7s} sum={r['owners'][kind]['sum']:.6f} "
                      f"top=fid{fid} ({m:.6f}) entries={len(val[k])}")
    # find the busiest cell
    best = None
    for h in pyr.levels[0].values():       # every reference (blobs are shared)
        for cell, val in pyr.get_tile(h).cells.items():
            n = sum(len(p) for p in val)
            if best is None or n > best[0]:
                best = (n, cell, val)
    if best:
        n, cell, val = best
        print(f"busiest cell {cell}: {n} mixture entries "
              f"(overlap preserved, not overwritten)")

    # 4. exports
    hr("4. exports")
    out = os.path.join(HERE, "out")
    os.makedirs(out, exist_ok=True)
    pgm(pyr, level=0, kind="polygon", path=os.path.join(out, "coverage.pgm"))
    print("  wrote out/coverage.pgm (white=none .. black=full coverage)")
    csv_cells(pyr, os.path.join(out, "cells.csv"))
    print("  wrote out/cells.csv (per-cell CSV with owners)")
    m = dense_matrix(pyr, level=2, kind="polygon")
    print(f"  dense matrix level 2: {len(m)}x{len(m[0])} "
          f"(coarse view, exact aggregation)")

    # 5. multi-level consistency
    hr("5. multi-level consistency (aggregation is exact)")
    for lvl in (1, 2, 4):
        if lvl >= pyr.n_levels():
            continue
        tot = 0.0
        for h in pyr.levels[lvl].values():     # every reference (blobs shared)
            for val in pyr.get_tile(h).cells.values():
                tot += cell_measure(val, "polygon")
        drift = abs(tot - t["polygon"]) / max(1.0, t["polygon"])
        print(f"  level {lvl}: polygon mass={tot:.9f} drift={drift:.2e}")

    # 6. accuracy cross-check vs shapely (independent oracle, pre-update)
    hr("6. accuracy cross-check vs shapely (independent oracle)")
    try:
        import numpy as np
        from shapely.geometry import Polygon as SP, box
    except ImportError:
        print("  (numpy/shapely unavailable -- skipping)")
        np = None
    if np is not None:
        ref = np.zeros((g.h, g.w))
        shapes = []
        for f in feats:
            if f.kind == "polygon":
                for poly in f.polys:
                    shapes.append(SP(poly[0], poly[1:]))
        for j in range(g.h):
            y1 = g.ytop - j * g.cell
            for i in range(g.w):
                x0 = g.x0 + i * g.cell
                b = box(x0, y1 - g.cell, x0 + g.cell, y1)
                a = 0.0
                for sh in shapes:
                    if sh.intersects(b):
                        a += sh.intersection(b).area
                ref[j, i] = a
        got = np.array(dense_matrix(pyr, level=0, kind="polygon")) * g.cell ** 2
        diff = np.abs(got - ref)
        print(f"  max cell error : {diff.max():.3e} (cell area {g.cell**2:.4f})")
        print(f"  mean cell error: {diff.mean():.3e}")
        print(f"  mass error     : {abs(got.sum() - ref.sum()):.3e} of {ref.sum():.3f}")
        assert diff.max() < 1e-9, "accuracy regression"
        print("  PASS: exact to floating-point noise")

    # 7. incremental update
    hr("7. incremental update == fresh bake (byte-for-byte)")
    edit_fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": 5001, "properties": {"label": "new-park"},
         "geometry": {"type": "Polygon",
                      "coordinates": [ring_of(70, 30, 7)]}}]}
    edit = features_from_geojson(edit_fc)
    t0 = time.perf_counter()
    report = pyr.update(removed_fids=(), added=edit)
    t_inc = time.perf_counter() - t0
    # fresh bake on the SAME grid (update never re-fits the grid)
    t0 = time.perf_counter()
    fresh = grist.Pyramid.bake(feats + edit, pyr.grid, tile_size=16)
    t_full = time.perf_counter() - t0
    print(f"  update: {t_inc*1000:.1f} ms, tiles changed per level "
          f"{ {k: v for k, v in report['changed'].items() if v} }")
    print(f"  fresh : {t_full*1000:.1f} ms  (speedup {t_full/max(t_inc,1e-9):.1f}x)")
    same = codec.serialize(pyr) == codec.serialize(fresh)
    print(f"  byte-identical to fresh bake: {same}")
    assert same

    # 8. save + verify
    hr("8. save & verify")
    path = os.path.join(out, "city.grist")
    h = codec.save(pyr, path)
    print(f"  wrote {path} ({os.path.getsize(path)//1024} KiB, digest {h[:16]}...)")
    back = codec.load(path)
    print(f"  round-trip identical: {codec.serialize(back) == codec.serialize(pyr)}")

    hr("demo complete")


def ring_of(cx, cy, r):
    n = 24
    pts = [[round(cx + r * math.cos(2 * math.pi * k / n), 6),
            round(cy + r * math.sin(2 * math.pi * k / n), 6)] for k in range(n)]
    pts.append(pts[0])
    return pts


if __name__ == "__main__":
    main()
