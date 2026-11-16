"""Offline trace record primitives; no persistence or runtime wiring."""
from dataclasses import dataclass
from datetime import datetime
import json
import re
from uuid import UUID

_HASH = re.compile(r'^[0-9a-f]{64}$')
_DECISION_ID = re.compile(r'^dec-[0-9a-f]{32}$')
_UTC_TIMESTAMP = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)$')
_COMPONENTS = frozenset({'policy', 'task', 'effect'})
_OUTCOMES = frozenset({'allow', 'deny', 'reconcile', 'accepted', 'blocked', 'completed', 'failed', 'unknown'})
_ENVIRONMENTS = frozenset({'synthetic', 'replay', 'staging', 'production'})
# Join fields (schema v1): tie a policy decision to the effect it authorizes.
_EFFECT_ID = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')
_CAPABILITY = re.compile(r'^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$')
_REASON = re.compile(r'^[a-z][a-z_]{0,63}$')
SCHEMA_V0 = 'potter.trace.decision/v0'
SCHEMA_V1 = 'potter.trace.decision/v1'

def validate_timestamp(value):
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise ValueError('timestamp must be canonical UTC ISO 8601')
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('timestamp must be canonical UTC ISO 8601') from exc

@dataclass(frozen=True)
class DecisionTrace:
    task_id: str
    trace_id: str
    component: str
    outcome: str
    decision_digest: str
    environment: str
    timestamp: str
    subject_id: str | None = None
    action_hash: str | None = None
    effect_id: str | None = None
    capability: str | None = None
    reason_code: str | None = None

    def __post_init__(self):
        for value, pattern, name in ((self.action_hash, _HASH, 'action hash'), (self.effect_id, _EFFECT_ID, 'effect ID'),
                                     (self.capability, _CAPABILITY, 'capability'), (self.reason_code, _REASON, 'reason code')):
            if value is not None and (not isinstance(value, str) or not pattern.fullmatch(value)
                                      or (name == 'capability' and len(value) > 128)):
                raise ValueError(f'invalid {name}')
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
        if self.subject_id is not None:
            if not isinstance(self.subject_id, str):
                raise ValueError('invalid subject ID')
            if self.component == 'policy':
                if not _DECISION_ID.fullmatch(self.subject_id):
                    raise ValueError('invalid policy decision ID')
            else:
                try:
                    if str(UUID(self.subject_id)) != self.subject_id:
                        raise ValueError()
                except (ValueError, AttributeError) as exc:
                    raise ValueError('subject ID must be a canonical UUID') from exc
        validate_timestamp(self.timestamp)

    def jsonl(self):
        joined = any(v is not None for v in (self.action_hash, self.effect_id, self.capability, self.reason_code))
        data = {'schema': SCHEMA_V1 if joined else SCHEMA_V0, 'task.id': self.task_id,
          'trace.id': self.trace_id, 'potter.component': self.component,
          'potter.outcome': self.outcome, 'potter.decision.digest': self.decision_digest,
          'potter.environment': self.environment, 'time': self.timestamp}
        if self.subject_id is not None:
            data['potter.subject.id'] = self.subject_id
        for key, value in (('potter.action.hash', self.action_hash), ('potter.effect.id', self.effect_id),
                           ('potter.capability', self.capability), ('potter.reason', self.reason_code)):
            if value is not None:
                data[key] = value
        return json.dumps(data, sort_keys=True, separators=(',', ':')) + '\n'
