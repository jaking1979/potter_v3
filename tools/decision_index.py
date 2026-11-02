#!/usr/bin/env python3
"""B2 decision index: canonical-wording hashes, drift check, release-test IDs.

Ratified decision wording lives in Notion (the canonical source). This tool keeps
an exact snapshot of each decision's wording in governance/decisions/wording/<ID>.md
and its SHA-256 in governance/decisions/manifest.json.

  check                 verify every snapshot matches its manifest hash (CI gate)
  extract ID EXPORT     print the verbatim Decision & Reconciliation Record for ID
                        from a raw Notion page export (the snapshot source)
  compare ID EXPORT     extract from a fresh Notion export and compare to snapshot
  coverage              list ratified decisions not yet indexed (exit 3 if any)
  compare-all EXPORT --page-id ID --retrieved-at T --page-last-edited T --candidate SHA
                        L16 source-check record (JSON) for every indexed decision
  verify-source-check RECORD EXPORT --candidate SHA --now T
                        check a record's bindings and freshness (L16 b, d, e)
  rt-ids [ID]           list release-test IDs derived from snapshots
  hash FILE             print the canonical hash of a file

Snapshots are produced only by `extract`, so a snapshot is reproducible from
the canonical Notion export rather than hand-copied. CI has no Notion
credentials: `compare` runs in an agent session that can read Notion; CI runs
`check`. A coordinated edit of wording+hash is caught by `compare`, and the
manifest hash is bound into every T39 attestation.
Exit codes: 0 ok, 1 usage error, 2 drift/mismatch, 3 incomplete coverage.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECISIONS = ROOT / "governance" / "decisions"
MANIFEST = DECISIONS / "manifest.json"
ID_RE = re.compile(r"^T\d+(?:-[A-Za-z0-9]+)*$")
EXPECTED = DECISIONS / "expected.json"
RT_LINE = re.compile(r"^\s*(\d+)\.\s+(.+)$")
RT_INLINE = re.compile(r"\((\d+)\)\s+(.+?)(?=;\s*\(\d+\)\s|$)")
RT_S = re.compile(r"^\s*-\s*(?:<b>)?(S\d+)\b[^<]*(?:</b>)?\s*(.*)$")
TAG = re.compile(r"<[^>]+>")


def canonical(text: str) -> str:
    """Normalize only presentation noise: Unicode form, line endings, trailing
    whitespace and blank-line runs. Any change to words still changes the hash."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln == "" and (not out or out[-1] == ""):
            continue
        out.append(ln)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(canonical(text).encode("utf-8")).hexdigest()


