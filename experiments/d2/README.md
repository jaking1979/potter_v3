# D2 offline registry experiment

This is an isolated synthetic prototype for D2 (T7; T14/T2-6; T36), a T2 Dolores-owned package requiring Claude's exact-candidate review. The Build Plan gates merge/integration on B2/B4/C1. Offline build-ahead does not satisfy those dependencies or release acceptance.

`registry/bound.py` now verifies the supplied manifest's exact raw-byte SHA-256 against a pin held outside that manifest in `registry/synthetic_authority.py`. Schema identity, version, complete required class inventory, deferred membership, per-class capability coverage, strict JSON and disabled deferred capabilities are checked. Generated registry bytes must match all entries, including disabled entries and order. The offline lookup validates both inputs on every call and denies mismatches and unknown/disabled names. There is no caller digest, approval flag or deferred-list override on this path.

The checked-in pin and manifest are **synthetic test authority**, not an authenticated export from Notion or B2/B4. The three fixture classes and capabilities are illustrative, not the ratified canonical set. Updating the pin is a reviewed code/trust-configuration change; a caller cannot self-assert a digest in the input. Digest identity is over exact bytes, including the final newline; whitespace or ordering changes require a pin change. Deterministic registry generation sorts capability names independently of source ordering.

The old `deferred.py` functions remain untrusted low-level transformations for comparison tests; callers must not use them as runtime authorization. The bound lookup's `True` means only that a synthetic fixture entry is enabled. It grants no execution permission. Python code/configuration and the process are trusted in this offline threat model; no sandbox against callers modifying module globals or code is claimed.

Still absent: authenticated canonical source acquisition/identity, full T14/skill definition schema and hashes, canonical class-to-capability completeness evidence, B2/B4 decision/RT mapping, rollback/revocation/update custody and C1 runtime enforcement. No production wiring or side effects exist. T7/T14/T36 acceptance, T22 readiness and T39 signed review/merge evidence remain open. This branch is not a signed receipt, merge approval or runtime-ready claim.

Run: `python3 -m unittest discover -s tests -v`.

Local reconstructed candidate: 13 tests passed, including original four plus pinned authority/registry omission, unknown extra class/entry, changed enabled bits, attempted self-hash/promotion, malformed/duplicate JSON, schema/version mismatch, deterministic bytes and fail-closed lookup. Mocked pin updates in defense-in-depth tests exercise malformed but otherwise digest-matching source rejection; the production API has no such override. No repository CI result is claimed.

