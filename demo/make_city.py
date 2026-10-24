"""Generate a synthetic city GeoJSON for the Grist demo.

Deterministic (fixed seed): parks (polygons with holes), roads (lines),
buildings (small polygons), POIs (points). Written to demo/city.geojson.
"""
import json
import math
import random
import os

HERE = os.path.dirname(os.path.abspath(__file__))
EXTENT = 100.0


def ring(cx, cy, r, n, jitter=0.0, rng=None):
    pts = []
    for k in range(n):
        rr = r * (1.0 + (rng.uniform(-jitter, jitter) if rng and jitter else 0.0))
        a = 2 * math.pi * k / n
        pts.append([round(cx + rr * math.cos(a), 6), round(cy + rr * math.sin(a), 6)])
    pts.append(pts[0])
    return pts


def main():
    rng = random.Random(2024)
    feats = []

    # parks: big polygons with a hole (pond)
    for i in range(6):
        cx, cy = rng.uniform(15, 85), rng.uniform(15, 85)
        r = rng.uniform(6, 11)
        outer = ring(cx, cy, r, rng.randint(8, 14), 0.25, rng)
        hole = ring(cx, cy, r * 0.3, 6)
        feats.append({"type": "Feature", "id": i, "properties": {"label": f"park{i}"},
                      "geometry": {"type": "Polygon", "coordinates": [outer, hole]}})

    # roads: polylines
    for i in range(14):
        x, y = rng.uniform(0, EXTENT), rng.uniform(0, EXTENT)
        pts = [[round(x, 6), round(y, 6)]]
        for _ in range(rng.randint(2, 5)):
            x = min(EXTENT, max(0.0, x + rng.uniform(-25, 25)))
            y = min(EXTENT, max(0.0, y + rng.uniform(-25, 25)))
            pts.append([round(x, 6), round(y, 6)])
        feats.append({"type": "Feature", "id": 100 + i,
                      "properties": {"label": f"road{i}"},
                      "geometry": {"type": "LineString", "coordinates": pts}})

    # buildings: small axis-aligned-ish blocks
    for i in range(120):
        cx, cy = rng.uniform(2, 98), rng.uniform(2, 98)
        w, h = rng.uniform(1.0, 3.0), rng.uniform(1.0, 3.0)
        ang = rng.uniform(0, math.pi / 2)
        ca, sa = math.cos(ang), math.sin(ang)
        corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
        pts = [[round(cx + px * ca - py * sa, 6), round(cy + px * sa + py * ca, 6)]
               for px, py in corners]
        pts.append(pts[0])
        feats.append({"type": "Feature", "id": 200 + i,
                      "properties": {"label": f"bldg{i}"},
                      "geometry": {"type": "Polygon", "coordinates": [pts]}})

    # POIs
    for i in range(25):
        feats.append({"type": "Feature", "id": 900 + i,
                      "properties": {"label": f"poi{i}"},
                      "geometry": {"type": "Point",
                                   "coordinates": [round(rng.uniform(0, EXTENT), 6),
                                                   round(rng.uniform(0, EXTENT), 6)]}})

    out = os.path.join(HERE, "city.geojson")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)
    print(f"wrote {out}: {len(feats)} features")


if __name__ == "__main__":
    main()
