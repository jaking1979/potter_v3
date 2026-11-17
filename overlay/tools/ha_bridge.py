#!/usr/bin/env python3
"""Home Assistant bridge for Merrick's scheduled jobs (which have no MCP tools).

Speaks Merrick's JSON contract on stdin/stdout and Home Assistant's REST API on the other side. The address and
token are read from a root-only file and never printed, logged or returned: on any failure the answer is
{"ok": false, "error": "<short code>"} and a non-zero exit.

  echo '{"op":"get_state","entity_id":"climate.x"}' | ha_bridge.py --env /etc/potter/secrets/ha.env
  ops: get_state {entity_id} | call_service {domain, service, data} | history {entity_id, start}

Merrick's own safety layer (allowlist, clamps, read-back) sits above this; the bridge adds only shape checks.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
NAME = re.compile(r"^[a-z_]+$")
TIMEOUT_S, MAX_BYTES = 15, 2_000_000


class Fail(Exception):
    pass


def load_env(path: str) -> tuple[str, str]:
    url = token = ""
    try:
        for line in Path(path).read_text().splitlines():
            k, _, v = line.strip().partition("=")
            if k == "HA_URL":
                url = v.strip().strip('"').rstrip("/")
            elif k == "HA_TOKEN":
                token = v.strip().strip('"')
    except OSError:
        raise Fail("not_configured")
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d{2,5})?", url) or not token:
        raise Fail("not_configured")
    return url, token


def request(url: str, token: str, method: str, path: str, body=None, opener=urllib.request.urlopen):
    req = urllib.request.Request(url + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with opener(req, timeout=TIMEOUT_S) as r:
            raw = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise Fail("unauthorized" if e.code in (401, 403) else f"http_{e.code}")
    except Exception:                                        # noqa: BLE001 - never surface transport detail
        raise Fail("unreachable")
    if len(raw) > MAX_BYTES:
        raise Fail("too_large")
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise Fail("bad_reply")


def handle(op: dict, url: str, token: str, opener=urllib.request.urlopen) -> dict:
    kind = op.get("op") if isinstance(op, dict) else None
    if kind == "get_state":
        eid = str(op.get("entity_id", ""))
        if not ENTITY.match(eid):
            raise Fail("bad_request")
        st = request(url, token, "GET", "/api/states/" + eid, opener=opener)
        if st is None:
            return {"ok": True, "state": None}
        return {"ok": True, "state": {"state": st.get("state"), "attributes": st.get("attributes") or {},
                                       "last_updated": st.get("last_updated")}}
    if kind == "call_service":
        domain, service, data = str(op.get("domain", "")), str(op.get("service", "")), op.get("data")
        if not NAME.match(domain) or not NAME.match(service) or not isinstance(data, dict) \
                or not ENTITY.match(str(data.get("entity_id", ""))):
            raise Fail("bad_request")
        if request(url, token, "POST", f"/api/services/{domain}/{service}", data, opener=opener) is None:
            raise Fail("no_such_service")
        return {"ok": True}
    if kind == "history":
        eid, start = str(op.get("entity_id", "")), str(op.get("start", ""))
        if not ENTITY.match(eid) or not re.fullmatch(r"[0-9T:\-+.Z]{10,35}", start):
            raise Fail("bad_request")
        q = urllib.parse.urlencode({"filter_entity_id": eid, "minimal_response": ""})
        out = request(url, token, "GET", f"/api/history/period/{urllib.parse.quote(start)}?{q}", opener=opener)
        rows = out[0] if isinstance(out, list) and out and isinstance(out[0], list) else []
        return {"ok": True, "rows": [{"state": r.get("state"), "last_changed": r.get("last_changed")}
                                     for r in rows if isinstance(r, dict)]}
    raise Fail("bad_request")


def main(argv: list[str], stdin=sys.stdin, stdout=sys.stdout, opener=urllib.request.urlopen) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="/etc/potter/secrets/ha.env")
    ns = ap.parse_args(argv)
    try:
        op = json.loads(stdin.read())
        url, token = load_env(ns.env)
        json.dump(handle(op, url, token, opener), stdout)
        return 0
    except Fail as e:
        json.dump({"ok": False, "error": str(e)}, stdout)
        return 1
    except ValueError:
        json.dump({"ok": False, "error": "bad_request"}, stdout)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
