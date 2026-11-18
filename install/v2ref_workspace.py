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
import json
import re
import shutil
import sys
from pathlib import Path

# What the owner has told v3 since the install is given to the baseline too, as v2-style daily notes, so T1 compares
# what each design DOES with the same information (v3: graph, Jev, context; v2: flat notes) rather than who heard it.
JOB_TEXT = re.compile(r"^\s*(run the [a-z0-9-]+ skill|read heartbeat|this is an evaluation run|\[cron|system:)", re.I)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") in (None, "text", "input_text"))
    return ""


def owner_messages(sessions: Path, since: str = "") -> list[tuple[str, str]]:
    """(ISO time, text) of user turns in the owner's own sessions; job, heartbeat and evaluation sessions are skipped."""
    out = []
    if not sessions or not sessions.is_dir():
        return out
    for f in sorted(sessions.rglob("*.jsonl")):
        if re.search(r"(t1-eval|cron|heartbeat|isolated|subagent)", f.name, re.I):
            continue
        for line in f.read_text(errors="ignore").splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            m = o.get("message") if isinstance(o.get("message"), dict) else o
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            text = _text(m.get("content")).strip()
            ts = str(o.get("timestamp") or o.get("ts") or m.get("timestamp") or "")
            if len(text) < 3 or JOB_TEXT.match(text) or "t1-eval-" in text or (since and ts and ts < since):
                continue
            out.append((ts, text[:2000]))
    return sorted(set(out))


def write_notes(dst: Path, msgs: list[tuple[str, str]]) -> int:
    by_day: dict[str, list[str]] = {}
    for ts, text in msgs:
        by_day.setdefault(ts[:10] or "undated", []).append(f"- {ts[11:16] or '--:--'} The owner said: {text}")
    (dst / "memory").mkdir(exist_ok=True)
    for day, lines in by_day.items():
        (dst / "memory" / f"{day}.md").write_text(f"# {day}\n\nWhat the owner told the assistant today.\n\n" + "\n".join(lines) + "\n")
    return len(msgs)


PERSONAL = ["USER.md", "preferences", "learnings", "rules/people", "rules/domains", "rules/projects", "rules/personal"]


def build(src: Path, dst: Path, sessions: Path | None = None, since: str = "") -> dict:
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
    told = write_notes(dst, owner_messages(sessions, since)) if sessions else 0
    return {"files": n, "has_agents_md": (dst / "AGENTS.md").exists(), "has_soul": (dst / "SOUL.md").exists(), "owner_messages": told}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="dst", required=True)
    ap.add_argument("--sessions", help="v3's session transcripts: the owner's messages become the baseline's daily notes")
    ap.add_argument("--since", default="", help="ISO date: only messages from then on")
    a = ap.parse_args(argv)
    r = build(Path(a.src), Path(a.dst), Path(a.sessions) if a.sessions else None, a.since)
    print(f"v2 baseline workspace: {r['files']} files (AGENTS.md {'yes' if r['has_agents_md'] else 'no'}, SOUL.md {'yes' if r['has_soul'] else 'no'}; {r['owner_messages']} owner messages as notes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
