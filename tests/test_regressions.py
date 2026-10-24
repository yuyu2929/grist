"""Regression tests for the defects found in the 0.1.1 audit.

Each test fails against 0.1.0 and passes after the fix.
"""
import math
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from grist import Grid, codec, query
from grist.bake import bake_features
from grist.features import Feature, features_from_geojson


def _poly(fid, ring):
    return Feature(fid, "polygon", polys=[[ring]])


def _run_cli(*args):
    return subprocess.run([sys.executable, "-m", "grist.cli", *args], cwd=ROOT,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


# --------------------------------------------------------------- incremental

def test_update_keeps_point_on_tile_boundary():
    """A point on a half-open tile edge belongs to the next tile.

    level-0 tiles are 4 cells wide; with cell=0.25 the boundary is x=1.0.
    The dirty-feature filter used a strict overlap test, so the point was
    never recomputed and an incremental update lost it (updated 1.0 vs a
    fresh bake's 2.0).
    """
    g = Grid(0.0, 2.0, 0.25, 8, 8)
    seed = [Feature(0, "point", points=[(0.1, 0.1)])]
    edge = Feature(7, "point", points=[(1.0, 0.5)])
    p = bake_features(seed, g, tile_size=4)
    p.update(added=[edge])
    fresh = bake_features(seed + [edge], g, tile_size=4)
    assert codec.serialize(p) == codec.serialize(fresh), "boundary point lost"
    assert math.isclose(p.totals()["point"], 2.0)


def test_update_keeps_line_on_tile_boundary():
    g = Grid(0.0, 2.0, 0.25, 8, 8)
    seed = [Feature(0, "point", points=[(0.1, 0.1)])]
    edge = Feature(9, "line", lines=[[(1.0, 0.2), (1.0, 1.8)]])
    p = bake_features(seed, g, tile_size=4)
    p.update(added=[edge])
    fresh = bake_features(seed + [edge], g, tile_size=4)
    assert codec.serialize(p) == codec.serialize(fresh), "boundary line lost"
    assert math.isclose(p.totals()["line"], 1.6)


def test_update_matches_fresh_for_ordinary_edits():
    g = Grid(0.0, 10.0, 0.25, 40, 40)
    base = [_poly(i, [(i, 0), (i + 3, 0), (i + 3, 3), (i, 3)]) for i in range(6)]
    new = _poly(99, [(1.2, 1.2), (4.4, 1.2), (4.4, 4.4), (1.2, 4.4)])
    p = bake_features(base, g, tile_size=4)
    p.update(added=[new])
    fresh = bake_features(base + [new], g, tile_size=4)
    assert codec.serialize(p) == codec.serialize(fresh)

    p2 = bake_features(base, g, tile_size=4)
    p2.update(removed_fids=[2])
    fresh2 = bake_features([f for f in base if f.fid != 2], g, tile_size=4)
    assert codec.serialize(p2) == codec.serialize(fresh2)


# --------------------------------------------------------------- read policies

def test_cell_owner_policies_are_distinct():
    cell = ((), (), ((1, 0.6), (2, 0.4)))
    assert query.cell_owner(cell, "polygon", policy="top") == (1, 0.6)
    assert query.cell_owner(cell, "polygon", policy="max") == (-1, 0.6)
    assert query.cell_owner(cell, "polygon", policy="sum") == (-1, 1.0)
    try:
        query.cell_owner(cell, "polygon", policy="bogus")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown policy accepted")


def test_cell_owner_top_breaks_ties_by_smallest_fid():
    cell = ((), (), ((7, 0.5), (3, 0.5)))
    assert query.cell_owner(cell, "polygon", policy="top") == (3, 0.5)


# --------------------------------------------------------------- feature ids

def test_duplicate_feature_ids_are_rejected():
    data = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": 1,
         "geometry": {"type": "Point", "coordinates": [0.5, 0.5]}, "properties": {}},
        {"type": "Feature", "id": 1,
         "geometry": {"type": "Point", "coordinates": [1.5, 1.5]}, "properties": {}},
    ]}
    try:
        features_from_geojson(data)
    except ValueError as exc:
        assert "duplicate feature id" in str(exc), exc
    else:
        raise AssertionError("duplicate ids were silently accepted")


def test_duplicate_fids_are_rejected_at_the_bake_layer_too():
    g = Grid(0.0, 3.0, 1.0, 3, 3)
    feats = [Feature(5, "point", points=[(0.5, 0.5)]),
             Feature(5, "point", points=[(1.5, 1.5)])]
    try:
        bake_features(feats, g, tile_size=4)
    except ValueError as exc:
        assert "duplicate feature id" in str(exc), exc
    else:
        raise AssertionError("duplicate fids silently collapsed one feature")


# --------------------------------------------------------------- cli

