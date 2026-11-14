"""Offline C1 policy-record shape adapter; not an authentication boundary.

The caller must obtain task context from a trusted execution boundary.
No confidentiality, effect atomicity, crash recovery, or production use is
provided by this experimental mapping.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid5

from .policy_binding import SyntheticPolicyAudit

_TRACE_NAMESPACE = UUID("78deff20-580c-4935-bfad-71d7f467d47c")


class C1ShapeAdapter:
    """Map a C1-shaped record to SyntheticPolicyAudit under explicit context.

    A stable per-environment decision trace ID allows time-only retries to be
    recognised by a suitably configured sink. Task attribution remains a
    caller assertion until independently authenticated.
    """
    def __init__(self, audit, *, environment, clock=None):
        if not isinstance(audit, SyntheticPolicyAudit):
            raise TypeError("offline audit required")
        if environment not in ("synthetic", "replay") or audit._environment != environment:
            raise ValueError("audit environment mismatch")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._audit = audit
        self._environment = environment
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, record):
        if not isinstance(record, dict):
            raise ValueError("policy record required")
        decision = record.get("decision_id")
        outcome = record.get("outcome")
        task = record.get("task_id")
        if not isinstance(decision, str) or not decision:
            raise ValueError("decision identity required")
        if task is None and outcome != "deny":
            raise ValueError("only deny may be unlinked")
        # SyntheticPolicyAudit validates task IDs, decision IDs and outcome;
        # fields not in its current schema are deliberately not persisted.
        moment = self._clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise ValueError("aware clock required")
        trace_id = str(uuid5(_TRACE_NAMESPACE, self._environment + ":" + decision))
        event = {"task_id": task, "correlation": "missing" if task is None else "request",
                 "decision_id": decision, "outcome": outcome}
        with self._audit.bind(task_id=task, trace_id=trace_id, timestamp=moment.isoformat()):
            return self._audit.hook(event)
