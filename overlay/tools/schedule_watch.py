#!/usr/bin/env python3
"""Schedule watchdog: tells the owner when a scheduled job failed, did not run when due, or could not deliver.

v2's biggest reliability problem was jobs that silently stopped running. This is a plain program (no model), run
every 15 minutes by a command job. It reads OpenClaw's own job list, compares each job's state with what should
have happened, and sends ONE Telegram message per new problem. No problem, no message.

  schedule_watch.py --to <telegram id> [--state FILE] [--grace-min 10] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SELF = "potter-schedule-watch"
BAD = {"error", "timeout", "timed_out", "failed", "cancelled"}


def evaluate(jobs: list, now_ms: int, grace_ms: int = 600_000) -> list[dict]:
    """Return one finding per enabled job that has a problem. Each: {id, name, kind, detail, sig}."""
    out = []
    for j in jobs if isinstance(jobs, list) else []:
        if not isinstance(j, dict) or not j.get("enabled") or j.get("name") == SELF:
            continue
        st = j.get("state") or {}
        name = str(j.get("displayName") or j.get("name") or j.get("id"))
        jid = str(j.get("id") or name)
        last = st.get("lastRunAtMs")
        status = st.get("lastStatus") or st.get("lastRunStatus")
        nxt = st.get("nextRunAtMs") or j.get("nextRunAtMs")
        if status in BAD:
            n = st.get("consecutiveErrors") or 1
            detail = str(st.get("lastError") or st.get("lastDiagnosticSummary") or status)[:200]
            out.append({"id": jid, "name": name, "kind": "failed",
                        "detail": f"{detail}" + (f" ({n} times in a row)" if n > 1 else ""),
                        "sig": f"failed:{last}"})
        elif isinstance(nxt, (int, float)) and nxt + grace_ms < now_ms:
            mins = int((now_ms - nxt) // 60000)
            out.append({"id": jid, "name": name, "kind": "missed",
                        "detail": f"was due {mins} minutes ago and has not run", "sig": f"missed:{int(nxt)}"})
        elif st.get("lastDeliveryStatus") in ("failed", "error"):
            out.append({"id": jid, "name": name, "kind": "undelivered",
                        "detail": "ran, but its message could not be delivered", "sig": f"undelivered:{last}"})
        elif (st.get("consecutiveSkipped") or 0) >= 3:
            out.append({"id": jid, "name": name, "kind": "skipped",
                        "detail": f"skipped {st['consecutiveSkipped']} times in a row", "sig": f"skipped:{last}"})
    return out


def new_findings(findings: list[dict], seen: dict) -> tuple[list[dict], dict]:
    """Drop findings already reported (same job, same signature). Returns (to_report, next_seen)."""
    nxt = {f["id"]: f["sig"] for f in findings}          # recovered jobs fall out, so a later failure alerts again
    return [f for f in findings if seen.get(f["id"]) != f["sig"]], nxt


def render(findings: list[dict]) -> str:
    label = {"failed": "FAILED", "missed": "DID NOT RUN", "undelivered": "NOT DELIVERED", "skipped": "SKIPPED"}
    lines = [f"Schedule check: {len(findings)} job(s) need attention"]
    lines += [f"- {label[f['kind']]}: {f['name']} - {f['detail']}" for f in findings[:15]]
    if len(findings) > 15:
        lines.append(f"...and {len(findings) - 15} more")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--state", default="/etc/potter/schedule-watch.json")
    ap.add_argument("--grace-min", type=int, default=10)
    ap.add_argument("--openclaw", default="openclaw")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)
    oc = ns.openclaw.split()
    r = subprocess.run(oc + ["cron", "list", "--json"], capture_output=True, text=True)
    state = Path(ns.state)
    try:
        seen = json.loads(state.read_text())
    except (OSError, ValueError):
        seen = {}
    if r.returncode != 0:
        # The scheduler itself is unreachable: that is the worst case, say so once.
        findings = [{"id": "_gateway", "name": "OpenClaw scheduler", "kind": "failed",
                     "detail": "could not read the job list (gateway down?)", "sig": "gateway-down"}]
    else:
        try:
            data = json.loads(r.stdout)
        except ValueError:
            data = {}
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        findings = evaluate(jobs, int(time.time() * 1000), ns.grace_min * 60_000)
    report, nxt = new_findings(findings, seen)
    if report:
        text = render(report)
        if ns.dry_run:
            print(text)
        else:
            s = subprocess.run(oc + ["message", "send", "--channel", "telegram", "--target", ns.to, "--message", text],
                               capture_output=True, text=True)
            if s.returncode != 0:
                return 1                     # keep old state so the next run tries again
    if not ns.dry_run:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(nxt))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
