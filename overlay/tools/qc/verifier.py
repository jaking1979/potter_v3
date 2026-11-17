#!/usr/bin/env python3
"""Quality control, step 2: check last night's claims against evidence, and check every schedule.

It never re-reads a transcript to confirm a claim. It looks at the world: the scheduler's job list, files in the
workspace, recent commits, and (optionally) a read-only library listing. It never changes anything it checks.

  --mode verify   nightly: verify every claim for the audited day, write follow-ups for the ones the evidence
                  does not support, check every enabled scheduled job (last run ok? ran when due?), and send ONE
                  bounded plain-text report if anything is wrong. Nothing wrong: says nothing.
  --mode recheck  hourly: re-verify only the open items of the most recent day; closes follow-ups that now
                  verify. Always silent.

  verifier.py --state DIR --workspace DIR [--mode verify|recheck] [--date YYYY-MM-DD] [--tz America/New_York]
              [--to <telegram id>] [--openclaw "openclaw"] [--jobs-file FILE] [--library-cmd "..."]
              [--wake-cmd "..."] [--grace-min 10] [--dry-run]

Exit 0 when the checks ran (whatever they found). Exit 1 only when the report could not be delivered.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_TOOLS = str(Path(__file__).resolve().parent.parent)
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
import schedule_watch  # noqa: E402  (the one place schedule health is judged)
from qc import common  # noqa: E402

FOUND, NOT_FOUND, UNKNOWN = "FOUND", "NOT_FOUND", "UNKNOWN"
MAX_RECHECKS = 24
REPORT_MAX_CHARS, REPORT_MAX_ITEMS = 3500, 10
FILE_TYPES = (".md", ".py", ".json", ".jsonl", ".mjs", ".js", ".yaml", ".yml", ".tsv", ".sh", ".txt", ".csv")

STOPWORDS = set("""a an the and or for with of to in on at by is was are were set sets setting will do did done
scheduled schedule reminder reminders follow followup follow-up exists exist created saved filed built fixed sent
verified etc more about into from has have had be been this that these those it its their there cron job list
entry entries notion reference library file files query get add update remove new wrote written added patched
updated committed also already just named called potter openclaw workspace state today tonight""".split())


def keywords(text: str) -> list[str]:
    """Distinctive words (4+ letters, not filler) in the claim and its hint, in order, no repeats."""
    return list(dict.fromkeys(w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
                              if len(w) >= 4 and w not in STOPWORDS))


def explicit_paths(text: str) -> list[str]:
    """Things in the text that look like a relative file path (`a/b.md`, notes/x-*.md)."""
    cands = re.findall(r"`([^`\n]+)`", text or "") + re.findall(r"(?<![\w/.:~-])(?:[\w.-]+/)+[\w.*-]+", text or "")
    out = []
    for c in cands:
        c = c.strip().strip("'\"")
        if c and " " not in c and ("/" in c or re.search(r"\.\w{1,5}$", c)) and not c.startswith(("/", "~", "-")) \
                and ".." not in c.split("/") and "://" not in c:
            out.append(c)
    return list(dict.fromkeys(out))


def check_cron(text: str, jobs: list | None, min_hits: int = 2) -> tuple[str, str]:
    """Is there a scheduled job whose name or payload carries the claim's distinctive words?"""
    if jobs is None:
        return UNKNOWN, "job list not available"
    kws = keywords(text)
    if not kws:
        return UNKNOWN, "no distinctive words to look for"
    for j in jobs:
        if not isinstance(j, dict):
            continue
        payload = j.get("payload") if isinstance(j.get("payload"), dict) else {}
        hay = " ".join(str(v) for v in (j.get("name"), j.get("displayName"), j.get("message"), j.get("command"),
                                        payload.get("message"), payload.get("command")) if v).lower()
        hits = [k for k in kws if k in hay]
        if len(hits) >= min(min_hits, len(kws)):
            return FOUND, f"job '{j.get('displayName') or j.get('name')}' matches {hits[:4]}"
    return NOT_FOUND, f"no scheduled job matches {kws[:6]}"


