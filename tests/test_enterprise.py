"""Enterprise layer: Ed25519 correctness, license gating, parallel bake."""
import os
import math
import random
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grist import Grid, Pyramid, codec
from grist.enterprise import (Gate, License, LicenseError, ed25519_public_key,
                              ed25519_sign, ed25519_verify, issue_license,
                              verify_license)
from grist.features import Feature


def test_ed25519_roundtrip():
    sk = secrets.token_bytes(32)
    pk = ed25519_public_key(sk)
    for msg in (b"", b"grist", secrets.token_bytes(100)):
        sig = ed25519_sign(sk, msg)
        assert ed25519_verify(pk, msg, sig)
        assert not ed25519_verify(pk, msg + b"x", sig)
        assert not ed25519_verify(secrets.token_bytes(32), msg, sig)


def test_ed25519_rejects_malleable_signatures():
    sk = secrets.token_bytes(32)
    pk = ed25519_public_key(sk)
    sig = bytearray(ed25519_sign(sk, b"msg"))
    sig[63] ^= 0x01          # flip a low bit of s
    assert not ed25519_verify(pk, b"msg", bytes(sig))


def test_license_issue_verify():
    sk = secrets.token_bytes(32)
    doc, pk = issue_license(sk, "acme", "pro", 30)
    lic = verify_license(doc, pk)
    assert lic.sub == "acme" and lic.tier == "pro"
    assert lic.days_left() <= 30


def test_license_forged_key_rejected():
    sk = secrets.token_bytes(32)
    doc, pk = issue_license(sk, "acme", "pro", 30)
    try:
        verify_license(doc, secrets.token_bytes(32))
        raise AssertionError("forged key accepted")
    except LicenseError:
        pass


def test_license_tampered_payload_rejected():
    sk = secrets.token_bytes(32)
    doc, pk = issue_license(sk, "acme", "pro", 30)
    # upgrade tier without re-signing (must rebuild the document JSON, since
    # the payload is an escaped string inside it)
    import json as _json
    d = _json.loads(doc)
    d["payload"] = d["payload"].replace('"tier":"pro"', '"tier":"enterprise"')
    tampered = _json.dumps(d)
    try:
        verify_license(tampered, pk)
        raise AssertionError("tampered payload accepted")
    except LicenseError:
        pass


def test_license_expired_rejected():
    sk = secrets.token_bytes(32)
    doc, pk = issue_license(sk, "acme", "pro", -1)   # already expired
    try:
        verify_license(doc, pk)
        raise AssertionError("expired license accepted")
    except LicenseError:
        pass


def test_license_malformed():
    sk = secrets.token_bytes(32)
    _, pk = issue_license(sk, "acme", "pro", 30)
    for bad in ("{}", "not json", '{"payload": "{}", "sig": "zz"}',
                '{"payload": "{}", "sig": "' + "00" * 64 + '"}'):
        try:
            verify_license(bad, pk)
            raise AssertionError(f"malformed accepted: {bad[:30]}")
        except LicenseError:
            pass


def test_gate_free_tier_blocks():
    sk = secrets.token_bytes(32)
    _, pk = issue_license(sk, "acme", "pro", 30)
    gate = Gate.free(pk)
    for feat in ("bake.parallel", "container.encrypt", "audit.log", "store.remote"):
        assert not gate.enabled(feat)
        try:
            gate.check(feat)
            raise AssertionError(f"free gate allowed {feat}")
        except LicenseError:
            pass


def test_gate_pro_tier():
    sk = secrets.token_bytes(32)
    doc, pk = issue_license(sk, "acme", "pro", 30)
    gate = Gate(public_key=pk, license=verify_license(doc, pk))
    assert gate.enabled("bake.parallel")
    assert gate.enabled("audit.log")
    assert not gate.enabled("container.encrypt")   # enterprise-only
    assert not gate.enabled("store.remote")        # not implemented at all
    gate.log("bake", "test")                       # audit requires license
    assert gate._audit


def test_enterprise_only_features():
    sk = secrets.token_bytes(32)
    doc, pk = issue_license(sk, "acme", "enterprise", 30)
    gate = Gate(public_key=pk, license=verify_license(doc, pk))
    assert gate.enabled("container.encrypt")
    # advertised but unimplemented -> never granted
    assert not gate.enabled("store.remote")


def test_parallel_bake_equals_serial():
    sk = secrets.token_bytes(32)
    doc, pk = issue_license(sk, "acme", "enterprise", 30)
    gate = Gate(public_key=pk, license=verify_license(doc, pk))
    rng = random.Random(7)
    feats = []
    for i in range(40):
        cx, cy = rng.uniform(0, 50), rng.uniform(0, 50)
        r = rng.uniform(0.5, 3.0)
        k = rng.randint(3, 9)
        ring = [(cx + r * math.cos(2 * math.pi * t / k),
                 cy + r * math.sin(2 * math.pi * t / k)) for t in range(k)]
        feats.append(Feature(i, "polygon", polys=[[ring]]))
    grid = Grid(0.0, 50.0, 50.0 / 64, 64, 64)
    base = Pyramid.bake(feats, grid, tile_size=16)
    par = gate.bake_parallel(feats, grid, tile_size=16, workers=4)
    assert codec.serialize(base) == codec.serialize(par)
    assert base.totals() == par.totals()


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
