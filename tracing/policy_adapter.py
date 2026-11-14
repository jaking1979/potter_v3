"""Offline C1-to-C6 experiment; neither durable nor authenticated evidence.

The unkeyed digest is not a confidentiality mechanism. Metadata and hashes can
leak information; do not use with sensitive records until privacy review.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from uuid import UUID, uuid5

from tracing.trace import DecisionTrace

_POLICY_TRACE_NAMESPACE = UUID('b6e8f0b1-1f38-4c12-a33c-a5d402b64fa0')
_DECISION_ID = re.compile(r'^dec-[0-9a-f]{32}$')
_FIELDS = ('decision_id', 'outcome', 'reason_code', 'capability', 'risk', 'action_hash', 'effect_id')


class PolicyTraceAdapter:
    def __init__(self, emit, *, environment='synthetic', clock=None):
        if not callable(emit):
            raise TypeError('emit must be callable')
        if environment not in ('synthetic', 'replay', 'staging', 'production'):
            raise ValueError('invalid environment')
        if clock is not None and not callable(clock):
            raise TypeError('clock must be callable')
        self._emit = emit
        self._environment = environment
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.unlinked_denials = 0

    def __call__(self, record):
        if not isinstance(record, dict):
            raise ValueError('invalid policy record')
        task_id, outcome = record.get('task_id'), record.get('outcome')
        if task_id is None:
            if outcome != 'deny':
                raise ValueError('only denial may be unlinked')
            self.unlinked_denials += 1
            return
        decision_id = record.get('decision_id')
        if not isinstance(decision_id, str) or not _DECISION_ID.fullmatch(decision_id):
            raise ValueError('invalid decision id')
        if any(field not in record for field in _FIELDS):
            raise ValueError('incomplete policy record')
        if any(value is not None and not isinstance(value, str) for value in (record[f] for f in _FIELDS)):
            raise ValueError('invalid policy metadata')
        encoded = json.dumps({f: record[f] for f in _FIELDS}, sort_keys=True,
                             separators=(',', ':'), ensure_ascii=True).encode('ascii')
        time = self._clock()
        if not isinstance(time, datetime):
            raise ValueError('clock must return a datetime')
        trace = DecisionTrace(task_id=task_id,
            trace_id=str(uuid5(_POLICY_TRACE_NAMESPACE, decision_id)),
            component='policy', outcome=outcome,
            decision_digest=sha256(encoded).hexdigest(),
            environment=self._environment, timestamp=time.isoformat(),
            subject_id=decision_id)
        self._emit(trace)