def check_file(text: str, workspace: Path | None, max_files: int = 50000, min_hits: int = 2) -> tuple[str, str]:
    """Does a named path exist in the workspace, or a file whose name carries a distinctive word?"""
    if workspace is None or not Path(workspace).is_dir():
        return UNKNOWN, "workspace not available"
    root = Path(workspace).resolve()
    paths = explicit_paths(text)
    for rel in paths:
        if any(ch in rel for ch in "*?["):
            try:
                hit = next(iter(root.glob(rel)), None)
            except (ValueError, NotImplementedError, OSError):
                hit = None
            if hit is not None and hit.resolve().is_relative_to(root):
                return FOUND, f"file exists: {hit.relative_to(root)}"
        else:
            cand = (root / rel).resolve()
            if cand.is_relative_to(root) and cand.exists():
                return FOUND, f"file exists: {rel}"
    kws = keywords(text)
    if not kws:
        return UNKNOWN, "no distinctive words to look for"
    names = {Path(p).name.lower() for p in paths}
    seen = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", "venv", ".venv")]
        for fn in files:
            seen += 1
            if seen > max_files:
                return UNKNOWN, "workspace too large to search fully"
            low = fn.lower()
            if os.path.islink(os.path.join(dirpath, fn)):
                continue                         # a link can point outside the workspace: not evidence
            stem = keywords(low.rsplit(".", 1)[0])
            if low in names or (low.endswith(FILE_TYPES) and (sum(k in low for k in kws) >= min(min_hits, len(kws))
                                                               or (stem and all(w in kws for w in stem)))) \
                    or any(fnmatch.fnmatch(low, n) for n in names if "*" in n):
                return FOUND, f"file matches: {fn}"
    return NOT_FOUND, f"no file in the workspace matches {(paths or kws)[:6]}"


def check_text(text: str, haystack: str | None, what: str, min_hits: int = 2) -> tuple[str, str]:
    """Do the claim's distinctive words appear in a listing (recent commits, a library export)?"""
    if haystack is None:
        return UNKNOWN, f"{what} not available"
    kws = keywords(text)
    if not kws:
        return UNKNOWN, "no distinctive words to look for"
    low = haystack.lower()
    hits = [k for k in kws if k in low]
    if len(hits) >= min(min_hits, len(kws)):
        return FOUND, f"{what} matches {hits[:4]}"
    return NOT_FOUND, f"{what} has nothing matching {kws[:6]}"


class Evidence:
    """Everything the verifier may look at, gathered once and read-only. Any part may be missing (None)."""

    def __init__(self, workspace: Path | None = None, jobs: list | None = None, git_log: str | None = None,
                 library: str | None = None, min_hits: int = 2):
        self.workspace, self.jobs, self.git_log, self.library, self.min_hits = workspace, jobs, git_log, library, min_hits

    def cron(self, text):
        return check_cron(text, self.jobs, self.min_hits)

    def file(self, text):
        return check_file(text, self.workspace, min_hits=self.min_hits)

    def git(self, text):
        return check_text(text, self.git_log, "recent commits", self.min_hits)

    def lib(self, text):
        return check_text(text, self.library, "the library", self.min_hits)


# category -> evidence to try, in order. Empty = no deterministic check exists for that kind of claim.
CATEGORY_CHECKS = {
    "reminder_set": ("cron",), "scheduled": ("cron",),
    "filed": ("lib", "file"),
    "saved": ("file", "git"), "built": ("file", "git"), "did": ("file", "git"),
    "fixed": ("file", "git", "lib"),
    "verified": (), "sent": (), "other": (), "will_do": (),
}


def verify_claim(claim: dict, ev: Evidence) -> tuple[str, str]:
    """(status, detail). verified = evidence found; not_done = looked and found nothing; unverified = could not
    look (no check for this kind of claim, or the evidence source is unavailable); pending = a future promise."""
    cat = claim.get("category", "other")
    if cat == "will_do":
        return "pending", "future commitment: cannot be checked until it is due"
    checks = CATEGORY_CHECKS.get(cat, ())
    if not checks:
        return "unverified", f"no deterministic check for '{cat}' claims"
    text = f"{claim.get('claim', '')} {claim.get('hint', '')}"
    missing = None
    for name in checks:
        status, detail = getattr(ev, name)(text)
        if status == FOUND:
            return "verified", detail
        if status == NOT_FOUND and missing is None:
            missing = detail
    return ("not_done", missing) if missing else ("unverified", "evidence source not available")


