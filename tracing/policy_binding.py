"""Synthetic C1/C6 binding proposal. No context authentication or runtime use.

Caller explicitly binds independently supplied task context around decide().
ContextVar avoids a mutable per-engine current-task field. A canonical decision
ID supplies stable local event identity, never authorization to execute effects.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
from uuid import UUID, uuid5

from .policy_adapter import emit_policy_audit
from .trace import validate_timestamp

_EVENT_NAMESPACE = UUID('5d1e17ca-a940-4c92-83f8-99e9e7b3a2c1')


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError('canonical UUID required')


@dataclass(frozen=True)
class SyntheticContext:
    task_id: str | None
    trace_id: str
    timestamp: str

    def __post_init__(self):
        if self.task_id is not None:
            _uuid(self.task_id)
        _uuid(self.trace_id)
        validate_timestamp(self.timestamp)


class SyntheticPolicyAudit:
    """Hook for synthetic/replay local experiments only; sink owns durability.

    The caller must invoke the policy engine synchronously inside bind(). Tasks
    intentionally spawned inside a bound async context inherit that context;
    each independent request must establish its own binding. This is isolation
    of caller-supplied context, NOT provenance or an authority boundary.
    """
    def __init__(self, sink, *, environment):
        if environment not in ('synthetic', 'replay'):
            raise ValueError('offline environment required')
        self._sink = sink
        self._environment = environment
        self._context = ContextVar('synthetic_policy_audit', default=None)

    @contextmanager
    def bind(self, *, task_id, trace_id, timestamp):
        context = SyntheticContext(task_id, trace_id, timestamp)
        token = self._context.set(context)
        try:
            yield
        finally:
            self._context.reset(token)

    def hook(self, event):
        context = self._context.get()
        if context is None:
            raise ValueError('explicit request context required')

        def persist(line):
            # The adapter validates decision ID/correlation before this runs.
            # UUID5 includes the environment and separate audit record kind.
            record = json.loads(line)
            kind = 'uncorrelated' if record['schema'] == 'potter.trace.uncorrelated-denial/v0' else 'linked'
            subject = record['decision.id'] if kind == 'uncorrelated' else record['potter.subject.id']
            event_id = str(uuid5(_EVENT_NAMESPACE,
                self._environment + ':' + kind + ':' + subject))
            self._sink(event_id, line)

        return emit_policy_audit(event, task_id=context.task_id,
            trace_id=context.trace_id, environment=self._environment,
            timestamp=context.timestamp, sink=persist)
