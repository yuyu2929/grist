"""grist command line interface.

Subcommands:
    bake     GeoJSON -> .grist pyramid
    query    point/cell queries against a .grist file
    export   dense matrix / PGM / CSV from a .grist file
    update   apply incremental edits (add/remove features from GeoJSON)
    diff     compare two .grist files (tiles / totals)
    stats    pyramid statistics
    verify   integrity check (digests, blob hashes, conservation)
    license  verify a license document against the built-in public key

All subcommands exit 0 on success, non-zero with a message on failure.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Optional

from . import codec
from .bake import bake_geojson, fit_grid
from .features import features_from_geojson, load_features
from .pyramid import Pyramid
from .query import cell_measure, point_query


def _load_pyramid(path: str) -> Pyramid:
    return codec.load(path)


def _cmd_bake(args: argparse.Namespace) -> int:
    with open(args.input, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    pyr = bake_geojson(data, cell=args.cell, cell_count=args.cell_count,
                       tile_size=args.tile, pad=args.pad, max_levels=args.levels)
    h = codec.save(pyr, args.output)
    t = pyr.totals()
    print(f"baked {len(pyr.features)} features into {args.output}")
    print(f"grid: {pyr.grid.w}x{pyr.grid.h} cell={pyr.grid.cell:.6g} "
          f"levels={pyr.n_levels()} tiles={sum(len(l) for l in pyr.levels)}")
    print(f"totals: points={t['point']:.6g} lines={t['line']:.6g} polys={t['polygon']:.6g}")
    print(f"digest: {h}")
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    pyr = _load_pyramid(args.input)
    if args.level >= pyr.n_levels():
        print(f"error: file has {pyr.n_levels()} levels, level {args.level} out of range",
              file=sys.stderr)
        return 2
    r = point_query(pyr, args.x, args.y, level=args.level)
    if not r["inside"]:
        print("outside grid")
        return 0
    print(f"cell {r['cell']} (level-0) -> level-{args.level} cell {r['level_cell']}")
    val = r["value"]
    if val is None:
        print("  empty")
        return 0
    for k, kind in enumerate(("point", "line", "polygon")):
        if not val[k]:
            continue
        fid, m = r["owners"][kind]["top"]
        print(f"  {kind:7s} total={r['owners'][kind]['sum']:.6g} "
              f"top=fid{fid}({m:.6g}) entries={len(val[k])}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    pyr = _load_pyramid(args.input)
    if args.level >= pyr.n_levels():
        print(f"error: file has {pyr.n_levels()} levels", file=sys.stderr)
        return 2
    if args.format == "pgm":
        from .query import pgm
        pgm(pyr, level=args.level, kind=args.kind, path=args.output)
    elif args.format == "csv":
        from .query import csv_cells
        csv_cells(pyr, args.output, level=args.level)
    else:
        from .query import dense_matrix
        m = dense_matrix(pyr, level=args.level, kind=args.kind)
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump({"w": len(m[0]) if m else 0, "h": len(m), "kind": args.kind,
                       "cells": m}, fh)
    print(f"exported {args.format} level {args.level} -> {args.output}")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    if not args.output:
        print("error: update needs -o/--output (the input file is not modified "
              "in place)", file=sys.stderr)
        return 2
    pyr = _load_pyramid(args.input)
    feats = []
    if args.edits:
        with open(args.edits, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        feats = features_from_geojson(data)
    report = pyr.update(removed_fids=args.remove or (), added=feats)
    h = codec.save(pyr, args.output)
    changed = {lvl: n for lvl, n in report["changed"].items() if n}
    print(f"updated {args.output}: changed tiles per level {changed}")
    print(f"digest: {h}")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    a = _load_pyramid(args.a)
    b = _load_pyramid(args.b)
    print(f"features: {len(a.features)} vs {len(b.features)}")
    for lvl in range(max(a.n_levels(), b.n_levels())):
        ta = a.levels[lvl] if lvl < a.n_levels() else {}
        tb = b.levels[lvl] if lvl < b.n_levels() else {}
        added = sorted(set(tb) - set(ta))
        removed = sorted(set(ta) - set(tb))
        modified = sorted(k for k in set(ta) & set(tb) if ta[k] != tb[k])
        print(f"level {lvl}: +{len(added)} -{len(removed)} ~{len(modified)} tiles")
    ta, tb = a.totals(), b.totals()
    for k in ta:
        print(f"total[{k}]: {ta[k]:.6g} vs {tb[k]:.6g} (delta {tb[k]-ta[k]:+.6g})")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    pyr = _load_pyramid(args.input)
    t = pyr.totals()
    print(f"features      : {len(pyr.features)}")
    print(f"grid          : {pyr.grid.w}x{pyr.grid.h} cell={pyr.grid.cell:.6g}")
    print(f"levels        : {pyr.n_levels()}")
    print(f"tiles         : {sum(len(l) for l in pyr.levels)} "
          f"(unique blobs: {len(pyr.store)})")
    print(f"store bytes   : {pyr.store.total_bytes()}")
    print(f"totals        : points={t['point']:.6g} lines={t['line']:.6g} "
          f"polys={t['polygon']:.6g}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        pyr = _load_pyramid(args.input)
    except ValueError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    # every manifest tile present & hash-consistent (checked in deserialize)
    # conservation: totals equal sum of feature measures
    # (sorted iteration + fsum: the check itself must be deterministic)
    t = pyr.totals()
    vals: dict[str, list[float]] = {"point": [], "line": [], "polygon": []}
    for fid in sorted(pyr.features):
        f = pyr.features[fid]
        if f.kind in vals:
            vals[f.kind].append(f.measure_total())
    expect = {k: math.fsum(v) for k, v in vals.items()}
    ok = True
    for k in expect:
        err = abs(t[k] - expect[k])
        tol = 1e-9 * max(1.0, abs(expect[k]))
        flag = "ok" if err <= tol else "MISMATCH"
        if err > tol:
            ok = False
        print(f"{k:7s}: stored={t[k]:.9f} expected={expect[k]:.9f} err={err:.3g} {flag}")
    print("VERIFY OK" if ok else "VERIFY FAILED")
    return 0 if ok else 1


def _cmd_license(args: argparse.Namespace) -> int:
    from .enterprise import verify_license, ed25519_public_key
    if args.issue:
        # Only the vendor can mint: a built-in "demo" issuer key used to live
        # here, which let anybody generate a valid enterprise license with one
        # command - the gate was decorative.
        if not args.private_key:
            print("error: --issue requires --private-key (issuer key, kept "
                  "offline by the vendor)", file=sys.stderr)
            return 2
        from .enterprise import issue_license
        sk = bytes.fromhex(args.private_key)
        doc, pk = issue_license(sk, args.issue, args.tier, args.days)
        print(doc)
        print(f"# public key (hex): {pk.hex()}", file=sys.stderr)
        return 0
    if not args.file:
        print("error: pass --file LICENSE or --issue SUBJECT", file=sys.stderr)
        return 2
    with open(args.file, "r", encoding="utf-8") as fh:
        text = fh.read()
    # Verifying a document only needs the public key. The demo key is the
    # default so `grist license --issue` output can be inspected out of the
    # box; it verifies nothing that was not signed by that demo key.
    pk = bytes.fromhex(args.public_key) if args.public_key else ed25519_public_key(
        bytes.fromhex("11" * 32))
    try:
        lic = verify_license(text, pk)
    except Exception as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    print(f"VALID: sub={lic.sub} tier={lic.tier} feats={list(lic.feats)} "
          f"expires in {lic.days_left()}d")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="grist", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bake", help="GeoJSON -> .grist")
    b.add_argument("input")
    b.add_argument("-o", "--output", default="out.grist")
    b.add_argument("--cell", type=float, default=None)
    b.add_argument("--cell-count", type=int, default=None)
    b.add_argument("--tile", type=int, default=16)
    b.add_argument("--pad", type=float, default=None,
                   help="domain margin around the data bbox "
                        "(default: auto, 1e-9 of the extent, so the half-open "
                        "domain strictly covers the data; 0 = tight, "
                        "boundary-coincident geometry falls outside)")
    b.add_argument("--levels", type=int, default=None)
    b.set_defaults(fn=_cmd_bake)

    q = sub.add_parser("query", help="query a point in a .grist file")
    q.add_argument("input")
    q.add_argument("x", type=float)
    q.add_argument("y", type=float)
    q.add_argument("--level", type=int, default=0)
    q.set_defaults(fn=_cmd_query)

    e = sub.add_parser("export", help="export dense matrix / PGM / CSV")
    e.add_argument("input")
    e.add_argument("output")
    e.add_argument("--format", choices=["dense", "pgm", "csv"], default="dense")
    e.add_argument("--kind", choices=["point", "line", "polygon"], default="polygon")
    e.add_argument("--level", type=int, default=0)
    e.set_defaults(fn=_cmd_export)

    u = sub.add_parser("update", help="incremental update from an edits GeoJSON")
    u.add_argument("input")
    u.add_argument("-o", "--output", default=None)
    u.add_argument("--edits", default=None)
    u.add_argument("--remove", type=int, nargs="*", default=[])
    u.set_defaults(fn=_cmd_update)

    d = sub.add_parser("diff", help="diff two .grist files")
    d.add_argument("a")
    d.add_argument("b")
    d.set_defaults(fn=_cmd_diff)

    s = sub.add_parser("stats", help="pyramid statistics")
    s.add_argument("input")
    s.set_defaults(fn=_cmd_stats)

    v = sub.add_parser("verify", help="integrity + conservation check")
    v.add_argument("input")
    v.set_defaults(fn=_cmd_verify)

    l = sub.add_parser("license", help="issue / verify enterprise licenses")
    l.add_argument("--file", default=None)
    l.add_argument("--issue", default=None, help="mint a license for SUBJECT")
    l.add_argument("--tier", choices=["pro", "enterprise"], default="pro")
    l.add_argument("--days", type=int, default=365)
    l.add_argument("--private-key", default=None, help="hex ed25519 seed (issuer)")
    l.add_argument("--public-key", default=None, help="hex ed25519 pubkey (verifier)")
    l.set_defaults(fn=_cmd_license)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as e:
        print(f"error: file not found: {e.filename or e}", file=sys.stderr)
        return 1
    except (ValueError, KeyError) as e:
        # malformed GeoJSON, duplicate feature ids, corrupt container, ...
        print(f"error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