def schedule_findings(jobs: list | None, now_ms: int, grace_ms: int = 600_000) -> list[dict]:
    """Problems with enabled scheduled jobs, judged by the schedule watchdog's own evaluate().

    Adds the one thing the watchdog cannot do: check the watchdog job itself.
    """
    if jobs is None:
        return [{"id": "_gateway", "name": "scheduler", "kind": "failed",
                 "detail": "could not read the job list, so no schedule was checked", "sig": "no-job-list"}]
    out = schedule_watch.evaluate(jobs, now_ms, grace_ms)
    for j in jobs:
        if isinstance(j, dict) and j.get("name") == schedule_watch.SELF:
            out += schedule_watch.evaluate([{**j, "name": "schedule watchdog", "displayName": "schedule watchdog"}],
                                           now_ms, grace_ms)
    return out


_LABEL = {"failed": "FAILED", "missed": "DID NOT RUN", "undelivered": "NOT DELIVERED", "skipped": "SKIPPED"}


def render(day: str, open_claims: list[dict], sched: list[dict], followup_path: str = "") -> str:
    """One plain-text report, bounded in items and in length. Empty string when there is nothing to say."""
    if not open_claims and not sched:
        return ""
    lines = [f"Quality check for {day}: {len(open_claims) + len(sched)} item(s) need attention"]
    if open_claims:
        lines.append(f"Claims the evidence does not support ({len(open_claims)}):")
        for c in open_claims[:REPORT_MAX_ITEMS]:
            lines.append(f"- [{c.get('category')}] {str(c.get('claim'))[:140]} :: {str(c.get('detail'))[:120]}")
        if len(open_claims) > REPORT_MAX_ITEMS:
            lines.append(f"...and {len(open_claims) - REPORT_MAX_ITEMS} more")
    if sched:
        lines.append(f"Schedules ({len(sched)}):")
        for f in sched[:REPORT_MAX_ITEMS]:
            lines.append(f"- {_LABEL.get(f['kind'], f['kind'].upper())}: {str(f['name'])[:80]} - {str(f['detail'])[:160]}")
        if len(sched) > REPORT_MAX_ITEMS:
            lines.append(f"...and {len(sched) - REPORT_MAX_ITEMS} more")
    if open_claims and followup_path:
        lines.append(f"Follow-ups: {followup_path} (rechecked hourly; nothing was changed by this check)")
    text = "\n".join(lines)
    return text if len(text) <= REPORT_MAX_CHARS else text[:REPORT_MAX_CHARS - 16].rstrip() + "\n...(truncated)"


def update_followups(path: Path, results: list[dict], now: datetime) -> None:
    """Open a follow-up for each not_done claim; close the ones that now verify. One row per claim id."""
    rows = {r.get("id"): r for r in common.load_jsonl(path)}
    for c in results:
        old = rows.get(c["id"])
        if c["status"] == "not_done":
            if old and old.get("status") == "open":
                old["detail"] = c.get("detail")
            else:
                rows[c["id"]] = {"id": c["id"], "date": c.get("date"), "claim": c.get("claim"),
                                 "category": c.get("category"), "detail": c.get("detail"), "source": c.get("source"),
                                 "status": "open", "created_utc": now.isoformat(), "resolved_utc": None}
        elif c["status"] == "verified" and old and old.get("status") == "open":
            old.update(status="resolved", resolved_utc=now.isoformat(), detail=c.get("detail"))
    if rows:
        common.write_jsonl(path, list(rows.values()))


def run_pass(mode: str, claims: list[dict], prev: dict, ev: Evidence, now: datetime) -> tuple[list[dict], dict]:
    """Verify claims. Returns (claims checked in this pass, new verify state by id)."""
    state, done = dict(prev), []
    for c in claims:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        old = state.get(c["id"], {})
        if mode == "recheck":
            if old.get("status") not in ("unverified", "not_done"):
                continue
            if old.get("recheck_count", 0) >= MAX_RECHECKS:
                continue                         # a day of retries is enough; the follow-up stays open
        status, detail = verify_claim(c, ev)
        rec = {**c, "status": status, "detail": detail, "status_updated_utc": now.isoformat(),
               "recheck_count": old.get("recheck_count", 0) + 1}
        state[c["id"]] = rec
        done.append(rec)
    return done, state


def _capture(run, argv: list[str], timeout: int = 60) -> str | None:
    try:
        r = run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def load_jobs(run, oc: list[str], jobs_file: str | None) -> list | None:
    raw = None
    if jobs_file:
        try:
            raw = Path(jobs_file).read_text(encoding="utf-8")
        except OSError:
            return None
    else:
        raw = _capture(run, oc + ["cron", "list", "--json"])
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return jobs if isinstance(jobs, list) else None