def load_manifest(path: Path = MANIFEST) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = [d["id"] for d in data["decisions"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate decision id in manifest")
    for d in data["decisions"]:
        if not ID_RE.match(d["id"]):
            raise ValueError(f"bad decision id: {d['id']}")
    return data


def wording_path(entry: dict, base: Path = DECISIONS) -> Path:
    return base / entry["wording_file"]


def check(manifest_path: Path = MANIFEST) -> list[str]:
    """Return a list of problems; empty means every snapshot matches its hash."""
    problems = []
    data = load_manifest(manifest_path)
    base = manifest_path.parent
    for entry in data["decisions"]:
        p = wording_path(entry, base)
        if not p.exists():
            problems.append(f"{entry['id']}: missing wording file {entry['wording_file']}")
            continue
        actual = digest(p.read_text(encoding="utf-8"))
        if actual != entry["sha256"]:
            problems.append(f"{entry['id']}: hash mismatch (manifest {entry['sha256'][:12]}, file {actual[:12]})")
    listed = {wording_path(e, base).resolve() for e in data["decisions"]}
    for f in sorted((base / "wording").glob("*.md")):
        if f.resolve() not in listed:
            problems.append(f"unlisted wording file {f.name}")
    return problems


def compare(decision_id: str, notion_export: str, manifest_path: Path = MANIFEST) -> bool:
    """True if the record extracted from a fresh Notion export matches the snapshot."""
    data = load_manifest(manifest_path)
    for entry in data["decisions"]:
        if entry["id"] == decision_id:
            snap = wording_path(entry, manifest_path.parent).read_text(encoding="utf-8")
            return digest(snap) == digest(extract(decision_id, notion_export))
    raise KeyError(decision_id)


def extract(decision_id: str, export: str) -> str:
    """Verbatim body of '## Decision & Reconciliation Record — <ID>' up to the next
    level-1/2 heading or the end of the page content."""
    head = re.compile(r"^##\s+Decision & Reconciliation Record\s+[—-]\s+" + re.escape(decision_id) + r"\b.*$", re.M)
    m = head.search(export)
    if not m:
        raise KeyError(f"no Decision & Reconciliation Record for {decision_id}")
    rest = export[m.end():]
    stop = re.search(r"^#{1,2}\s|^</content>", rest, re.M)
    body = rest[: stop.start()] if stop else rest
    return canonical(m.group(0) + "\n" + body)


def rt_ids(decision_id: str, text: str) -> list[tuple[str, str]]:
    """Release-test IDs from a record's acceptance list. Supported forms (as used
    in the Council record): numbered lines after an 'Acceptance' label
    (RT-<ID>-<n>), an inline '(1) ...; (2) ...' list (RT-<ID>-<n>), and the T37
    '- **S<n>**' appendix bullets (RT-<ID>-S<n>)."""
    out: list[tuple[str, str]] = []
    in_acc = False
    for ln in canonical(text).split("\n"):
        plain = TAG.sub("", ln).strip()
        ms = RT_S.match(ln)
        if ms:
            out.append((f"RT-{decision_id}-{ms.group(1)}", TAG.sub("", ms.group(2)).strip()))
            continue
        if re.match(r"^(?:#+\s*)?acceptance\b", plain, re.I):
            in_acc = True
            after = plain.split(":", 1)[1] if ":" in plain else ""
            for n, desc in RT_INLINE.findall(after):
                out.append((f"RT-{decision_id}-{n}", desc.strip().rstrip(".")))
            continue
        if in_acc:
            m = RT_LINE.match(plain)
            if m:
                out.append((f"RT-{decision_id}-{m.group(1)}", m.group(2).strip()))
            elif plain and not out:
                continue
            elif plain:
                in_acc = False
    num = [int(i.rsplit("-", 1)[1]) for i, _ in out if i.rsplit("-", 1)[1].isdigit()]
    if num and num != list(range(1, len(num) + 1)):
        raise ValueError(f"{decision_id}: acceptance tests must be numbered 1..n, got {num}")
    if len({i for i, _ in out}) != len(out):
        raise ValueError(f"{decision_id}: duplicate release-test IDs")
    return out


SOURCE_CHECK_SCHEMA = "potter.source-check/v0"
FRESHNESS_S = 24 * 3600  # L16 (d)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_sha256(manifest_path: Path = MANIFEST) -> str:
    return _sha256(manifest_path.read_bytes())


def compare_all(export: str, page_id: str, retrieved_at: int, page_last_edited: int,
                candidate: str, manifest_path: Path = MANIFEST) -> dict:
    """L16 (b): a source-check record binding the export, page, candidate and
    manifest to a per-decision result. The reviewer signs it with its T39 key;
    signature handling belongs to the receipt gate (B1)."""
    results = {}
    for entry in load_manifest(manifest_path)["decisions"]:
        try:
            results[entry["id"]] = compare(entry["id"], export, manifest_path)
        except KeyError:
            results[entry["id"]] = False
    return {
        "schema": SOURCE_CHECK_SCHEMA,
        "page_id": page_id,
        "export_sha256": _sha256(export.encode("utf-8")),
        "retrieved_at": int(retrieved_at),
        "page_last_edited": int(page_last_edited),
        "candidate": candidate,
        "manifest_sha256": manifest_sha256(manifest_path),
        "results": results,
        "pass": bool(results) and all(results.values()),
    }


def verify_source_check(record: dict, export: str, candidate: str, now: int,
                        manifest_path: Path = MANIFEST) -> list[str]:
    """L16 (b, d, e) binding and freshness checks; empty list means acceptable.
    Re-derives the results from the supplied export rather than trusting them."""
    problems = []
    if not isinstance(record, dict) or record.get("schema") != SOURCE_CHECK_SCHEMA:
        return ["unknown or missing schema"]
    if record.get("candidate") != candidate:
        problems.append("bound to a different candidate")
    if record.get("manifest_sha256") != manifest_sha256(manifest_path):
        problems.append("bound to a different manifest")
    if record.get("export_sha256") != _sha256(export.encode("utf-8")):
        problems.append("export does not match the recorded export hash")
    ra, le = record.get("retrieved_at"), record.get("page_last_edited")
    if not (isinstance(ra, int) and isinstance(le, int)):
        problems.append("missing timestamps")
    else:
        if le > ra:
            problems.append("page edited after retrieval")
        if not (0 <= now - ra < FRESHNESS_S):
            problems.append("stale or future-dated retrieval")
    fresh = compare_all(export, record.get("page_id", ""), ra or 0, le or 0, candidate, manifest_path)
    if fresh["results"] != record.get("results") or not fresh["pass"] or record.get("pass") is not True:
        problems.append("comparison failed or results do not reproduce")
    return problems


def coverage(manifest_path: Path = MANIFEST, expected_path: Path = EXPECTED) -> list[str]:
    have = {d["id"] for d in load_manifest(manifest_path)["decisions"]}
    want = json.loads(expected_path.read_text(encoding="utf-8"))["ratified"]
    return [i for i in want if i not in have]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]
    if cmd == "check":
        problems = check()
        for p in problems:
            print(f"DRIFT: {p}")
        if not problems:
            print(f"ok: {len(load_manifest()['decisions'])} decisions match manifest")
        return 2 if problems else 0
    if cmd == "compare" and len(args) == 2:
        same = compare(args[0], Path(args[1]).read_text(encoding="utf-8"))
        print("ok: matches snapshot" if same else f"DRIFT: Notion wording for {args[0]} differs from snapshot")
        return 0 if same else 2
    if cmd == "extract" and len(args) == 2:
        sys.stdout.write(extract(args[0], Path(args[1]).read_text(encoding="utf-8")))
        return 0
    if cmd == "compare-all" and len(args) == 9:
        opts = dict(zip(args[1::2], args[2::2]))
        rec = compare_all(Path(args[0]).read_text(encoding="utf-8"), opts["--page-id"],
                          int(opts["--retrieved-at"]), int(opts["--page-last-edited"]), opts["--candidate"])
        print(json.dumps(rec, indent=2, sort_keys=True))
        return 0 if rec["pass"] else 2
    if cmd == "verify-source-check" and len(args) == 6:
        opts = dict(zip(args[2::2], args[3::2]))
        problems = verify_source_check(json.loads(Path(args[0]).read_text(encoding="utf-8")),
                                       Path(args[1]).read_text(encoding="utf-8"),
                                       opts["--candidate"], int(opts["--now"]))
        for p_ in problems:
            print(f"REFUSED: {p_}")
        if not problems:
            print("ok: source check binds this candidate and manifest, fresh, all match")
        return 2 if problems else 0
    if cmd == "coverage":
        missing = coverage()
        print("ok: all ratified decisions indexed" if not missing else "NOT INDEXED: " + ", ".join(missing))
        return 3 if missing else 0
    if cmd == "rt-ids":
        data = load_manifest()
        for entry in data["decisions"]:
            if args and entry["id"] != args[0]:
                continue
            for rid, desc in rt_ids(entry["id"], wording_path(entry).read_text(encoding="utf-8")):
                print(f"{rid}\t{desc}")
        return 0
    if cmd == "hash" and len(args) == 1:
        print(digest(Path(args[0]).read_text(encoding="utf-8")))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
