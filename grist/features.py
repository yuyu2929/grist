"""GeoJSON input parsing into Grist's internal feature model.

Supported geometries: Point, MultiPoint, LineString, MultiLineString,
Polygon, MultiPolygon, GeometryCollection (flattened). Each feature keeps
its own fid; Multi* geometries are folded into the feature's point list /
polyline list / ring-list list so that all parts share one mixture entry.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterator, Optional


class Feature:
    __slots__ = ("fid", "kind", "points", "lines", "polys", "label")

    def __init__(self, fid: int, kind: str, points=(), lines=(), polys=(), label: str = ""):
        self.fid = fid
        self.kind = kind          # "point" | "line" | "polygon"
        self.points = [tuple(p) for p in points]
        self.lines = [list(l) for l in lines]
        self.polys = [list(rl) for rl in polys]
        self.label = label
        self._check_structure()

    def _check_structure(self) -> None:
        """Fail fast on malformed nesting (points must be pairs, lines
        sequences of pairs, polys lists of ring-lists of pairs).

        The most common construction mistake is passing a bare ring as a
        polygon part; without this check it corrupts silently and surfaces
        much later as a confusing unpack error.
        """
        if self.kind not in ("point", "line", "polygon"):
            raise ValueError(f"bad feature kind: {self.kind!r}")
        for p in self.points:
            if len(p) != 2:
                raise ValueError(f"point is not a pair: {p!r}")
        for line in self.lines:
            for p in line:
                if len(p) != 2:
                    raise ValueError(f"line vertex is not a pair: {p!r}")
        for part in self.polys:
            if not isinstance(part, (list, tuple)):
                raise ValueError(f"polygon part is not a ring list: {part!r:.80}")
            for ring in part:
                if not isinstance(ring, (list, tuple)) or not ring:
                    raise ValueError(
                        f"polygon part is not a list of rings: {part!r:.80}")
                for p in ring:
                    if not isinstance(p, (list, tuple)) or len(p) != 2:
                        raise ValueError(f"ring vertex is not a pair: {p!r}")

    def bbox(self) -> Optional[tuple[float, float, float, float]]:
        xs: list[float] = []
        ys: list[float] = []
        for x, y in self.points:
            xs.append(x); ys.append(y)
        for line in self.lines:
            for x, y in line:
                xs.append(x); ys.append(y)
        for rings in self.polys:
            for ring in rings:
                for x, y in ring:
                    xs.append(x); ys.append(y)
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def measure_total(self) -> float:
        """Exact size under Grist semantics (count / length / even-odd area).

        Polygon area uses the even-odd convention documented in THEORY.md:
        rings oriented by nesting depth, area = |sum of signed areas| per
        polygon part (holes and islands cancel/add accordingly).
        """
        if self.kind == "point":
            return float(len(self.points))
        if self.kind == "line":
            return sum(math.hypot(b[0] - a[0], b[1] - a[1])
                       for line in self.lines for a, b in zip(line, line[1:]))
        from .coverage import _normalize_rings
        from .geom import ring_signed_area
        total = 0.0
        for poly in self.polys:
            # even-odd semantics: orient rings by nesting depth, then the
            # xor area is the signed shoelace sum (same normalization the
            # coverage kernel applies, so measure_total and the kernel
            # always agree)
            rings = _normalize_rings(poly)
            signed = sum(ring_signed_area(ring) for ring in rings)
            total += abs(signed)
        return total


def index_features(features) -> dict[int, "Feature"]:
    """Collect features into {fid: feature}, refusing duplicate ids.

    The pyramid addresses every per-cell measure by fid, so two features with
    the same id collapse into one and half the data silently disappears
    (measured: two point features sharing id 1 produced a point total of 1.0
    instead of 2.0).  Fail loudly at the input boundary instead.
    """
    out: dict[int, Feature] = {}
    dups: list[int] = []
    for f in features:
        if f.fid in out:
            dups.append(f.fid)
        out[f.fid] = f
    if dups:
        raise ValueError(
            "duplicate feature id(s): "
            + ", ".join(str(d) for d in sorted(set(dups)))
            + " - feature ids must be unique (GeoJSON `id` or properties)"
        )
    return out


def _finite(v: Any) -> float:
    f = float(v)
    if not math.isfinite(f):
        raise ValueError(f"non-finite coordinate: {v!r}")
    return f


def _pt(coords: Any) -> tuple[float, float]:
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        raise ValueError(f"bad position: {coords!r}")
    return (_finite(coords[0]), _finite(coords[1]))


def _ring(coords: Any) -> list[tuple[float, float]]:
    if not isinstance(coords, (list, tuple)) or len(coords) < 3:
        raise ValueError(f"bad linear ring: {coords!r}")
    ring = [_pt(c) for c in coords]
    if ring[0] == ring[-1]:
        ring = ring[:-1]
    return ring


def feature_from_geometry(fid: int, geom: dict, props: Optional[dict] = None) -> Optional[Feature]:
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "GeometryCollection":
        feats = [feature_from_geometry(fid, g, props) for g in geom.get("geometries", [])]
        feats = [f for f in feats if f is not None]
        if not feats:
            return None
        if len({f.kind for f in feats}) != 1:
            raise ValueError("GeometryCollection mixes kinds; split by kind first")
        f0 = feats[0]
        for f in feats[1:]:
            f0.points += f.points
            f0.lines += f.lines
            f0.polys += f.polys
        return f0
    if gtype == "Point":
        return Feature(fid, "point", points=[_pt(coords)])
    if gtype == "MultiPoint":
        return Feature(fid, "point", points=[_pt(c) for c in coords])
    if gtype == "LineString":
        return Feature(fid, "line", lines=[[_pt(c) for c in coords]])
    if gtype == "MultiLineString":
        return Feature(fid, "line", lines=[[_pt(c) for c in l] for l in coords])
    if gtype == "Polygon":
        return Feature(fid, "polygon", polys=[[_ring(r) for r in coords]])
    if gtype == "MultiPolygon":
        return Feature(fid, "polygon", polys=[[_ring(r) for r in poly] for poly in coords])
    raise ValueError(f"unsupported geometry type: {gtype!r}")


def load_features(path: str) -> list[Feature]:
    """Load a GeoJSON file (FeatureCollection, Feature, or bare geometry)."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return features_from_geojson(data)


def features_from_geojson(data: dict) -> list[Feature]:
    feats: list[Feature] = []
    if data.get("type") == "FeatureCollection":
        raw = data.get("features", [])
    elif data.get("type") == "Feature":
        raw = [data]
    else:
        raw = [{"type": "Feature", "geometry": data, "properties": {}}]
    for idx, item in enumerate(raw):
        geom = item.get("geometry")
        if geom is None:
            continue
        props = item.get("properties") or {}
        fid = item.get("id", idx)
        if not isinstance(fid, int):
            fid = idx
        label = ""
        for key in ("label", "name", "class", "category"):
            v = props.get(key)
            if isinstance(v, str):
                label = v
                break
            if v is not None:
                label = str(v)
                break
        f = feature_from_geometry(fid, geom, props)
        if f is not None:
            f.label = label
            feats.append(f)
    if not feats:
        raise ValueError("no usable features in input")
    index_features(feats)      # duplicate ids would silently drop features
    return feats
