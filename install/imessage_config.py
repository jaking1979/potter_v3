#!/usr/bin/env python3
"""The iMessage channel config for a Gateway on Linux and Messages on a Mac reached over SSH.

Safe defaults: only the owner's own handles can talk to Potter, in DMs and in groups; in a group Potter answers
only when named. Anyone else (for example a child in a family group) can read what Potter sends but cannot
direct it. Messages Potter sends to anyone but the owner go through potter-core's approval.

  imessage_config.py --mac-host H --mac-user U --owner-handles "+15555550100,me@example.com" --cli-path P --out FILE
"""
import argparse
import json
import re
import sys

HANDLE = re.compile(r"^(\+[1-9]\d{7,14}|[^@\s]+@[^@\s]+\.[a-z]{2,})$", re.I)


def build(mac_host: str, mac_user: str, handles: list[str], cli_path: str) -> dict:
    bad = [h for h in handles if not HANDLE.match(h)]
    if bad or not handles:
        raise ValueError(f"owner handles must be +E.164 phone numbers or email addresses: {bad or 'none given'}")
    if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", mac_user):
        raise ValueError("mac user must be a short macOS account name")
    return {"channels": {"imessage": {
        "enabled": True, "cliPath": cli_path, "remoteHost": f"{mac_user}@{mac_host}",
        "dbPath": f"/Users/{mac_user}/Library/Messages/chat.db", "includeAttachments": False,
        "dmPolicy": "allowlist", "allowFrom": handles,
        "groupPolicy": "allowlist", "groupAllowFrom": handles, "groups": {"*": {"requireMention": True}},
    }}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    for a in ("--mac-host", "--mac-user", "--owner-handles", "--cli-path", "--out"):
        ap.add_argument(a, required=True)
    ns = ap.parse_args(argv)
    try:
        cfg = build(ns.mac_host, ns.mac_user, [h.strip() for h in ns.owner_handles.split(",") if h.strip()], ns.cli_path)
    except ValueError as e:
        print(f"imessage: {e}", file=sys.stderr)
        return 2
    json.dump(cfg, open(ns.out, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
