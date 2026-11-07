"""Declarative responsibility definitions (package E2, decision T16).

A responsibility is a standing duty: a trigger (a schedule or an event topic)
creates one C2 task per trigger instance, and the task runs a fixed list of
steps. Each step names a registered C1 capability, the action it binds, and the
connector identity its effect is dispatched through. Nothing in a definition can
send a message: delivery is declared (``"josh"`` or ``"silent"``) and the
runner maps it onto an E1 ``JobSpec``.

Determinism rule: a step's action must be a pure function of the task payload
and the results of earlier steps. A resumed run rebuilds the action and the C2
journal refuses (``EffectConflict``) a step that comes back bound to a
different action.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from notifier import JobSpec

_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
DELIVERY = frozenset({"josh", "silent"})
TRUSTED_SCHEDULE_PROVENANCE = ("system_policy",)
# An event carries data from outside Potter. C1 treats this kind as untrusted,
# so any write it triggers needs an approval.
DEFAULT_EVENT_PROVENANCE = ("external_event",)
# A step result whose source the definition does not declare. Untrusted at C1.
UNVERIFIED_RESULT = "unverified_result"


class DefinitionError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    """Fires once per slot of ``every_s`` seconds (slot = floor((t - offset) / every))."""
    every_s: float
    offset_s: float = 0.0

    def __post_init__(self):
        if not (isinstance(self.every_s, (int, float)) and self.every_s > 0):
            raise DefinitionError("schedule needs every_s > 0")

    def slot(self, now: float) -> int:
        return int((now - self.offset_s) // self.every_s)


@dataclass(frozen=True)
class Event:
    """Fires on an event topic. ``key`` names the event field(s) that identify
    the subject. The trigger key is built from them, never from arrival order or
    time, so a duplicate, reordered or late copy of the same subject maps onto the
    same C2 task."""
    topic: str
    key: tuple[str, ...] = ("id",)

    def __post_init__(self):
        if isinstance(self.key, str):
            object.__setattr__(self, "key", (self.key,))
        if not self.topic or not self.key or not all(isinstance(k, str) and k for k in self.key):
            raise DefinitionError("event trigger needs a topic and key field names")

    def dedupe_key(self, event: Mapping[str, Any]) -> str:
        """Typed canonical encoding: the JSON array of ``[field, value]`` pairs.
        Values must be non-empty strings or integers, so no two distinct
        subjects can encode alike (separators are escaped by JSON, and ``1``
        and ``"1"`` stay different)."""
        if not isinstance(event, Mapping):
            raise DefinitionError("event must be a mapping")
        pairs = []
        for k in self.key:
            v = event.get(k)
            if isinstance(v, bool) or not isinstance(v, (str, int)) or v == "":
                raise DefinitionError(f"event dedupe field {k!r} must be a non-empty"
                                      " string or an integer")
            pairs.append([k, v])
        return json.dumps(pairs, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class StepContext:
    payload: Any
    results: Mapping[str, Any]


Builder = Callable[[StepContext], Mapping[str, Any]]


@dataclass(frozen=True)
class Step:
    """One effectful step. ``id`` is stable within the responsibility and is the
    C2 effect id (``record_intent(task, id, action)``) for steps that need no
    approval. ``parameters`` / ``preconditions`` are mappings or deterministic
    builders over ``StepContext``. ``observe`` reads live state for stateful C1
    capabilities; its value is passed as ``live_state`` and never persisted.

    ``result_provenance`` declares the source kinds of what this step returns
    (e.g. ``("external_sender",)`` for a mail read). ``None`` means unknown,
    which is recorded as ``unverified_result`` (untrusted). A result mapping's
    own ``"provenance"`` list is added to it, and can only add sources, never
    remove them. Any later step whose action is built by a callable (so it may
    use earlier results) carries the union of all earlier results' provenance
    to C1, so a write derived from external data needs an approval even when
    the trigger was a trusted schedule."""
    id: str
    capability: str
    operation: str
    target: str
    connector: str
    parameters: Mapping[str, Any] | Builder = field(default_factory=dict)
    preconditions: Mapping[str, Any] | Builder = field(default_factory=dict)
    observe: Callable[[StepContext], Mapping[str, Any]] | None = None
    approval_summary: str = "Approve this step?"
    result_provenance: tuple[str, ...] | None = None

    def __post_init__(self):
        if not _ID.match(self.id or ""):
            raise DefinitionError("step id must be dotted lowercase")
        if self.result_provenance is not None:
            object.__setattr__(self, "result_provenance", tuple(self.result_provenance))
            if not self.result_provenance or not all(
                    isinstance(k, str) and k for k in self.result_provenance):
                raise DefinitionError(f"step {self.id}: result_provenance must name sources")
        for name in ("capability", "operation", "target", "connector"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise DefinitionError(f"step {self.id}: {name} required")

    @property
    def derives_from_results(self) -> bool:
        """Conservative: a builder callable may read earlier results."""
        return callable(self.parameters) or callable(self.preconditions)

    def result_sources(self, result: Any) -> frozenset[str]:
        out = set(self.result_provenance or (UNVERIFIED_RESULT,))
        if isinstance(result, Mapping) and "provenance" in result:
            extra = result["provenance"]
            if isinstance(extra, (list, tuple)) and extra and all(
                    isinstance(k, str) and k for k in extra):
                out.update(extra)
            else:
                out.add(UNVERIFIED_RESULT)
        return frozenset(out)

    def build(self, ctx: StepContext, task_id: str):
        from policy._contracts import approval as c0
        params = self.parameters(ctx) if callable(self.parameters) else self.parameters
        pre = self.preconditions(ctx) if callable(self.preconditions) else self.preconditions
        return c0.BoundAction(task_id, self.capability, self.operation, self.target,
                              dict(params), dict(pre))


@dataclass(frozen=True)
class Responsibility:
    """``delivery`` is where results go: ``"josh"`` or ``"silent"``. Escalations
    and watchdog alerts always reach Josh, through the runner's ops job.

    ``cadence_s`` feeds C2's last-success watchdog (``stale_jobs``). It defaults
    to the schedule period. An event responsibility must state one explicitly,
    for example the longest normal gap between bills.

    ``provenance`` is the trigger's source kinds. A schedule defaults to
    ``system_policy``. An event always carries ``external_event``: a declared
    value can add sources but never remove that one.

    ``report`` builds the result text that is delivered on success (``None``
    means no result message). It must not include secrets or raw action
    parameters."""
    id: str
    trigger: Schedule | Event
    delivery: str
    steps: tuple[Step, ...]
    cadence_s: float | None = None
    tolerance: float = 1.5
    principal: str = "agent:potter"
    provenance: tuple[str, ...] | None = None
    report: Callable[[StepContext], str] | None = None

    def __post_init__(self):
        if not _ID.match(self.id or ""):
            raise DefinitionError("responsibility id must be dotted lowercase")
        if not isinstance(self.trigger, (Schedule, Event)):
            raise DefinitionError("trigger must be Schedule or Event")
        if self.delivery not in DELIVERY:
            raise DefinitionError("delivery must be 'josh' or 'silent'")
        object.__setattr__(self, "steps", tuple(self.steps))
        if not self.steps or not all(isinstance(s, Step) for s in self.steps):
            raise DefinitionError("a responsibility needs at least one Step")
        ids = [s.id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise DefinitionError("duplicate step id")
        if self.cadence_s is None:
            if isinstance(self.trigger, Event):
                raise DefinitionError("event responsibilities must declare cadence_s")
            object.__setattr__(self, "cadence_s", float(self.trigger.every_s))
        if not self.cadence_s > 0 or self.tolerance < 1:
            raise DefinitionError("cadence_s > 0 and tolerance >= 1 required")
        if self.provenance is None:
            prov = (TRUSTED_SCHEDULE_PROVENANCE if isinstance(self.trigger, Schedule)
                    else DEFAULT_EVENT_PROVENANCE)
        else:
            prov = tuple(self.provenance)
            if not all(isinstance(k, str) and k for k in prov):
                raise DefinitionError("provenance must name source kinds")
        if isinstance(self.trigger, Event):
            # Add-only, like result provenance: a definition can add sources to
            # an event but never declare its payload trusted.
            prov = tuple(sorted(set(prov) | set(DEFAULT_EVENT_PROVENANCE)))
        object.__setattr__(self, "provenance", prov)

    @property
    def job_spec(self) -> JobSpec:
        return JobSpec(self.id, self.delivery)

    def capabilities(self) -> frozenset[str]:
        return frozenset(s.capability for s in self.steps)
