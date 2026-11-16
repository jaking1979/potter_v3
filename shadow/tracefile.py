"""Durable, append-only policy trace for the shadow runtime.

One JSON line per policy decision (trace schema v1: decision id, outcome,
digest, and the action hash / capability / reason join fields), written with
fsync before the sink returns. If the file cannot be written the sink raises;
PolicyEngine then returns ``trace_error`` and the turn is denied, so a trace
outage fails closed. Rows carry no message text, parameters or secrets.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

from tracing.policy_adapter import emit_policy_audit

_LOCK = threading.Lock()


class JsonlTraceSink:
    def __init__(self, path, *, environment: str = "staging", clock=None):
        self._path = os.fspath(path)
        self._environment = environment
        self._clock = clock or (lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def _append(self, line: str) -> None:
        with _LOCK:
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)

    def __call__(self, record) -> None:
        """PolicyEngine ``trace_hook``: one decision record in, one durable row out."""
        task_id = record.get("task_id")
        # trace.id is derived from the decision id so a row is self-describing and stable.
        trace_id = _trace_id(record["decision_id"])
        emit_policy_audit(record, task_id=task_id, trace_id=trace_id, environment=self._environment,
                          timestamp=self._clock(), sink=self._append)


def _trace_id(decision_id: str) -> str:
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "potter.shadow.trace/" + decision_id))
