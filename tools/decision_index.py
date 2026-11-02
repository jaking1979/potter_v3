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
