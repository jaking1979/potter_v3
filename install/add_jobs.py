#!/usr/bin/env python3
"""Create the jobs in install/jobs.json with OpenClaw's own command, and record standby ones for handover.

  add_jobs.py --jobs install/jobs.json --openclaw "openclaw --profile x" --set POTTER_HOME=... --set WS=... \
              --set TZ=... --set TO=... --set KG_DB=... --set SESSIONS=... --handover /etc/potter/handover.json
Prints one line per job: name, on|standby, ok|FAILED. Exit 1 if any job could not be created.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

NAME = re.compile(r"^potter-[a-z0-9-]+$")
CRON = re.compile(r"^[\d*/,\-]+( [\d*/,\-]+){4}$")


def fill(text: str, values: dict) -> str:
    out = text
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    left = re.findall(r"\{[A-Z_]+\}", out)
    if left:
        raise ValueError(f"unfilled placeholder {left[0]}")
    return out


def argv_for(job: dict, values: dict, agent: str = "potter") -> list[str]:
    if not NAME.match(str(job.get("name", ""))) or not CRON.match(str(job.get("cron", ""))):
        raise ValueError(f"bad job definition: {job.get('name')!r}")
    timeout = int(job.get("timeout", 0))
    if timeout <= 0:
        raise ValueError(f"{job['name']}: every job needs a timeout")
    a = ["cron", "add", "--name", job["name"], "--declaration-key", job["name"], "--cron", job["cron"],
         "--tz", fill(job.get("tz", "{TZ}"), values), "--exact", "--timeout-seconds", str(timeout)]
    if job["kind"] == "command":
        a += ["--command", fill(job["command"], values), "--no-deliver"]
    elif job["kind"] == "agent":
        a += ["--agent", agent, "--session", "isolated", "--message", fill(job["message"], values)]
        a += (["--channel", "telegram", "--to", values["TO"], "--announce"] if job.get("deliver") else ["--no-deliver"])
    else:
        raise ValueError(f"{job['name']}: unknown kind")
    if job.get("standby"):
        a.append("--disabled")
    return a


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", required=True)
    ap.add_argument("--openclaw", default="openclaw")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--handover")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)
    values = dict(s.split("=", 1) for s in ns.set)
    jobs = json.loads(Path(ns.jobs).read_text())["jobs"]
    failed, standby = 0, []
    for job in jobs:
        a = shlex.split(ns.openclaw) + argv_for(job, values)
        if ns.dry_run:
            print(" ".join(shlex.quote(x) for x in a))
            continue
        r = subprocess.run(a, capture_output=True, text=True)
        ok = r.returncode == 0
        failed += not ok
        print(f"{job['name']}: {'standby' if job.get('standby') else 'on'}, {'ok' if ok else 'FAILED'}")
        if job.get("standby"):
            standby.append({"name": job["name"], "enabled": False, "purpose": job.get("purpose", "")})
    if ns.handover and standby:
        p = Path(ns.handover)
        reg = json.loads(p.read_text()) if p.exists() else {"crons": [], "apps": [], "secrets_needed": []}
        have = {c.get("name") for c in reg.setdefault("crons", [])}
        reg["crons"] += [s for s in standby if s["name"] not in have]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(reg, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
