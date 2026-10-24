"""Container format: round-trip, integrity, corruption detection."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grist import Grid, Pyramid, codec
from grist.features import Feature


def sample_pyramid():
    feats = [
        Feature(0, "polygon", polys=[[[(0, 0), (10, 0), (10, 10), (0, 10)],
                                      [(4, 4), (6, 4), (6, 6), (4, 6)]]],
                label="square-hole"),
        Feature(1, "line", lines=[[(0, 0), (10, 10)], [(2, 8), (8, 2)]], label="diag"),
        Feature(2, "point", points=[(1.5, 8.5), (9.5, 1.5)], label="pts"),
    ]
    return Pyramid.bake(feats, Grid(0.0, 10.0, 1.0, 10, 10))


def test_roundtrip_identical():
    pyr = sample_pyramid()
    data = codec.serialize(pyr)
    back = codec.deserialize(data)
    assert codec.serialize(back) == data


def test_roundtrip_preserves_totals():
    pyr = sample_pyramid()
    back = codec.deserialize(codec.serialize(pyr))
    assert pyr.totals() == back.totals()


def test_roundtrip_preserves_features():
    pyr = sample_pyramid()
    back = codec.deserialize(codec.serialize(pyr))
    assert set(back.features) == set(pyr.features)
    for fid, f in pyr.features.items():
        g = back.features[fid]
        assert f.kind == g.kind and f.label == g.label
        assert f.points == g.points
        assert f.lines == g.lines
        assert f.polys == g.polys


def test_digest_detects_corruption():
    data = codec.serialize(sample_pyramid())
    for pos in (len(data) // 3, len(data) // 2, len(data) - 40):
        bad = bytearray(data)
        bad[pos] ^= 0xFF
        try:
            codec.deserialize(bytes(bad))
            raise AssertionError(f"corruption at {pos} not detected")
        except ValueError:
            pass


def test_bad_magic():
    pyr = sample_pyramid()
    data = bytearray(codec.serialize(pyr))
    data[0:8] = b"NOTGRIST"
    try:
        codec.deserialize(bytes(data))
        raise AssertionError("bad magic not detected")
    except ValueError:
        pass


def test_truncated_file():
    data = codec.serialize(sample_pyramid())
    try:
        codec.deserialize(data[:len(data) // 2])
        raise AssertionError("truncation not detected")
    except ValueError:
        pass


def test_save_load_file():
    pyr = sample_pyramid()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.grist")
        h = codec.save(pyr, path)
        back = codec.load(path)
        assert codec.serialize(back) == codec.serialize(pyr)
        assert h == codec.content_hash(open(path, "rb").read())


def test_deterministic_bytes_across_bakes():
    feats = [Feature(0, "polygon", polys=[[[(0, 0), (5, 0), (5, 5), (0, 5)]]]),
             Feature(1, "line", lines=[[(0, 5), (5, 0)]])]
    a = Pyramid.bake(feats, Grid(0.0, 5.0, 0.5, 10, 10))
    b = Pyramid.bake(list(reversed(feats)), Grid(0.0, 5.0, 0.5, 10, 10))
    assert codec.serialize(a) == codec.serialize(b)


def test_store_pruning_on_serialize():
    """Orphaned blobs (from updates) must not leak into the file."""
    feats = [Feature(0, "polygon", polys=[[[(0, 0), (10, 0), (10, 10), (0, 10)]]])]
    pyr = Pyramid.bake(feats, Grid(0.0, 10.0, 1.0, 10, 10))
    pyr.update(removed_fids=(), added=[Feature(1, "polygon",
                                               polys=[[[(2, 2), (4, 2), (4, 4), (2, 4)]]])])
    referenced = pyr.store.referenced(pyr.levels)
    data = codec.serialize(pyr)
    # file contains exactly the referenced blobs
    back = codec.deserialize(data)
    assert set(back.store._blobs) == referenced


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
