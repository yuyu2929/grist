"""CLI end-to-end: bake / query / export / update / diff / stats / verify / license."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grist.cli import main

FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "id": 0, "properties": {"label": "square"},
         "geometry": {"type": "Polygon", "coordinates": [
             [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]}},
        {"type": "Feature", "id": 1, "properties": {"label": "ring"},
         "geometry": {"type": "Polygon", "coordinates": [
             [[2, 2], [8, 2], [8, 8], [2, 8], [2, 2]],
             [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]]}},
        {"type": "Feature", "id": 2, "properties": {"label": "diag"},
         "geometry": {"type": "LineString", "coordinates": [[0, 0], [10, 10]]}},
        {"type": "Feature", "id": 3, "properties": {"label": "pt"},
         "geometry": {"type": "Point", "coordinates": [3.5, 7.25]}},
    ],
}


def setup_tmp():
    d = tempfile.mkdtemp()
    inp = os.path.join(d, "in.geojson")
    with open(inp, "w") as fh:
        json.dump(FIXTURE, fh)
    return d, inp


def test_cli_bake_verify_stats():
    d, inp = setup_tmp()
    out = os.path.join(d, "out.grist")
    assert main(["bake", inp, "-o", out, "--cell-count", "32"]) == 0
    assert os.path.exists(out)
    assert main(["verify", out]) == 0
    assert main(["stats", out]) == 0


def test_cli_query():
    d, inp = setup_tmp()
    out = os.path.join(d, "out.grist")
    main(["bake", inp, "-o", out, "--cell-count", "32"])
    assert main(["query", out, "5.0", "5.0"]) == 0
    assert main(["query", out, "999.0", "999.0"]) == 0


def test_cli_export_formats():
    d, inp = setup_tmp()
    out = os.path.join(d, "out.grist")
    main(["bake", inp, "-o", out, "--cell-count", "32"])
    for fmt, ext in (("dense", "json"), ("pgm", "pgm"), ("csv", "csv")):
        p = os.path.join(d, f"out.{ext}")
        assert main(["export", out, p, "--format", fmt]) == 0
        assert os.path.exists(p)
    with open(os.path.join(d, "out.pgm"), "rb") as fh:
        assert fh.read(3) == b"P5\n"


def test_cli_update_and_diff():
    d, inp = setup_tmp()
    out = os.path.join(d, "out.grist")
    main(["bake", inp, "-o", out, "--cell-count", "32"])
    edits = os.path.join(d, "edits.geojson")
    with open(edits, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": 9,
             "geometry": {"type": "Polygon", "coordinates": [
                 [[12, 2], [18, 2], [18, 8], [12, 8], [12, 2]]]}}]}, fh)
    out2 = os.path.join(d, "out2.grist")
    assert main(["update", out, "-o", out2, "--edits", edits]) == 0
    assert main(["diff", out, out2]) == 0


def test_cli_verify_detects_corruption():
    d, inp = setup_tmp()
    out = os.path.join(d, "out.grist")
    main(["bake", inp, "-o", out, "--cell-count", "32"])
    with open(out, "r+b") as fh:
        fh.seek(40)
        b = fh.read(1)
        fh.seek(40)
        fh.write(bytes([b[0] ^ 0xFF]))
    assert main(["verify", out]) == 1


def test_cli_license_roundtrip():
    doc = main.__globals__["main"]  # keep reference for clarity
    from grist.enterprise import ed25519_public_key
    sk = bytes.fromhex("22" * 32)
    pk = ed25519_public_key(sk)
    # issue with the demo key, verify with the same key
    assert main(["license", "--issue", "acme", "--days", "30",
                 "--private-key", sk.hex()]) == 0
    d = tempfile.mkdtemp()
    path = os.path.join(d, "lic.json")
    # capture the issued doc by re-issuing programmatically
    from grist.enterprise import issue_license
    doc_text, pk2 = issue_license(sk, "acme", "pro", 30)
    with open(path, "w") as fh:
        fh.write(doc_text)
    assert main(["license", "--file", path, "--public-key", pk2.hex()]) == 0
    # forged key must fail
    assert main(["license", "--file", path,
                 "--public-key", ed25519_public_key(bytes.fromhex("33" * 32)).hex()]) == 1


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
