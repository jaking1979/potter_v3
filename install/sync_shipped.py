#!/usr/bin/env python3
"""Install the shipped skills and global rules without overwriting what Potter changed locally.

  sync_shipped.py --repo REPO --ws WS                 install/refresh; never overwrite a locally edited file
  sync_shipped.py --repo REPO --ws WS --report        only rebuild the drift report (state/shipped/DRIFT.md, drift.json)
  sync_shipped.py --repo REPO --ws WS --accept PATH   take the repo's version of PATH (a file or a skill folder,
                                                      relative to the workspace); the local copy is archived first

How a local edit is recognised: state/shipped/manifest.json records the git blob id of every file as it was
installed. A file whose content is neither that nor the new version was changed here. For files installed before
the manifest existed, any version from the repo's history counts as "not changed here".

A kept file gets the repo's new version beside it as <file>.upstream, and is listed in the drift report until the
repo catches up (the file then matches and goes back to being refreshed) or the owner accepts the repo's version.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

SHIPPED = ("skills", "rules/global")          # relative to overlay/workspace and to the workspace
UPSTREAM = ".upstream"
MAX_NEW_FILE = 20000                          # bytes of a new local file quoted in the report


def blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def shipped_files(src: Path) -> dict[str, Path]:
    out = {}
    for top in SHIPPED:
        base = src / top
        for p in sorted(base.rglob("*")) if base.exists() else []:
            if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(UPSTREAM):
                if top == "rules/global" and p.suffix != ".md":
                    continue
                out[p.relative_to(src).as_posix()] = p
    return out


def history(repo: Path) -> dict[str, set[str]]:
    """Every blob id each shipped path has had in the repo's (possibly shallow) history."""
    seen: dict[str, set[str]] = {}
    try:
        r = subprocess.run(["git", "-C", str(repo), "log", "--raw", "--no-abbrev", "--format=", "--",
                            *[f"overlay/workspace/{t}" for t in SHIPPED]], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return seen
    for line in r.stdout.splitlines():
        if not line.startswith(":"):
            continue
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 4 and path.startswith("overlay/workspace/"):
            seen.setdefault(path[len("overlay/workspace/"):], set()).add(parts[3])
    return seen


def commit(repo: Path) -> str:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def sync(repo: Path, ws: Path, dry: bool = False) -> dict:
    src = repo / "overlay/workspace"
    state = ws / "state/shipped"
    manifest = load(state / "manifest.json", {"files": {}})
    files = manifest.setdefault("files", {})
    hist = None
    counts = {"added": 0, "updated": 0, "same": 0, "kept": 0}
    kept = []
    for rel, sp in shipped_files(src).items():
        data = sp.read_bytes()
        up = blob(data)
        dest = ws / rel
        loc = blob(dest.read_bytes()) if dest.is_file() else None
        base = (files.get(rel) or {}).get("blob")
        side = dest.with_name(dest.name + UPSTREAM)
        if loc is not None and loc != up and loc != base:
            if base is None:
                hist = history(repo) if hist is None else hist
                unchanged = loc in hist.get(rel, set())
            else:
                unchanged = False
            if not unchanged:
                counts["kept"] += 1
                kept.append(rel)
                if not dry:
                    side.write_bytes(data)
                    files[rel] = {"blob": base, "upstream": up}
                continue
        status = "added" if loc is None else "same" if loc == up else "updated"
        counts[status] += 1
        if not dry:
            if status != "same":
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sp, dest)
            side.unlink(missing_ok=True)
            files[rel] = {"blob": up}
    if not dry:
        manifest["commit"] = commit(repo)
        manifest["synced"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        state.mkdir(parents=True, exist_ok=True)
        (state / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
        report(repo, ws)
    return {"counts": counts, "kept": kept}


def notes_for(ws: Path, rel: str) -> list[str]:
    try:
        lines = (ws / "state/shipped/notes.md").read_text().splitlines()
    except OSError:
        return []
    key = rel.split("/")[1] if rel.startswith("skills/") else rel.rsplit("/", 1)[-1]
    return [ln.strip() for ln in lines if rel in ln or key in ln][-5:]


def report(repo: Path, ws: Path) -> dict:
    """What differs here from the repo: edited shipped files (with a diff) and files Potter added in shipped folders."""
    src = repo / "overlay/workspace"
    shipped = shipped_files(src)
    edited, new = [], []
    for rel, sp in shipped.items():
        dest = ws / rel
        if not dest.is_file():
            continue
        a, b = sp.read_bytes(), dest.read_bytes()
        if a == b:
            continue
        try:
            diff = "".join(difflib.unified_diff(a.decode().splitlines(True), b.decode().splitlines(True),
                                                f"repo/{rel}", f"potter/{rel}"))
        except UnicodeDecodeError:
            diff = "(binary file differs)"
        edited.append({"path": rel, "diff": diff, "notes": notes_for(ws, rel)})
    for top in SHIPPED:
        base = ws / top
        for p in sorted(base.rglob("*")) if base.exists() else []:
            rel = p.relative_to(ws).as_posix()
            if (p.is_file() and rel not in shipped and not p.name.endswith(UPSTREAM) and "__pycache__" not in p.parts
                    and not (top == "rules/global" and p.suffix != ".md")):
                text = p.read_bytes()[:MAX_NEW_FILE].decode(errors="replace")
                new.append({"path": rel, "content": text, "notes": notes_for(ws, rel)})
    out = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "repo_commit": commit(repo),
           "edited": edited, "new": new}
    state = ws / "state/shipped"
    state.mkdir(parents=True, exist_ok=True)
    (state / "drift.json").write_text(json.dumps(out, indent=1) + "\n")
    md = [f"# Local changes to Potter's shipped files", "",
          f"Generated {out['generated']} against repo {out['repo_commit']}. "
          f"{len(edited)} edited, {len(new)} new.", ""]
    for e in edited:
        md += [f"## Edited: `{e['path']}`"] + [f"- note: {n}" for n in e["notes"]] + ["", "```diff", e["diff"].rstrip(), "```", ""]
    for n in new:
        md += [f"## New: `{n['path']}`"] + [f"- note: {x}" for x in n["notes"]] + ["", "```", n["content"].rstrip(), "```", ""]
    (state / "DRIFT.md").write_text("\n".join(md) + "\n")
    return out


def accept(repo: Path, ws: Path, target: str) -> list[str]:
    src = repo / "overlay/workspace"
    target = target.strip("/")
    chosen = [r for r in shipped_files(src) if r == target or r.startswith(target + "/")]
    if not chosen:
        raise SystemExit(f"{target}: not a shipped file or folder")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    manifest = load(ws / "state/shipped/manifest.json", {"files": {}})
    done = []
    for rel in chosen:
        dest, sp = ws / rel, src / rel
        if dest.is_file() and dest.read_bytes() != sp.read_bytes():
            arch = ws / "state/shipped/archive" / stamp / rel
            arch.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, arch)
            done.append(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, dest)
        dest.with_name(dest.name + UPSTREAM).unlink(missing_ok=True)
        manifest.setdefault("files", {})[rel] = {"blob": blob(sp.read_bytes())}
    (ws / "state/shipped/manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    report(repo, ws)
    return done


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--ws", required=True)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--accept")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)
    repo, ws = Path(ns.repo), Path(ns.ws)
    if ns.accept:
        done = accept(repo, ws, ns.accept)
        print(f"took the repo's version of {ns.accept}" + (f"; your copy is in state/shipped/archive ({len(done)} file(s))" if done else ""))
        return 0
    if ns.report:
        r = report(repo, ws)
        print(f"{len(r['edited'])} edited, {len(r['new'])} new (state/shipped/DRIFT.md)")
        return 0
    r = sync(repo, ws, ns.dry_run)
    c = r["counts"]
    line = f"{c['same']} up to date, {c['updated']} updated, {c['added']} added"
    if r["kept"]:
        line += f", {c['kept']} kept because they were changed here (repo version beside each as .upstream): " + ", ".join(r["kept"])
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
