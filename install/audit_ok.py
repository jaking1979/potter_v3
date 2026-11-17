#!/usr/bin/env python3
"""Read `openclaw security audit` on stdin. Exit 0 when there are no critical findings, or when the only one is the
Tailscale Funnel exposure and the owner chose Funnel (/etc/potter/remote-access says "funnel"). Prints what failed."""
import re
import sys
from pathlib import Path

CHOICE = Path("/etc/potter/remote-access")


def critical_ids(text: str) -> list[str]:
    out, section = [], None
    for line in text.splitlines():
        head = line.strip()
        if head in ("CRITICAL", "WARN", "WARNING", "INFO"):
            section = head
            continue
        m = re.match(r"^([a-z][a-z0-9_]*\.[a-z0-9_.]+)\s", line)
        if section == "CRITICAL" and m:
            out.append(m.group(1))
    return out


def ok(text: str, chosen: str) -> tuple[bool, list[str]]:
    m = re.search(r"Summary:\s*(\d+)\s*critical", text)
    if m and m.group(1) == "0":
        return True, []
    ids = critical_ids(text)
    rest = [i for i in ids if not (chosen == "funnel" and i == "gateway.tailscale_funnel")]
    return (bool(ids) and not rest), rest or ids or ["audit did not run"]


if __name__ == "__main__":
    chosen = CHOICE.read_text().strip() if CHOICE.exists() else "serve"
    good, bad = ok(sys.stdin.read(), chosen)
    if not good:
        print("critical: " + ", ".join(bad))
    sys.exit(0 if good else 1)
