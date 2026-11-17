#!/usr/bin/env python3
"""Switch a scheduled job on or off by its NAME. OpenClaw's own enable/disable take the job's id, which nobody
remembers; this looks the id up from the full job list (disabled jobs included) and then calls the real command.

  cron_switch.py on|off NAME [NAME ...] [--openclaw "openclaw --profile x"]

Prints one line per job: "<name>: on", "<name>: off", or "<name>: not found" / "<name>: FAILED <reason>".
Exit 0 only when every named job ended in the requested state.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys


def jobs(oc: list[str], run=subprocess.run) -> list[dict]:
    r = run(oc + ["cron", "list", "--all", "--json"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("could not read the job list")
    d = json.loads(r.stdout)
    out = d.get("jobs", []) if isinstance(d, dict) else d
    return [j for j in out if isinstance(j, dict)]


def main(argv: list[str], run=subprocess.run, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("state", choices=("on", "off"))
    ap.add_argument("names", nargs="+")
    ap.add_argument("--openclaw", default="openclaw")
    ns = ap.parse_args(argv)
    oc = shlex.split(ns.openclaw)
    try:
        listed = jobs(oc, run)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
        print(f"cron_switch: {e}", file=out)
        return 1
    ok = True
    for name in ns.names:
        hits = [j for j in listed if j.get("name") == name or j.get("declarationKey") == name]
        if len(hits) != 1:
            print(f"{name}: {'not found' if not hits else 'ambiguous (several jobs have this name)'}", file=out)
            ok = False
            continue
        r = run(oc + ["cron", "enable" if ns.state == "on" else "disable", str(hits[0]["id"])],
                capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            print(f"{name}: {ns.state}", file=out)
        else:
            ok = False
            print(f"{name}: FAILED {(r.stderr or r.stdout).strip().splitlines()[-1:] or ['']}", file=out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
