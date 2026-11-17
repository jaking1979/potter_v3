#!/usr/bin/env python3
"""Evening check-in nudge: ONE recurring job, no model, no daily one-off jobs.

The old system created a new one-off job every morning; 41 of them piled up and the nudge silently died for weeks.
This program runs every 10 minutes on weekday afternoons and evenings and decides, each time, from scratch:
  - already nudged today?            -> do nothing
  - work calendar readable?          -> target = end of today's last busy block + 20 minutes
  - calendar not readable / no blocks -> target = 18:00 (a failed calendar read never kills the nudge)
  - now >= target and before 21:30   -> send the opening question once, record it
Because it re-reads the calendar on every run, a meeting that runs late simply moves the target.

  evening_nudge.py --to <telegram id> --state FILE [--tz America/New_York] [--calendar ID] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

OPENING = "Evening check-in: how did the day actually go?"
DEFAULT_AT, CUTOFF, AFTER_LAST = dtime(18, 0), dtime(21, 30), timedelta(minutes=20)


def last_busy_end(doc, calendar: str, tz: ZoneInfo, day) -> datetime | None | bool:
    """From a Composio free/busy reply: end of the last busy block on `day`. None = no blocks. False = unusable."""
    try:
        if not doc.get("successful", False):
            return False
        entry = doc["data"]["calendars"][calendar]
        if entry.get("errors"):
            return False
        ends = []
        for b in entry.get("busy", []):
            try:
                e = datetime.fromisoformat(str(b["end"]).replace("Z", "+00:00")).astimezone(tz)
            except (KeyError, ValueError):
                continue
            if e.date() == day:
                ends.append(e)
        return max(ends) if ends else None
    except (AttributeError, KeyError, TypeError):
        return False


def target_time(now: datetime, busy_end) -> datetime:
    if isinstance(busy_end, datetime):
        return busy_end + AFTER_LAST
    return now.replace(hour=DEFAULT_AT.hour, minute=DEFAULT_AT.minute, second=0, microsecond=0)


def decide(now: datetime, busy_end, state: dict) -> str:
    """'send', 'wait', 'done' (already sent today), 'skip' (weekend or too late)."""
    today = now.date().isoformat()
    if state.get("date") == today and state.get("sent"):
        return "done"
    if now.weekday() >= 5 or now.time() >= CUTOFF:
        return "skip"
    return "send" if now >= target_time(now, busy_end) else "wait"


def read_busy(calendar: str, tz_name: str, now: datetime, composio: str | None):
    if not calendar or not composio:
        return False
    day = now.date()
    args = json.dumps({"items": [calendar], "time_min": f"{day}T00:00:00",
                       "time_max": f"{day + timedelta(days=1)}T00:00:00", "timezone": tz_name})
    try:
        r = subprocess.run([composio, "execute", "GOOGLECALENDAR_FIND_FREE_SLOTS", "-d", args],
                           capture_output=True, text=True, timeout=60)
        return last_busy_end(json.loads(r.stdout), calendar, ZoneInfo(tz_name), day) if r.returncode == 0 else False
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--calendar", default="")
    ap.add_argument("--openclaw", default="openclaw")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)
    now = datetime.now(ZoneInfo(ns.tz))
    path = Path(ns.state)
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    if decide(now, None, state) in ("done", "skip"):          # cheap exits before touching the calendar
        return 0
    busy_end = read_busy(ns.calendar, ns.tz, now, shutil.which("composio"))
    verdict = decide(now, busy_end, state)
    if ns.dry_run:
        print(verdict, target_time(now, busy_end).isoformat())
        return 0
    if verdict != "send":
        return 0
    r = subprocess.run(ns.openclaw.split() + ["message", "send", "--channel", "telegram", "--target", ns.to,
                                              "--message", OPENING], capture_output=True)
    if r.returncode != 0:
        return 1                                              # not recorded: the next run tries again
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": now.date().isoformat(), "sent": True, "sent_at": now.isoformat(),
                                "calendar_used": isinstance(busy_end, datetime)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
