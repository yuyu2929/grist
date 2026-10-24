"""Container format: the .grist file.

Layout (all integers little-endian, all text UTF-8):

    magic           8 bytes  b"GRISTPY1"
    version         1 byte   FORMAT_VERSION
    grid params     x0, ytop, cell (double), w, h (uint32)
    tile_size       uint32
    feature table   count (uint32), then per feature:
                        fid uint32, kind code uint8, label length uint16,
                        label bytes, payload length uint32, payload bytes
    store           count (uint32), then per blob:
                        hash length uint8 (32), hash bytes, blob length uint32,
                        blob bytes
    manifest        level count uint32, then per level:
                        tile count uint32, then per tile:
                            ti, tj (uint32), hash (32 raw bytes)
    digest          blake2b-256 of everything above (integrity footer)

The payload is the geometry serialization (see _encode_geometry). The blob
section stores tiles verbatim -- they are already canonical (content-
addressed), so the whole file is a deterministic function of the pyramid
state: baking the same features twice yields byte-identical files.

This is the personal/open-core layer: a plain, dependency-free, self-
describing format. Enterprise packaging (licensing, encryption, chunked
streaming) is layered on top by enterprise.py.
"""
from __future__ import annotations

import struct
from hashlib import blake2b
from typing import Iterable, Optional

from .features import Feature
from .grid import Grid
from .pyramid import Pyramid, TileStore

MAGIC = b"GRISTPY1"
FORMAT_VERSION = 1

_HDR = struct.Struct("<8sB")            # magic, version
_GRID = struct.Struct("<dddII")         # x0, ytop, cell, w, h
_U32 = struct.Struct("<I")
_U16 = struct.Struct("<H")
_U8 = struct.Struct("<B")
_DBL = struct.Struct("<d")
_PT = struct.Struct("<dd")              # one vertex

_KIND_CODE = {"point": 1, "line": 2, "polygon": 3}
_CODE_KIND = {v: k for k, v in _KIND_CODE.items()}


# --------------------------------------------------------------------------
# geometry payload
# --------------------------------------------------------------------------

def _encode_geometry(f: Feature) -> bytes:
    parts = [_U8.pack(_KIND_CODE[f.kind])]
    for pts in ([f.points] if f.kind == "point" else []):
        parts.append(_U32.pack(len(pts)))
        for x, y in pts:
            parts.append(_PT.pack(x, y))
    if f.kind == "line":
        parts.append(_U32.pack(len(f.lines)))
        for line in f.lines:
            parts.append(_U32.pack(len(line)))
            for x, y in line:
                parts.append(_PT.pack(x, y))
    elif f.kind == "polygon":
        parts.append(_U32.pack(len(f.polys)))
        for poly in f.polys:
            parts.append(_U32.pack(len(poly)))
            for ring in poly:
                parts.append(_U32.pack(len(ring)))
                for x, y in ring:
                    parts.append(_PT.pack(x, y))
    return b"".join(parts)


def _decode_geometry(data: bytes) -> tuple[str, list, list, list]:
    off = 0

    def take(n):
        nonlocal off
        b = data[off:off + n]
        if len(b) != n:
            raise ValueError("truncated geometry payload")
        off += n
        return b

    def u32():
        nonlocal off
        (v,) = _U32.unpack(take(4))
        return v

    def pts(n):
        return [_PT.unpack(take(16)) for _ in range(n)]

    (kind_code,) = _U8.unpack(take(1))
    kind = _CODE_KIND.get(kind_code)
    if kind is None:
        raise ValueError("bad kind code")
    points: list = []
    lines: list = []
    polys: list = []
    if kind == "point":
        n = u32()
        points = pts(n)
    elif kind == "line":
        n = u32()
        for _ in range(n):
            m = u32()
            lines.append(pts(m))
    else:
        n = u32()
        for _ in range(n):
            m = u32()
            poly = []
            for _r in range(m):
                k = u32()
                poly.append(pts(k))
            polys.append(poly)
    if off != len(data):
        raise ValueError("trailing bytes in geometry payload")
    return kind, points, lines, polys


# --------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------

def _put(parts: list[bytes], blob: bytes) -> None:
    parts.append(blob)


