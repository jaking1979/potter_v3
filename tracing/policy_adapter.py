"""Offline C1 event adapter. Caller must provide trusted per-request context."""
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
import re

from .emitter import emit
from .trace import DecisionTrace

_DECISION = re.compile(r'^dec-[0-9a-f]{32}$')


def emit_uncorrelated_denial(event, *, environment, timestamp, sink):
    """Record a malformed-request denial without inventing a task identity.

    This separate audit format is not a task-linked DecisionTrace. Callers
    must account for it explicitly; sink errors propagate.
    """
    if not isinstance(event, Mapping) or event.get('correlation') != 'missing' or event.get('task_id') is not None:
        raise ValueError('expected missing correlation')
    decision_id = event.get('decision_id')
    if not isinstance(decision_id, str) or not _DECISION.fullmatch(decision_id) or event.get('outcome') != 'deny':
        raise ValueError('expected bounded denial')
    if environment not in ('synthetic', 'replay', 'staging', 'production'):
        raise ValueError('invalid environment')
    try:
        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if dt.tzinfo is None or dt.utcoffset() != timezone.utc.utcoffset(dt):
            raise ValueError()
    except (ValueError, AttributeError) as exc:
        raise ValueError('timestamp must be UTC ISO 8601') from exc
    sink(json.dumps({'schema': 'potter.trace.uncorrelated-denial/v0',
                     'decision.id': decision_id, 'potter.environment': environment,
                     'time': timestamp}, sort_keys=True, separators=(',', ':')) + '\n')


def emit_policy_event(event, *, task_id, trace_id, environment, timestamp, sink):
    """Emit a bounded C1 decision; do not copy capabilities, hashes or raw data.

    task_id and trace_id must come from the active task context, not the event.
    Context binding, trusted provenance, storage and full instrumentation remain
    integration responsibilities. Exceptions propagate to the policy caller.
    """
    if not isinstance(event, Mapping):
        raise ValueError('expected policy event mapping')
    if event.get('correlation') != 'request' or event.get('task_id') != task_id:
        raise ValueError('missing or mismatched task correlation')
    decision_id = event.get('decision_id')
    outcome = event.get('outcome')
    if outcome not in ('allow', 'deny', 'reconcile'):
        raise ValueError('unsupported policy outcome')
    # Hash only the validated identifier and outcome; no caller text is included.
    # Validate before hashing so arbitrary input never becomes an opaque payload.
    probe = DecisionTrace(task_id=task_id, trace_id=trace_id, component='policy',
                          outcome=outcome, decision_digest='0' * 64,
                          environment=environment, timestamp=timestamp,
                          subject_id=decision_id)
    if decision_id is None:
        raise ValueError('decision ID required')
    digest = hashlib.sha256(json.dumps([probe.subject_id, outcome],
                            separators=(',', ':')).encode('utf-8')).hexdigest()
    emit(DecisionTrace(task_id=task_id, trace_id=trace_id, component='policy',
                       outcome=outcome, decision_digest=digest,
                       environment=environment, timestamp=timestamp,
                       subject_id=decision_id), sink)
