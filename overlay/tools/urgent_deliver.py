#!/usr/bin/env python3
"""Deliver urgent mail items to the owner. Plain program, no model: the trusted side of the mail quarantine.

The mail-intake turn reads untrusted email and has no way to message anyone. When it judges something urgent it
appends one JSON line to the queue. This program, run every few minutes by a command job, formats those records
from a fixed template (email-derived text is cleaned and length-capped), sends ONE Telegram message, and marks
them delivered only after the send succeeds. A hostile email can at most cause a short notification.

Queue record: {"id","ts","severity":"urgent"|"critical","sender","summary","deadline"?,"source"?}
  urgent   -> held during quiet hours, delivered after they end
  critical -> delivered at any hour

  urgent_deliver.py --to <telegram id> --queue FILE [--tz America/New_York] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

QUIET_START, QUIET_END = 23, 8
SUMMARY_MAX, SENDER_MAX, MAX_ITEMS = 240, 80, 10
KEEP_DELIVERED = timedelta(days=3)
OPS = re.compile(r"verification code|recovery email|app password|2-step verification|security alert|"
                 r"sign-in attempt|confirm your (email|account)|one-time (code|password)", re.I)


def clean(value, n: int) -> str:
    if not isinstance(value, str):
        return ""
    s = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", value)).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def quiet(now: datetime) -> bool:
    return now.hour >= QUIET_START or now.hour < QUIET_END


def operational(rec: dict) -> bool:
    """Potter's own account plumbing (codes, security notices) is never surfaced to the owner."""
    return bool(OPS.search(str(rec.get("summary", ""))))


def load(path: Path) -> list[dict]:
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue                       # a malformed line never stops delivery of the rest
        if isinstance(r, dict):
            out.append(r)
    return out


def select(records: list[dict], now: datetime) -> list[dict]:
    q = quiet(now)
    picked = []
    for r in records:
        if r.get("delivered") or r.get("suppressed"):
            continue
        if operational(r):
            r["suppressed"] = True
            continue
        if q and r.get("severity") != "critical":
            continue
        picked.append(r)
    return picked[:MAX_ITEMS]


def render(items: list[dict]) -> str:
    head = "Urgent from your inbox" if len(items) == 1 else f"{len(items)} urgent from your inbox"
    lines = [head]
    for r in items:
        mark = "[CRITICAL]" if r.get("severity") == "critical" else "[urgent]"
        lines.append(f"{mark} {clean(r.get('summary'), SUMMARY_MAX) or '(no summary)'}")
        meta = ["from " + (clean(r.get("sender"), SENDER_MAX) or "unknown sender")]
        if clean(r.get("deadline"), 80):
            meta.append("by " + clean(r.get("deadline"), 80))
        lines.append("   " + " - ".join(meta))
    return "\n".join(lines)


def save(path: Path, records: list[dict], now_utc: datetime) -> None:
    keep = []
    for r in records:
        if r.get("delivered") or r.get("suppressed"):
            try:
                t = datetime.fromisoformat(str(r.get("delivered_at") or r.get("ts") or "").replace("Z", "+00:00"))
            except ValueError:
                t = now_utc
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if now_utc - t > KEEP_DELIVERED:
                continue
        keep.append(r)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in keep))
    os.replace(tmp, path)


def run(queue: Path, now: datetime, send) -> int:
    """Returns the number of items delivered. `send(text) -> bool`."""
    records = load(queue)
    if not records:
        return 0
    items = select(records, now)
    delivered = 0
    if items and send(render(items)):
        stamp = now.astimezone(timezone.utc).isoformat()
        for r in items:
            r["delivered"], r["delivered_at"] = True, stamp
        delivered = len(items)
    save(queue, records, now.astimezone(timezone.utc))
    return delivered


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--queue", required=True)
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--openclaw", default="openclaw")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)
    oc = ns.openclaw.split()

    def send(text: str) -> bool:
        if ns.dry_run:
            print(text)
            return False
        return subprocess.run(oc + ["message", "send", "--channel", "telegram", "--target", ns.to, "--message", text],
                              capture_output=True).returncode == 0

    failed = {"v": False}

    def tracked(text: str) -> bool:
        ok = send(text)
        failed["v"] = not ok and not ns.dry_run
        return ok

    run(Path(ns.queue), datetime.now(ZoneInfo(ns.tz)), tracked)
    return 1 if failed["v"] else 0          # a failed send is a failed job, so the watchdog reports it


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
