"""C6 trace emission for issue / decide / send (redaction-safe).

C6 ``DecisionTrace`` accepts components ``policy|task|effect``. Mapping:

* approval issued (prompt raised)  -> component ``task``,   outcome ``accepted``
* approval decided                 -> component ``task``,   outcome ``accepted``
                                      (approve) or ``blocked`` (deny/refused)
* notification send result         -> component ``effect``, outcome
                                      ``completed`` / ``failed`` / ``unknown``

IDs are UUIDv5 of E1's own ids; the digest is sha256 of a fixed descriptor
(event, ids, action hash). No message text, parameters or sender refs.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Callable

from tracing.emitter import emit
from tracing.trace import DecisionTrace

E1_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:potter:v3:e1:notifier")

Sink = Callable[[str], None]


def _uuid(value: str) -> str:
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except (ValueError, AttributeError, TypeError):
        pass
    return str(uuid.uuid5(E1_NAMESPACE, value))


def _ts(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit_trace(sink: Sink | None, *, task_key: str, trace_key: str, component: str,
               outcome: str, descriptor: str, subject_key: str, now: float,
               environment: str = "synthetic") -> None:
    if sink is None:
        return
    rec = DecisionTrace(task_id=_uuid(task_key), trace_id=_uuid(trace_key),
                        component=component, outcome=outcome,
                        decision_digest=hashlib.sha256(descriptor.encode()).hexdigest(),
                        environment=environment, timestamp=_ts(now),
                        subject_id=_uuid(subject_key))
    emit(rec, sink)