def serialize(pyr: Pyramid) -> bytes:
    # The in-memory store may hold blobs orphaned by incremental updates.
    # A .grist file contains exactly the live set (manifest-referenced
    # blobs), which keeps files minimal and makes "updated pyramid ==
    # fresh bake" a byte-level identity rather than a semantic one.
    referenced = pyr.store.referenced(pyr.levels)
    parts: list[bytes] = []
    parts.append(_HDR.pack(MAGIC, FORMAT_VERSION))
    g = pyr.grid
    parts.append(_GRID.pack(g.x0, g.ytop, g.cell, g.w, g.h))
    parts.append(_U32.pack(pyr.tile_size))
    parts.append(_U32.pack(len(pyr.features)))
    for fid in sorted(pyr.features):
        f = pyr.features[fid]
        payload = _encode_geometry(f)
        label = f.label.encode("utf-8")
        parts.append(_U32.pack(f.fid))
        parts.append(_U16.pack(len(label)))
        parts.append(label)
        parts.append(_U32.pack(len(payload)))
        parts.append(payload)
    # blob store: deterministic order by hash
    parts.append(_U32.pack(len(referenced)))
    for h in sorted(referenced):
        blob = pyr.store._blobs[h]
        hb = bytes.fromhex(h)
        parts.append(_U8.pack(len(hb)))
        parts.append(hb)
        parts.append(_U32.pack(len(blob)))
        parts.append(blob)
    # manifest
    parts.append(_U32.pack(len(pyr.levels)))
    for level in pyr.levels:
        parts.append(_U32.pack(len(level)))
        for (ti, tj) in sorted(level):
            parts.append(_U32.pack(ti))
            parts.append(_U32.pack(tj))
            parts.append(bytes.fromhex(level[(ti, tj)]))
    body = b"".join(parts)
    return body + blake2b(body, digest_size=32).digest()


def deserialize(data: bytes) -> Pyramid:
    if len(data) < 8 + 1 + 32:
        raise ValueError("file too short")
    body, digest = data[:-32], data[-32:]
    if blake2b(body, digest_size=32).digest() != digest:
        raise ValueError("digest mismatch: file corrupted or truncated")
    magic, version = _HDR.unpack_from(body, 0)
    if magic != MAGIC:
        raise ValueError("not a .grist file")
    if version != FORMAT_VERSION:
        raise ValueError(f"unsupported format version {version}")
    off = _HDR.size

    def take(n):
        nonlocal off
        b = body[off:off + n]
        if len(b) != n:
            raise ValueError("truncated file")
        off += n
        return b

    def u32():
        nonlocal off
        (v,) = _U32.unpack(take(4))
        return v

    x0, ytop, cell, w, h = _GRID.unpack(take(_GRID.size))
    tile_size = u32()
    grid = Grid(x0, ytop, cell, w, h)
    store = TileStore()
    nf = u32()
    features: dict[int, Feature] = {}
    for _ in range(nf):
        fid = u32()
        (llen,) = _U16.unpack(take(2))
        label = take(llen).decode("utf-8")
        plen = u32()
        kind, points, lines, polys = _decode_geometry(take(plen))
        features[fid] = Feature(fid, kind, points=points, lines=lines, polys=polys, label=label)
    nb = u32()
    for _ in range(nb):
        (hlen,) = _U8.unpack(take(1))
        hb = take(hlen)
        blen = u32()
        blob = take(blen)
        h = hb.hex()
        if blake2b(blob, digest_size=32).hexdigest() != h:
            raise ValueError("blob hash mismatch")
        store._blobs[h] = blob
    nl = u32()
    levels: list[dict[tuple[int, int], str]] = []
    for _ in range(nl):
        nt = u32()
        level: dict[tuple[int, int], str] = {}
        for _t in range(nt):
            ti = u32()
            tj = u32()
            hb = take(32)
            if hb.hex() not in store:
                raise ValueError(f"manifest references missing tile {hb.hex()[:12]}")
            level[(ti, tj)] = hb.hex()
        levels.append(level)
    if off != len(body):
        raise ValueError("trailing bytes in file body")
    return Pyramid(grid, tile_size, levels, store, features)


def content_hash(data: bytes) -> str:
    """Stable digest of a whole .grist file (includes the embedded footer)."""
    return blake2b(data, digest_size=32).hexdigest()


def save(pyr: Pyramid, path: str) -> str:
    data = serialize(pyr)
    with open(path, "wb") as fh:
        fh.write(data)
    return content_hash(data)


def load(path: str) -> Pyramid:
    with open(path, "rb") as fh:
        return deserialize(fh.read())
