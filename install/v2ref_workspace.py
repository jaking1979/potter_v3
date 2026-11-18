#!/usr/bin/env python3
"""Build the v2 baseline agent's workspace from the v2 files the import already put in v3's workspace.

The T1 evaluation compares v3 with v2 without touching live v2 and without the owner copying answers by hand:
v2's own machinery (AGENTS.md, SOUL.md, IDENTITY.md, TOOLS.md, MEMORY.md, rules, prompts, wiki, state notes) is
in reference/v2/, and v2's personal files (USER.md, preferences, people/domain/project rules, learnings) are live in
v3's workspace. Together they are v2's brain as of the export. v3's own additions (its global rules, skills, state)
are left out, so the baseline answers with v2's files only.

  v2ref_workspace.py --from <v3 workspace> --to <v2ref workspace>
"""
import argparse
import shutil
import sys
from pathlib import Path

PERSONAL = ["USER.md", "preferences", "learnings", "rules/people", "rules/domains", "rules/projects", "rules/personal"]


def build(src: Path, dst: Path) -> dict:
    ref = src / "reference" / "v2"
    if not ref.is_dir():
        raise SystemExit("v2ref: no reference/v2 in the workspace (was the v2 export imported?)")
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    n = 0
    for f in ref.rglob("*"):
        if f.is_file():
            t = dst / f.relative_to(ref)
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, t)
            n += 1
    for rel in PERSONAL:
        s = src / rel
        if s.is_file():
            shutil.copy2(s, dst / rel); n += 1
        elif s.is_dir():
            for f in s.rglob("*"):
                if f.is_file():
                    t = dst / rel / f.relative_to(s)
                    t.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, t); n += 1
    (dst / "state").mkdir(exist_ok=True)
    return {"files": n, "has_agents_md": (dst / "AGENTS.md").exists(), "has_soul": (dst / "SOUL.md").exists()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="dst", required=True)
    a = ap.parse_args(argv)
    r = build(Path(a.src), Path(a.dst))
    print(f"v2 baseline workspace: {r['files']} files (AGENTS.md {'yes' if r['has_agents_md'] else 'no'}, SOUL.md {'yes' if r['has_soul'] else 'no'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