def main(argv: list[str], now: datetime | None = None, run=subprocess.run, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True, help="quality-control state directory")
    ap.add_argument("--workspace", required=True, help="the assistant's workspace (searched, never written)")
    ap.add_argument("--mode", choices=["verify", "recheck"], default="verify")
    ap.add_argument("--date", default=None)
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--to", default=None, help="telegram id for the report; without it the report is printed")
    ap.add_argument("--openclaw", default="openclaw")
    ap.add_argument("--jobs-file", default=None, help="read the job list from this JSON file instead of the CLI")
    ap.add_argument("--library-cmd", default=None, help="read-only command that prints the reference library")
    ap.add_argument("--wake-cmd", default=None, help="optional command; gets the fix-it prompt as its last argument")
    ap.add_argument("--grace-min", type=int, default=10)
    ap.add_argument("--min-keyword-hits", type=int, default=2)   # one shared word is not evidence
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    state, oc = Path(ns.state), shlex.split(ns.openclaw)
    claims_dir = state / "claims"

    if ns.date:
        day = ns.date
    elif ns.mode == "recheck":
        days = sorted(p.stem for p in claims_dir.glob("*.jsonl")) if claims_dir.is_dir() else []
        day = days[-1] if days else common.audit_date(now, ns.tz)
    else:
        day = common.audit_date(now, ns.tz)

    claims = common.load_jsonl(claims_dir / f"{day}.jsonl")
    verify_path = state / "verify" / f"{day}.jsonl"
    prev = {r["id"]: r for r in common.load_jsonl(verify_path) if r.get("id")}
    work = bool(claims) and (ns.mode == "verify"
                             or any(r.get("status") in ("unverified", "not_done") for r in prev.values()))
    jobs = load_jobs(run, oc, ns.jobs_file) if (work or ns.mode == "verify") else None

    done, new_state = [], prev
    if work:
        ws = Path(ns.workspace)
        ev = Evidence(workspace=ws, jobs=jobs, min_hits=ns.min_keyword_hits,
                      git_log=_capture(run, ["git", "-C", str(ws), "log", "--oneline", "-50"], 30),
                      library=_capture(run, shlex.split(ns.library_cmd)) if ns.library_cmd else None)
        done, new_state = run_pass(ns.mode, claims, prev, ev, now)
    open_claims = [c for c in done if c["status"] == "not_done"]
    followup_path = state / "followup" / f"{day}.jsonl"
    if not ns.dry_run and done:
        common.write_jsonl(verify_path, list(new_state.values()))
        update_followups(followup_path, done, now)

    if ns.mode == "recheck":                     # always silent: it only keeps the record current
        if not ns.dry_run:
            common.log(state, f"verifier: mode=recheck day={day} rechecked={len(done)} open={len(open_claims)}", now)
        return 0

    sched = schedule_findings(jobs, int(now.timestamp() * 1000), ns.grace_min * 60_000)
    text = render(day, open_claims, sched, str(followup_path))
    if not ns.dry_run:
        common.log(state, f"verifier: mode=verify day={day} claims={len(claims)} not_done={len(open_claims)} "
                          f"schedule_problems={len(sched)}", now)
    if not text:
        return 0                                 # everything verified: silence is the result
    if ns.dry_run or not ns.to:
        print(text, file=out)
        return 0
    try:
        sent = run(oc + ["message", "send", "--channel", "telegram", "--target", ns.to, "--message", text],
                   capture_output=True, text=True, timeout=120)
        ok = sent.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        common.log(state, "verifier: report could not be delivered", now)
        return 1                                 # a failed job is what the schedule watchdog reports
    if ns.wake_cmd and open_claims:
        prompt = ("QUALITY CONTROL: the nightly check found things you said you did that the evidence does not "
                  "support. For each: investigate, do the work if it was missed, or correct the record if it was "
                  "handled differently. Report what you fixed and how you verified it.\n"
                  + "\n".join(f"- [{c['category']}] {c['claim'][:200]} (evidence: {str(c['detail'])[:150]})"
                              for c in open_claims[:8]))
        try:
            run(shlex.split(ns.wake_cmd) + [prompt], capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.SubprocessError):
            common.log(state, "verifier: wake command failed", now)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
