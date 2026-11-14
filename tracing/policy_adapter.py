"""Offline C1->C6 adapter; in-memory counters are not durable audit evidence."""
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4
from tracing.trace import DecisionTrace

class PolicyTraceAdapter:
    def __init__(self, emit, *, environment='synthetic'):
        if not callable(emit):
            raise TypeError('emit must be callable')
        self._emit = emit
        self._environment = environment
        self.unlinked_denials = 0

    def __call__(self, record):
        if not isinstance(record, dict):
            raise ValueError('invalid policy record')
        task_id, outcome, decision_id = record.get('task_id'), record.get('outcome'), record.get('decision_id')
        if task_id is None:
            if outcome != 'deny':
                raise ValueError('only denial may be unlinked')
            self.unlinked_denials += 1
            return
        # Digest only opaque decision ID, never raw request or personal content.
        if not isinstance(decision_id, str):
            raise ValueError('missing decision id')
        trace = DecisionTrace(task_id=task_id, trace_id=str(uuid4()), component='policy',
            outcome=outcome, decision_digest=sha256(decision_id.encode('ascii')).hexdigest(),
            environment=self._environment, timestamp=datetime.now(timezone.utc).isoformat(),
            subject_id=decision_id)
        self._emit(trace)
