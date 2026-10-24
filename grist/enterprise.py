"""Enterprise entitlement layer (open-core).

The math core is free and unrestricted. This module gates the *enterprise*
conveniences that are implemented today:

    bake.parallel        multi-process level-0 baking (large datasets)
    container.encrypt    AES-256-GCM sealed .grist container (at rest)
    audit.log            append-only JSONL audit trail of bake/update/seal ops

`store.remote` (S3-compatible tile store) is announced but NOT implemented; it
is listed in PLANNED_FEATURES and is never granted by a license, so nobody
pays for something that does not run.

Licensing is offline: a license file is a JSON document
    {"payload": <canonical JSON string>, "sig": <hex ed25519 signature>}
where the signature covers the payload bytes. The payload contains
    {"sub": ..., "tier": "pro"|"enterprise", "exp": <unix ts>, "feats": [...]}
`verify_license` checks the signature against a vendor public key and the
expiry; nothing phones home.

Ed25519 below is a compact pure-Python RFC 8032 implementation so the
entire package stays dependency-free. Signing is only used by the
license-issuing tool (`issue_license`), never on the consumer path.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# --------------------------------------------------------------------------
# Ed25519 (RFC 8032) -- pure python
# --------------------------------------------------------------------------

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = x * _I % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = 4 * pow(5, _P - 2, _P) % _P     # RFC 8032 base point y = 4/5 mod p
_BX = _xrecover(_BY)
_B = (_BX, _BY, 1, _BX * _BY % _P)          # extended homogeneous coords


def _add(p, q):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = t1 * 2 * _D * t2 % _P
    dd = z1 * 2 * z2 % _P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _double(p):
    return _add(p, p)


def _scalarmult(p, e: int):
    if e == 0:
        return (0, 1, 1, 0)
    q = _scalarmult(p, e // 2)
    q = _double(q)
    if e & 1:
        q = _add(q, p)
    return q


def _equal(p, q) -> bool:
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    if (x1 * z2 - x2 * z1) % _P != 0:
        return False
    if (y1 * z2 - y2 * z1) % _P != 0:
        return False
    return True


def _point_compress(p) -> bytes:
    x, y, z, _ = p
    zinv = pow(z, _P - 2, _P)
    x = x * zinv % _P
    y = y * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _point_decompress(data: bytes):
    if len(data) != 32:
        raise ValueError("bad point length")
    y = int.from_bytes(data, "little")
    sign = (y >> 255) & 1
    y &= (1 << 255) - 1
    x = _xrecover(y)
    if x & 1 != sign:
        x = _P - x
    return (x, y, 1, x * y % _P)


def _sha512_mod_l(b: bytes) -> int:
    import hashlib
    return int.from_bytes(hashlib.sha512(b).digest(), "little") % _L


def ed25519_sign(sk: bytes, msg: bytes) -> bytes:
    if len(sk) != 32:
        raise ValueError("secret key must be 32 bytes")
    h = __import__("hashlib").sha512(sk).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    az = _scalarmult(_B, a)
    pk = _point_compress(az)
    r = _sha512_mod_l(h[32:] + msg)
    rr = _point_compress(_scalarmult(_B, r))
    hh = _sha512_mod_l(rr + pk + msg)
    s = (r + hh * a) % _L
    return rr + int.to_bytes(s, 32, "little")


def ed25519_verify(pk: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pk) != 32 or len(sig) != 64:
        return False
    try:
        r = _point_decompress(sig[:32])
        s = int.from_bytes(sig[32:], "little")
        if s >= _L:
            return False
        a = _point_decompress(pk)
        hh = _sha512_mod_l(sig[:32] + pk + msg)
        ra = _scalarmult(_B, s)
        ha = _scalarmult(a, hh)
        return _equal(ra, _add(ha, r))
    except (ValueError, ArithmeticError):
        return False


# --------------------------------------------------------------------------
# license documents
# --------------------------------------------------------------------------

TIERS = ("pro", "enterprise")

#: features unlocked per tier -- ONLY features that are actually implemented.
#: Advertising an unimplemented feature is worse than not selling it: a buyer
#: pays for something that does not exist.
TIER_FEATURES = {
    "pro": ("bake.parallel", "audit.log"),
    "enterprise": ("bake.parallel", "audit.log", "container.encrypt"),
}

#: announced, not implemented yet -- never granted by a license
PLANNED_FEATURES = ("store.remote",)

ENC_MAGIC = b"GRISENC1"
_KDF_N, _KDF_R, _KDF_P = 2 ** 14, 8, 1

AUDIT_OPS = ("bake", "update", "encrypt", "decrypt")


class LicenseError(PermissionError):
    pass


@dataclass
class License:
    sub: str
    tier: str
    exp: int
    feats: tuple[str, ...] = ()

    def is_valid(self, now: Optional[int] = None) -> bool:
        if now is None:
            now = int(time.time())
        return self.exp > now

    def has(self, feat: str, now: Optional[int] = None) -> bool:
        if not self.is_valid(now=now):
            return False
        return feat in self.feats

    def days_left(self, now: Optional[int] = None) -> int:
        if now is None:
            now = int(time.time())
        return max(0, (self.exp - now) // 86400)

    def to_payload_json(self) -> str:
        return json.dumps({"sub": self.sub, "tier": self.tier, "exp": self.exp,
                           "feats": list(self.feats)}, sort_keys=True,
                          separators=(",", ":"))


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def verify_license(text: str, public_key: bytes, now: Optional[int] = None) -> License:
    """Verify a license document; raises LicenseError on any failure."""
    try:
        doc = json.loads(text)
        payload = json.loads(doc["payload"])
        sig = bytes.fromhex(doc["sig"])
    except (ValueError, KeyError, TypeError) as e:
        raise LicenseError(f"malformed license: {e}") from e
    if not ed25519_verify(public_key, doc["payload"].encode("utf-8"), sig):
        raise LicenseError("license signature invalid")
    if payload.get("tier") not in TIERS:
        raise LicenseError("unknown license tier")
    lic = License(sub=str(payload.get("sub", "")), tier=payload["tier"],
                  exp=int(payload.get("exp", 0)),
                  feats=tuple(payload.get("feats", ())))
    if not lic.is_valid(now=now):
        raise LicenseError("license expired")
    return lic


def issue_license(private_key: bytes, sub: str, tier: str, days: int,
                  now: Optional[int] = None) -> tuple[str, bytes]:
    """Vendor-side: mint a license document and its public key.

    Returns (license_text, public_key). Keep private_key on the issuer;
    embed public_key in the distributed build.
    """
    if now is None:
        now = int(time.time())
    lic = License(sub=sub, tier=tier, exp=now + days * 86400,
                  feats=TIER_FEATURES[tier])
    payload = lic.to_payload_json()
    sig = ed25519_sign(private_key, payload.encode("utf-8"))
    doc = json.dumps({"payload": payload, "sig": sig.hex()})
    return doc, ed25519_public_key(private_key)


def ed25519_public_key(sk: bytes) -> bytes:
    import hashlib
    h = hashlib.sha512(sk).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _point_compress(_scalarmult(_B, a))


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------

def _band_acc(band: tuple, grid_params: tuple) -> dict:
    """Worker function: accumulate one feature band's level-0 measures.

    Module-level (not a closure) so ProcessPoolExecutor can pickle it.
    grid_params carries the plain grid numbers; the worker rebuilds the
    Grid locally rather than pickling it.
    """
    from .coverage import line_lengths, point_counts, polygon_areas
    from .grid import Grid
    grid = Grid(*grid_params)
    acc: dict[tuple[int, int], dict[str, dict[int, float]]] = {}
    for f in band:
        if f.kind == "point":
            src = ((c, float(n)) for c, n in point_counts(f.points, grid).items())
            kind = "point"
        elif f.kind == "line":
            src = line_lengths(f.lines, grid).items()
            kind = "line"
        else:
            src = polygon_areas(f.polys, grid).items()
            kind = "polygon"
        for cell, v in src:
            d = acc.setdefault(cell, {"point": {}, "line": {}, "polygon": {}})
            d[kind][f.fid] = d[kind].get(f.fid, 0.0) + v
    return acc


@dataclass
class Gate:
    """Feature gate loaded from a license file (or the built-in free tier).

    The gate is an entitlement check, not DRM: `License` objects are plain
    values and a determined caller can construct one. What a commercial
    license buys is the legal right plus support, not an unpickable lock -
    see docs/BUSINESS.md.
    """

    public_key: bytes
    license: Optional[License] = None
    audit_path: Optional[str] = None
    _audit: list = field(default_factory=list)

    @classmethod
    def free(cls, public_key: bytes) -> "Gate":
        return cls(public_key=public_key, license=None)

    @classmethod
    def from_file(cls, path: str, public_key: bytes,
                  audit_path: Optional[str] = None) -> "Gate":
        """Load and *verify* a license document (signature + expiry)."""
        with open(path, "r", encoding="utf-8") as fh:
            lic = verify_license(fh.read(), public_key)
        return cls(public_key=public_key, license=lic, audit_path=audit_path)

    def check(self, feat: str, now: Optional[int] = None) -> None:
        if self.license is not None and self.license.has(feat, now=now):
            return
        raise LicenseError(
            f"feature '{feat}' requires a valid pro/enterprise license "
            f"(see docs/BUSINESS.md)")

    def enabled(self, feat: str, now: Optional[int] = None) -> bool:
        return self.license is not None and self.license.has(feat, now=now)

    def require(self, feat: str) -> Callable:
        def deco(fn):
            def wrapper(*a, **kw):
                self.check(feat)
                return fn(*a, **kw)
            wrapper.__name__ = fn.__name__
            wrapper.__doc__ = fn.__doc__
            return wrapper
        return deco

    # -- gated enterprise features ------------------------------------------
    def _aead(self):
        """Import the optional crypto backend lazily.

        AES-GCM comes from `cryptography` (the `enterprise` extra). The core
        library stays dependency-free, and hand-rolling crypto to avoid one
        optional import would be a much worse idea than the import.
        """
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        except ImportError as exc:  # pragma: no cover - depends on install
            raise LicenseError(
                "container.encrypt needs the optional crypto backend: "
                "pip install 'grist-raster[enterprise]'") from exc
        return AESGCM, Scrypt

    def encrypt_container(self, data: bytes, passphrase: str) -> bytes:
        """Seal a serialized .grist container with a passphrase.

        Layout: MAGIC(8) | salt(16) | nonce(12) | AES-256-GCM ciphertext.
        The key comes from scrypt(passphrase, salt); the container magic is
        authenticated as associated data, so a truncated or swapped file fails
        closed instead of decrypting to garbage.
        """
        self.check("container.encrypt")
        import os
        AESGCM, Scrypt = self._aead()
        salt = os.urandom(16)
        nonce = os.urandom(12)
        key = Scrypt(salt=salt, length=32, n=_KDF_N, r=_KDF_R, p=_KDF_P).derive(
            passphrase.encode("utf-8"))
        ct = AESGCM(key).encrypt(nonce, data, ENC_MAGIC)
        return ENC_MAGIC + salt + nonce + ct

    def decrypt_container(self, blob: bytes, passphrase: str) -> bytes:
        self.check("container.encrypt")
        AESGCM, Scrypt = self._aead()
        if not blob.startswith(ENC_MAGIC) or len(blob) < 8 + 16 + 12:
            raise LicenseError("not an encrypted .grist container")
        salt, nonce, ct = blob[8:24], blob[24:36], blob[36:]
        key = Scrypt(salt=salt, length=32, n=_KDF_N, r=_KDF_R, p=_KDF_P).derive(
            passphrase.encode("utf-8"))
        try:
            return AESGCM(key).decrypt(nonce, ct, ENC_MAGIC)
        except Exception as exc:
            raise LicenseError("wrong passphrase or corrupted container") from exc

    def save_encrypted(self, pyr, path: str, passphrase: str) -> bytes:
        self.check("container.encrypt")
        from . import codec
        blob = self.encrypt_container(codec.serialize(pyr), passphrase)
        with open(path, "wb") as fh:
            fh.write(blob)
        self.log("encrypt", f"{path} {len(blob)} bytes")
        return blob

    def load_encrypted(self, path: str, passphrase: str):
        self.check("container.encrypt")
        from . import codec
        with open(path, "rb") as fh:
            blob = fh.read()
        pyr = codec.deserialize(self.decrypt_container(blob, passphrase))
        self.log("decrypt", path)
        return pyr

    def bake_parallel(self, features, grid, tile_size: int = 16, workers: int = 0,
                      max_levels=None):
        """Multi-process level-0 bake (enterprise).

        Splits the feature set into bands, bakes each band in a worker
        process, then merges the per-cell mixtures. The merge is exact (cell
        mixtures are additive per feature), so the result equals a
        single-process bake of the same features byte-for-byte.

        On Windows/macOS the worker processes are spawned, so call this under
        an ``if __name__ == "__main__":`` guard (standard multiprocessing
        requirement, not specific to Grist).
        """
        self.check("bake.parallel")
        from concurrent.futures import ProcessPoolExecutor
        import os
        from .features import index_features
        from .pyramid import Pyramid
        if workers <= 0:
            workers = min(os.cpu_count() or 1, 8)
        feats = list(features)
        index_features(feats)      # duplicate ids would silently drop features
        if len(feats) <= 1 or workers == 1:
            return Pyramid.bake(feats, grid, tile_size=tile_size, max_levels=max_levels)
        bands: list[list] = [[] for _ in range(workers)]
        for idx, f in enumerate(feats):
            bands[idx % workers].append(f)
        bands = [b for b in bands if b]
        if len(bands) == 1:
            return Pyramid.bake(feats, grid, tile_size=tile_size, max_levels=max_levels)
        grid_params = (grid.x0, grid.ytop, grid.cell, grid.w, grid.h)
        with ProcessPoolExecutor(max_workers=len(bands)) as ex:
            partials = list(ex.map(_band_acc, [tuple(b) for b in bands],
                                   [grid_params] * len(bands)))
        acc: dict[tuple[int, int], dict[str, dict[int, float]]] = {}
        for part in partials:
            for cell, d in part.items():
                dst = acc.setdefault(cell, {"point": {}, "line": {}, "polygon": {}})
                for kind in d:
                    for fid, v in d[kind].items():
                        dst[kind][fid] = dst[kind].get(fid, 0.0) + v
        return Pyramid.from_acc(acc, grid, tile_size=tile_size, max_levels=max_levels,
                                features=index_features(feats))

    def log(self, op: str, detail: str) -> dict:
        """Append an audit entry (in memory, and to `audit_path` as JSONL)."""
        self.check("audit.log")
        entry = {"t": int(time.time()),
                 "sub": self.license.sub if self.license else "",
                 "op": op, "detail": detail}
        self._audit.append(entry)
        if self.audit_path:
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry
