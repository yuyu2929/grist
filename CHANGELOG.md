# Changelog

All notable changes to Grist are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/). The `.grist` container format
has its own `FORMAT_VERSION` in `grist/codec.py`; a breaking change to it bumps
the major version.

## [0.1.1] - 2026-10-25

Correctness audit. The geometry kernel held up (cell sums matched a shapely
oracle to 1e-15 on every shape tested), but several layers around it did not.

### Fixed

- **Incremental update dropped boundary-coincident geometry.** `update()`
  selects affected features with a strict bbox overlap test
  (`b[2] > r[0]`), but level-0 tiles are half-open. A point or a line lying
  exactly on a tile's left/bottom edge belongs to that tile and was never
  recomputed: the updated pyramid lost it, while a fresh bake kept it
  (measured: point total 1.0 vs 2.0, line length 0.0 vs 1.6). The test is now
  inclusive on both sides — over-including costs a little work, under-including
  is wrong.
- **Duplicate feature ids silently deleted data.** `Pyramid.bake` built
  `{f.fid: f for f in features}`, so two features sharing an id collapsed into
  one and half the input vanished without a word (two points with id 1 produced
  a point total of 1.0). `features.index_features()` now raises with the
  offending ids, and is applied at the GeoJSON boundary, in `bake()`,
  `bake_parallel()` and `from_acc()`.
- **`cell_owner(policy=...)` ignored its argument.** `POLICIES` advertised
  `("sum", "max", "top")` but the parameter was never read, so `sum` and `max`
  silently behaved like `top`. The three are now distinct and documented:
  `top` → (winning fid, its measure), `max` → (-1, largest single measure),
  `sum` → (-1, total measure); an unknown policy raises `ValueError`.
- **The CLI could mint its own enterprise license.** `grist license --issue`
  fell back to a hard-coded `11…11` demo key, so anyone could generate a valid
  enterprise license with one command — the gate was decorative.
  `--issue` now requires the vendor's `--private-key`.
- **`grist update` without `-o` crashed** with
  `TypeError: expected str, bytes or os.PathLike object, not NoneType`.
  It now explains that the input is not modified in place and exits 2.
- **CLI errors printed raw tracebacks** for a missing file or malformed
  GeoJSON. `main()` now reports `error: ...` and exits non-zero.
- **`python3 tests/run_all.py` failed on a clean machine.** `test_coverage.py`
  imported shapely at module level, so the first thing anyone runs after
  cloning died with `ModuleNotFoundError` even though the library has zero
  dependencies. The file now skips with an install hint.

### Added

- **`container.encrypt` is now implemented** (it was advertised in
  `TIER_FEATURES` with no code behind it). Sealed containers are
  `GRISENC1 | salt | nonce | AES-256-GCM`, keyed by scrypt from a passphrase;
  the format magic is authenticated as associated data, so a wrong passphrase
  or a truncated file fails closed. Needs the optional
  `grist-raster[enterprise]` extra (`cryptography`); the core stays
  dependency-free.
- **`audit.log` now persists.** `Gate(audit_path=...)` appends
  `{t, sub, op, detail}` JSONL lines for save/load/seal operations instead of
  keeping an in-memory list.
- `features.index_features()` — the single place that turns a feature sequence
  into the `{fid: feature}` map, with the duplicate check.
- `tests/test_regressions.py` — 11 regression tests pinning every fix above,
  including two boundary-coincident incremental updates.

### Changed

- **`TIER_FEATURES` only lists implemented features.** `container.encrypt`
  moved to enterprise (it was in pro) and `store.remote` moved to
  `PLANNED_FEATURES` — it was being sold and does not exist. A license never
  grants a planned feature.
- `Gate.from_file()` accepts `audit_path`.
- `cell_owner()` docstring documents the three policies precisely; behaviour
  for `policy="top"` (the default) is unchanged.
- `bake()` docstring now states that it returns `(pyramid, grid)`.

### Verified, not changed

The following were probed adversarially against shapely and found sound, so
they were deliberately left alone: area/length conservation on convex,
concave, holed, hole-touching, sliver and star-shaped polygons (error ≤ 1e-15);
coarse-cell aggregation equals the exact sum of its children (bit-identical);
`bake_parallel` is byte-identical to `bake`; duplicated vertices, collinear
tails, bowties and zero-area rings all match the oracle.

## [0.1.0] - 2024

Initial release: cell-integral vector→raster translation, SCCI coverage kernel,
aggregated pyramid, content-addressed tiles and incremental updates.

[0.1.1]: https://github.com/yuyu2929/grist/releases/tag/v0.1.1
[0.1.0]: https://github.com/yuyu2929/grist/releases/tag/v0.1.0
