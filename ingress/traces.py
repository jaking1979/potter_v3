"""C6 trace per ingress decision (redaction-safe).

Component ``task``; outcome ``accepted`` (event produced, trusted or not) or
``blocked`` (rejected, or duplicate re-delivery that produced nothing new).
IDs are UUIDv5 under the E3 namespace. The digest is sha256 of a fixed
descriptor: channel, decision, reason code, trust class and the content
digest. No message text, sender reference, header or parameter is included.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Callable

from tracing.emitter import emit
from tracing.trace import DecisionTrace

E3_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:potter:v3:e3:ingress")

Sink = Callable[[str], None]


def ingress_uuid(value: str) -> str:
    return str(uuid.uuid5(E3_NAMESPACE, value))


def emit_ingress_trace(sink: Sink | None, *, event_id: str, delivery_key: str, channel: str,
                       accepted: bool, reason: str, trust: str, content_digest: str,
                       now: int, environment: str = "synthetic") -> None:
    if sink is None:
        return
    descriptor = f"e3.ingress|{channel}|{'accepted' if accepted else 'blocked'}|{reason}|" \
                 f"{trust}|{content_digest}"
    rec = DecisionTrace(
        task_id=event_id, trace_id=ingress_uuid(delivery_key), component="task",
        outcome="accepted" if accepted else "blocked",
        decision_digest=hashlib.sha256(descriptor.encode()).hexdigest(),
        environment=environment,
        timestamp=datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        subject_id=event_id)
    emit(rec, sink)
