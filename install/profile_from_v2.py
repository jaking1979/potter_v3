#!/usr/bin/env python3
"""Build the owner's personal profile for Potter v3 from a Potter v2 export (schema potter.v2-export/2).

v3's machinery rules ship in the repo. What is personal (the owner's rules for domains, projects and people,
preferences, communication style, past learnings) is NOT stored in the repo: it is built here, at install, from
the export the owner uploads. Old machinery (v2's global rules, prompts, wiki) is kept as read-only reference
under `reference/v2/`, never as active rules.

  profile_from_v2.py plan  EXPORT.zip
  profile_from_v2.py apply EXPORT.zip --workspace DIR --state-dir DIR
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_v2 import SECRET_NAME, SECRET_TEXT, BundleError, check_zip  # noqa: E402

SCHEMA2 = "potter.v2-export/2"
MARK = "<!-- PERSONAL-INDEX"
ID32 = re.compile(r"\b[0-9a-f]{32}\b|\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I)
WALL = re.compile(r"EHR|hard wall|excluded", re.I)
SKIP_ACTIVE = {"rules/projects/potter-v2-rebuild.md"}            # about rebuilding v2 itself
# v2 path (relative to its workspace) -> (v3 path, active?)
ROUTES = [
    (re.compile(r"^USER\.md$"), lambda m, p: "USER.md", True),             # who the owner is: live, not reference
    (re.compile(r"^rules/(people|domains|projects)/[^/]+\.md$"), lambda m, p: p, True),
    (re.compile(r"^preferences/.+\.md$"), lambda m, p: p, True),
    (re.compile(r"^rules/global/communication\.md$"), lambda m, p: "rules/personal/communication-style.md", True),
    (re.compile(r"^\.learnings/(ERRORS|CORRECTIONS|OPEN_QUESTIONS)\.md$"), lambda m, p: f"learnings/{m.group(1)}.md", True),
    (re.compile(r"^trading/[^/]+\.md$"), lambda m, p: p, True),                    # mandates are authority files
    (re.compile(r"^(rules/.+\.md|prompts/.+\.md|wiki/.+\.md|AGENTS\.md|SOUL\.md|IDENTITY\.md|USER\.md|TOOLS\.md|"
                r"HEARTBEAT\.md|MEMORY\.md|state/[^/]+\.md)$"), lambda m, p: "reference/v2/" + p, False),
]


def find_workspace(names: list[str]) -> str:
    hits = sorted(n[: -len("rules/INDEX.md")] for n in names if n.endswith("/.openclaw/workspace/rules/INDEX.md"))
    if not hits:
        raise BundleError("no workspace found in the export (expected .../.openclaw/workspace/rules/INDEX.md)")
    return hits[0]


def read_manifest(z: zipfile.ZipFile) -> dict:
    """manifest.json at the top, or one folder down (GitHub's "Download ZIP" wraps everything in a folder)."""
    cands = [n for n in z.namelist() if n == "manifest.json" or (n.count("/") == 1 and n.endswith("/manifest.json"))]
    if len(cands) != 1:
        raise BundleError("manifest.json is missing")
    try:
        return json.loads(z.read(cands[0]))
    except ValueError:
        raise BundleError("manifest.json is not valid JSON")


def route(rel: str):
    if rel in SKIP_ACTIVE:
        return "reference/v2/" + rel, False
    for rx, fn, active in ROUTES:
        m = rx.match(rel)
        if m:
            return fn(m, rel), active
    return None, False


def excluded_ids(texts: list[str]) -> list[str]:
    """Ids named on a line that talks about the EHR / a hard wall / excluded material."""
    out = []
    for t in texts:
        for line in t.splitlines():
            if WALL.search(line):
                for m in ID32.finditer(line):
                    i = m.group(0).lower().replace("-", "")
                    if i not in out:
                        out.append(i)
    return out


def rewrite(text: str, old_ws: str, ids: list[str]) -> str:
    text = text.replace(old_ws, "").replace("~/.openclaw/workspace/", "")
    text = text.replace(".learnings/", "learnings/")
    def red(m):
        return "[excluded page]" if m.group(0).lower().replace("-", "") in ids else m.group(0)
    return ID32.sub(red, text)


def index_rows(v2_index: str, active: set[str]) -> list[str]:
    """Carry v2's trigger rows whose target file is an active personal file in v3."""
    rows = []
    for line in v2_index.splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        targets = re.findall(r"`([^`]+\.md)`", cells[-1])
        keep = []
        for t in targets:
            t = t.replace("../", "")
            t = t.replace(".learnings/", "learnings/")
            cand = t if t.startswith(("preferences/", "learnings/", "trading/")) else "rules/" + t
            if cand in active:
                keep.append(cand[len("rules/"):] if cand.startswith("rules/") else "../" + cand)
        if keep:
            rows.append(f"| {cells[0]} | " + ", ".join(f"`{k}`" for k in dict.fromkeys(keep)) + " |")
    return rows


def build_plan(z: zipfile.ZipFile) -> dict:
    check_zip(z)
    man = read_manifest(z)
    if man.get("schema") != SCHEMA2:
        raise BundleError(f"manifest schema must be {SCHEMA2}")
    names = z.namelist()
    ws = find_workspace(names)
    old_ws = "/" + ws[ws.index("home/"):] if "home/" in ws else "/" + ws
    # v2's main agent worked in workspace/main/; the top level holds older copies plus files only it has (such as
    # preferences/). Both are read; for a file in both, main's (the live one) wins.
    chosen, skipped = {}, []
    for n in names:
        if not n.startswith(ws) or n.endswith("/"):
            continue
        rel, prio = n[len(ws):], 0
        if rel.startswith("main/"):
            rel, prio = rel[len("main/"):], 1
        if rel.startswith(("merrick/", "main/")) or "/." in "/" + rel and not rel.startswith(".learnings/"):
            continue                                              # Merrick's workspace and hidden dirs
        if rel not in chosen or prio > chosen[rel][1]:
            chosen[rel] = (n, prio)
    files = []
    for rel, (n, prio) in sorted(chosen.items()):
        dest, active = route(rel)
        if dest is None:
            continue
        if SECRET_NAME.search(rel):
            skipped.append({"file": rel, "why": "looks like a secret file"})
            continue
        files.append({"src": n, "rel": rel, "dest": dest, "active": active})
    return {"ws": ws, "old_ws": old_ws, "files": files, "skipped": skipped}


def public(plan: dict) -> dict:
    return {"active_files": sorted(f["dest"] for f in plan["files"] if f["active"]),
            "reference_files": sum(1 for f in plan["files"] if not f["active"]),
            "skipped": plan["skipped"]}


def apply(zpath: Path, workspace: Path, state_dir: Path) -> dict:
    with zipfile.ZipFile(zpath) as z:
        plan = build_plan(z)
        raw = {}
        for f in plan["files"]:
            data = z.read(f["src"])
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                plan["skipped"].append({"file": f["rel"], "why": "not text"})
                continue
            if SECRET_TEXT.search(text):
                plan["skipped"].append({"file": f["rel"], "why": "contains something that looks like a secret"})
                continue
            raw[f["rel"]] = (f, text)
        ids = excluded_ids([t for _, t in raw.values()])
        active = set()
        for rel, (f, text) in raw.items():
            out = workspace / f["dest"]
            out.parent.mkdir(parents=True, exist_ok=True)
            body = rewrite(text, plan["old_ws"].rstrip("/") + "/", ids)
            if not f["active"]:
                body = "> Reference only: how the previous assistant did this. Not an active rule.\n\n" + body
            elif f["dest"].startswith("rules/"):
                body = ("> Your preferences and boundaries in this file stand. Where it describes HOW something is run "
                        "(services, scripts, schedules), `rules/global/` describes how v3 does it and wins.\n\n" + body)
            out.write_text(body, encoding="utf-8")
            if f["active"]:
                active.add(f["dest"])
        # personal index rows
        idx = workspace / "rules/INDEX.md"
        v2_index = raw.get("rules/INDEX.md", (None, ""))[1]
        rows = index_rows(v2_index, active)
        listed = " ".join(rows)
        for a in sorted(active):                                   # anything v2's index did not mention
            key = a[len("rules/"):] if a.startswith("rules/") else "../" + a
            if f"`{key}`" not in listed:
                rows.append(f"| {Path(a).stem.replace('-', ' ')} | `{key}` |")
        if idx.exists() and MARK in idx.read_text(encoding="utf-8"):
            head = idx.read_text(encoding="utf-8").split(MARK)[0]
            idx.write_text(head + "| When | File |\n|---|---|\n" + "\n".join(rows) + "\n", encoding="utf-8")
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "excluded-ids.json").write_text(json.dumps(ids), encoding="utf-8")
        rep = public({"files": [f for f, _ in raw.values()], "skipped": plan["skipped"]})
        rep["index_rows"] = len(rows)
        rep["excluded_ids_found"] = len(ids)
        return rep


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("bundle")
    a = sub.add_parser("apply"); a.add_argument("bundle")
    a.add_argument("--workspace", required=True); a.add_argument("--state-dir", required=True)
    ns = ap.parse_args(argv)
    try:
        if ns.cmd == "plan":
            with zipfile.ZipFile(ns.bundle) as z:
                print(json.dumps(public(build_plan(z)), indent=1))
        else:
            print(json.dumps(apply(Path(ns.bundle), Path(ns.workspace), Path(ns.state_dir)), indent=1))
        return 0
    except (BundleError, zipfile.BadZipFile) as e:
        print(f"profile import failed: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
