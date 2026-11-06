"""Offline trace record primitives; no persistence or runtime wiring."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from uuid import UUID

_HASH = re.compile(r'^[0-9a-f]{64}$')
_COMPONENTS = frozenset({'policy', 'task', 'effect'})
_OUTCOMES = frozenset({'allow', 'deny', 'reconcile', 'accepted', 'blocked', 'completed', 'failed', 'unknown'})
_ENVIRONMENTS = frozenset({'synthetic', 'replay', 'staging', 'production'})

@dataclass(frozen=True)
class DecisionTrace:
    task_id: str
    trace_id: str
    component: str
    outcome: str
    decision_digest: str
    environment: str
    timestamp: str

    def __post_init__(self):
        for value in (self.task_id, self.trace_id):
            try:
                if str(UUID(value)) != value:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError('IDs must be canonical UUIDs') from exc
        if self.component not in _COMPONENTS or self.outcome not in _OUTCOMES:
            raise ValueError('unsupported component or outcome')
        if self.environment not in _ENVIRONMENTS or not isinstance(self.decision_digest, str) or not _HASH.fullmatch(self.decision_digest):
            raise ValueError('invalid environment or digest')
        try:
            dt = datetime.fromisoformat(self.timestamp.replace('Z', '+00:00'))
            if dt.tzinfo is None or dt.utcoffset() != timezone.utc.utcoffset(dt):
                raise ValueError()
        except (ValueError, AttributeError) as exc:
            raise ValueError('timestamp must be UTC ISO 8601') from exc

    def jsonl(self):
        return json.dumps({'schema': 'potter.trace.decision/v0', 'task.id': self.task_id,
          'trace.id': self.trace_id, 'potter.component': self.component,
          'potter.outcome': self.outcome, 'potter.decision.digest': self.decision_digest,
          'potter.environment': self.environment, 'time': self.timestamp},
          sort_keys=True, separators=(',', ':')) + '\n'
