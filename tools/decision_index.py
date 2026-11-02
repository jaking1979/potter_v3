#!/usr/bin/env python3
"""B2 decision index: canonical-wording hashes, drift check, release-test IDs.

Ratified decision wording lives in Notion (the canonical source). This tool keeps
an exact snapshot of each decision's wording in governance/decisions/wording/<ID>.md
and its SHA-256 in governance/decisions/manifest.json.

  check                 verify every snapshot matches its manifest hash (CI gate)
  compare ID FILE       compare freshly exported Notion wording against the snapshot
  rt-ids [ID]           list release-test IDs (RT-<ID>-<n>) derived from snapshots
  hash FILE             print the canonical hash of a file

CI has no Notion credentials, so `compare` is run by an agent session that can
read Notion; CI runs `check`. Exit codes: 0 ok, 2 drift/mismatch, 1 usage error.
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
RT_LINE = re.compile(r"^\s*(?:\((\d+)\)|(\d+)\.)\s+(.+)$")


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


def compare(decision_id: str, notion_text: str, manifest_path: Path = MANIFEST) -> bool:
    data = load_manifest(manifest_path)
    for entry in data["decisions"]:
        if entry["id"] == decision_id:
            snap = wording_path(entry, manifest_path.parent).read_text(encoding="utf-8")
            return digest(snap) == digest(notion_text)
    raise KeyError(decision_id)


def rt_ids(decision_id: str, text: str) -> list[tuple[str, str]]:
    """Release-test IDs from the numbered list under '## Acceptance tests'."""
    out, in_section = [], False
    for ln in canonical(text).split("\n"):
        if ln.startswith("## "):
            in_section = ln.strip().lower().startswith("## acceptance tests")
            continue
        if in_section:
            m = RT_LINE.match(ln)
            if m:
                n = m.group(1) or m.group(2)
                out.append((f"RT-{decision_id}-{n}", m.group(3).strip()))
    nums = [int(i.rsplit("-", 1)[1]) for i, _ in out]
    if nums != list(range(1, len(nums) + 1)):
        raise ValueError(f"{decision_id}: acceptance tests must be numbered 1..n, got {nums}")
    return out


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