def test_cli_update_requires_output():
    import tempfile
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "c.grist")
    r = _run_cli("bake", os.path.join(ROOT, "demo", "city.geojson"), "-o", src,
                 "--cell-count", "32")
    assert r.returncode == 0, r.stderr
    r2 = _run_cli("update", src)
    assert r2.returncode == 2, r2.returncode
    assert "Traceback" not in r2.stderr
    assert "--output" in r2.stderr


def test_cli_reports_missing_file_without_traceback():
    r = _run_cli("stats", os.path.join(ROOT, "no-such-file.grist"))
    assert r.returncode != 0
    assert "Traceback" not in r.stderr, r.stderr[-300:]


def test_cli_cannot_self_issue_a_license():
    r = _run_cli("license", "--issue", "acme")
    assert r.returncode == 2, "the CLI minted a license with a built-in key"
    assert "private-key" in r.stderr


# --------------------------------------------------------------- numerics

def test_degenerate_rings_match_shapely():
    try:
        from shapely.geometry import Polygon as SPoly
    except ImportError:  # shapely is an optional test dependency
        print("   (shapely not installed - skipped)")
        return
    from grist.coverage import polygon_areas
    rings = {
        "dup_vertices": [(0, 0), (0, 0), (10, 0), (10, 10), (10, 10), (0, 10)],
        "collinear_tail": [(0, 0), (10, 0), (10, 10), (5, 10), (0, 10)],
        "zero_area": [(0, 0), (5, 0), (10, 0)],
        "tiny": [(0, 0), (1e-6, 0), (1e-6, 1e-6), (0, 1e-6)],
    }
    g = Grid(0.0, 10.0, 10.0 / 32, 32, 32)
    for name, ring in rings.items():
        total = math.fsum(polygon_areas([[ring]], g).values())
        oracle = SPoly(ring).area
        assert abs(total - oracle) <= 1e-9 * max(1.0, oracle), \
            f"{name}: grist={total} shapely={oracle}"


def test_conservation_on_a_rotated_square():
    """Rotated edges mean every strip is uncertified: pure clipping path."""
    from grist.coverage import polygon_areas
    import math as m
    # circumradius 3 -> side 3*sqrt(2) -> area 18; centred so the whole
    # square is inside the grid (a clipped square would fail for the wrong
    # reason)
    ring = [(5 + 3 * m.cos(m.pi / 4 + k * m.pi / 2), 5 + 3 * m.sin(m.pi / 4 + k * m.pi / 2))
            for k in range(4)]
    g = Grid(0.0, 10.0, 10.0 / 64, 64, 64)
    total = math.fsum(polygon_areas([[ring]], g).values())
    assert abs(total - 18.0) <= 1e-9, total


# --------------------------------------------------------------- enterprise

def _licensed_gate(tier="enterprise"):
    import secrets as _secrets
    import tempfile as _tempfile
    from grist.enterprise import Gate, issue_license, verify_license
    sk = _secrets.token_bytes(32)
    doc, pk = issue_license(sk, "acme", tier, 30)
    tmp = _tempfile.mkdtemp()
    return Gate(public_key=pk, license=verify_license(doc, pk),
                audit_path=os.path.join(tmp, "audit.jsonl")), tmp


def test_encrypted_container_roundtrips():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("   (cryptography not installed - skipped)")
        return
    from grist.enterprise import LicenseError
    from grist.bake import bake_features

    gate, tmp = _licensed_gate()
    g = Grid(0.0, 10.0, 0.5, 20, 20)
    pyr = bake_features([_poly(1, [(1, 1), (9, 1), (9, 9), (1, 9)])], g, tile_size=4)
    path = os.path.join(tmp, "sealed.grist")
    blob = gate.save_encrypted(pyr, path, "correct horse")
    assert blob.startswith(b"GRISENC1")
    back = gate.load_encrypted(path, "correct horse")
    assert codec.serialize(back) == codec.serialize(pyr)

    for bad in ("wrong passphrase", None):
        try:
            gate.load_encrypted(path, bad or "")
            raise AssertionError("wrong passphrase accepted")
        except LicenseError:
            pass
    # truncation must fail closed, not decrypt to garbage
    raw = open(path, "rb").read()
    with open(path, "wb") as fh:
        fh.write(raw[:-10])
    try:
        gate.load_encrypted(path, "correct horse")
        raise AssertionError("truncated container accepted")
    except LicenseError:
        pass


def test_encryption_requires_a_license():
    from grist.enterprise import Gate, LicenseError
    gate = Gate.free(bytes(32))
    try:
        gate.encrypt_container(b"payload", "pw")
    except LicenseError:
        pass
    else:
        raise AssertionError("free gate encrypted a container")


def test_audit_log_is_append_only_jsonl():
    gate, tmp = _licensed_gate()
    gate.log("bake", "one")
    gate.log("update", "two")
    lines = open(gate.audit_path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 2, lines
    import json as _json
    assert _json.loads(lines[0])["op"] == "bake"
    assert _json.loads(lines[1])["op"] == "update"


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
