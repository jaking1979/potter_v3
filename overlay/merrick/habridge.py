"""Home Assistant through a bridge command, for scheduled command jobs (which have no MCP tools).

This package contains no network client at all (the repo's lint forbids one). The real transport is a small
command supplied at install time; this class only speaks a JSON contract to it:

  stdin  {"op": "get_state", "entity_id": "climate.x"}                     stdout {"ok": true, "state": {...} | null}
  stdin  {"op": "call_service", "domain": "...", "service": "...", "data": {...}}   {"ok": true}
  stdin  {"op": "history", "entity_id": "sensor.x", "start": "<ISO time>"}  {"ok": true, "rows": [...]}

Any failure is a non-zero exit or {"ok": false, "error": "..."}. The Home Assistant address and token are the
bridge's business: they never pass through this code, so they cannot leak into Merrick's logs or replies.
`run` is injected so tests start no process.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from typing import Callable, Optional


class BridgeError(RuntimeError):
    pass


class BridgeHomeAssistant:
    def __init__(self, command: str, timeout: float = 20.0, run: Optional[Callable] = None):
        self.argv = shlex.split(command or "")
        if not self.argv:
            raise ValueError("no Home Assistant bridge command configured")
        self.timeout, self._run = timeout, run or subprocess.run

    def _ask(self, request: dict) -> dict:
        try:
            r = self._run(self.argv, input=json.dumps(request), capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as e:
            raise BridgeError(f"bridge did not run ({type(e).__name__})") from None
        if r.returncode != 0:
            raise BridgeError(f"bridge exited {r.returncode}")
        try:
            answer = json.loads(r.stdout)
        except ValueError:
            raise BridgeError("bridge did not return JSON") from None
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            raise BridgeError("bridge reported a failure")       # the bridge's own error text is not repeated
        return answer

    def get_state(self, entity_id: str) -> Optional[dict]:
        st = self._ask({"op": "get_state", "entity_id": entity_id}).get("state")
        return st if isinstance(st, dict) else None

    def call_service(self, domain: str, service: str, data: dict) -> None:
        self._ask({"op": "call_service", "domain": domain, "service": service, "data": data})

    def history(self, entity_id: str, start_iso: str) -> list:
        rows = self._ask({"op": "history", "entity_id": entity_id, "start": start_iso}).get("rows")
        return rows if isinstance(rows, list) else []
