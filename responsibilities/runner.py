"""Responsibility runner (package E2, decision T16; builds on C1, C2, E1).

    trigger ──(C2 trigger key)──▶ task ──claim──▶ steps ──▶ C1 decide ──▶ C2 journal ──▶ connector
                                                   │                          ▲
                                                   └── E1 notifier ◀── results, long-work state,
                                                       (JobHandle only)  escalations, watchdog

Rules this module enforces:

* Dedupe: every trigger instance maps to one C2 trigger key
  (``resp:<id>:slot:<n>`` or ``resp:<id>:event:<subject>``), so a duplicate,
  reordered or late trigger finds the existing task. A schedule slot older than
  the responsibility's watermark is dropped.
* Every step asks C1 first. ``allow`` journals the intent in C2 and executes it
  through C2 (``execute(..., connector=)``). A deny that only lacks an approval
  issues one through E1's ``ApprovalIssuer`` and parks the task in
  ``waiting_approval``. Any other deny fails the run.
* The runner never sends. It holds E1 ``JobHandle``s (enqueue-only): one per
  responsibility, bound to its declared delivery, and one ``potter.ops`` handle
  for escalations and watchdog alerts, which always go to Josh.
* Kill flag (E1): checked before claiming, before every step and right before
  every dispatch. A call already in flight completes and C2 records it. Nothing
  after it runs. The checkpoint records where to resume.
* Recovery: before any step of a claimed task runs, every ``unknown`` effect of
  that task is reconciled through C2 (``EffectJournal.reconcile``). If one stays
  unknown or is still in flight, the task is blocked (a retryable failure, so
  C2 escalates it after 3). Re-dispatch happens only where C2 itself allows it
  (fence or trusted contract digest). This module grants nothing.
* T16 state vocabulary (queued / running / waiting_approval / done / failed /
  escalated) is reported to E1's ``LongWork``: one ack after 30 s, an update
  only on a state change, a final message with the receipt link.

Staging-only (T22): connectors are injected, and the only ones in this
repository are test fakes.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from notifier import JobHandle, JobSpec, NotifierError, Stopped
from notifier.issuer import ApprovalIssuer, ApprovalRefusal
from notifier.kill import KillFlag
from policy import ALLOW, RECONCILE, Caller, DelegationHop, PolicyEngine, PolicyRequest
from runtime import (EFFECT_FAILED, SUCCEEDED, UNKNOWN, Database, Effect, EffectConflict,
                     EffectInFlight, EffectJournal, EffectKilled, EffectOutcomeUnknown, Escalation,
                     JournalApprovalLedger, LeaseLost, Task, TaskStore, URGENT)
from runtime.tasks import ESCALATE_AFTER
from runtime.db import as_database
from runtime.tasks import (DONE, ESCALATED, FAILED, QUEUED, RUNNING, TERMINAL,
                           WAITING_APPROVAL)

from . import _schema
from .definitions import Event, Responsibility, Schedule, Step, StepContext

OPS_JOB = "potter.ops"
LONG_WORK_ACK_S = 30.0   # mirrors E1 notifier.service.LONG_WORK_ACK_S (lint forbids importing it)
APPROVAL_CODES = frozenset({"approval_required", "untrusted_context_requires_approval"})
RECEIPT_PREFIX = "potter://receipts/"

# Run outcomes reported by run_once().
OUT_DONE, OUT_WAITING, OUT_RETRY, OUT_FAILED = "done", "waiting_approval", "retry", "failed"
OUT_ESCALATED, OUT_INTERRUPTED, OUT_BLOCKED, OUT_LEASE_LOST = (
    "escalated", "interrupted", "blocked", "lease_lost")
OUT_HELD = "held"


@dataclass(frozen=True)
class Connector:
    """An injected connector: ``perform(effect)`` must run ``effect.action()``
    and pass ``effect.idempotency_key`` to the provider. ``reconcile(effect)``
    reads provider state and returns ``succeeded`` / ``failed`` /
    ``still_unknown``. ``identity`` is what C2 binds the effect to."""
    identity: str
    perform: Callable[[Effect], Any]
    reconcile: Callable[[Effect], str]


@dataclass(frozen=True)
class RunResult:
    task_id: str
    responsibility: str | None
    outcome: str
    detail: str | None = None


# Internal control flow (never escapes this module).
class _Interrupt(Exception):
    pass


class _Blocked(Exception):
    pass


class _Retry(Exception):
    pass


class _Fatal(Exception):
    pass


class _TaskBindingMismatch(_Fatal):
    pass


class _Waiting(Exception):
    pass


def receipt_link(task_id: str) -> str:
    return f"{RECEIPT_PREFIX}{task_id}"


class KillSwitch:
    """C2 ``EffectJournal(kill_switch=...)`` callable for E1's global kill
    flag, bound to one database file. A ResponsibilityRunner refuses a journal
    whose kill switch is not a KillSwitch bound to the runner's own database:
    without it nothing but E2's early pre-check stands between /stop and a
    send, so the omission is refused at assembly instead of failing open."""
    __slots__ = ("_path", "_flag")

    def __init__(self, db: Database | str):
        db = as_database(db)
        self._path, self._flag = db.path, KillFlag(db)

    @property
    def db_path(self) -> str:
        return self._path

    def __call__(self, eff: Effect) -> bool:
        return self._flag.is_set()


def kill_switch_for(db: Database | str) -> KillSwitch:
    """The kill switch to pass when building the journal a
    ResponsibilityRunner uses: C2 then checks the flag inside the
    dispatch-claim transaction (which /stop cannot commit through) and again
    after the claim, just before ``perform``.

    Residual window (not closed): C2's last read and the connector call are
    not one atomic step, since ``perform`` runs outside any transaction. A /stop
    that commits after that last read does not stop that send. The effect is
    journaled and settled once (never re-dispatched), and the runner
    interrupts at the next step boundary. Only a provider- or C4-side fence
    can make "stop before send" a guarantee."""
    return KillSwitch(db)


class ResponsibilityRunner:
    def __init__(self, db: Database | str, *, definitions: Iterable[Responsibility],
                 journal: EffectJournal, policy: PolicyEngine, ledger: JournalApprovalLedger,
                 issuer: ApprovalIssuer, handle_for: Callable[[JobSpec], JobHandle],
                 connectors: Iterable[Connector], worker_id: str, lease_s: float = 120.0,
                 clock: Callable[[], float] = time.time, ops_job: str = OPS_JOB):
        if not worker_id or lease_s <= 0:
            raise ValueError("worker_id and positive lease_s required")
        self.db = as_database(db)
        for name, other in (("journal", journal.db), ("ledger", ledger.db), ("issuer", issuer.db)):
            if other.path != self.db.path:
                raise ValueError(f"{name} must share the runner's database")
        ks = journal.kill_switch
        if not isinstance(ks, KillSwitch) or ks.db_path != self.db.path:
            # Fail closed: a journal without this database's kill switch would
            # let a /stop that lands after the pre-check race the send.
            raise ValueError("journal must be built with kill_switch=kill_switch_for(db)"
                             " for the runner's database")
        _schema.ensure(self.db, now=clock())
        self.kill = KillFlag(self.db)
        self._clock = clock
        self.tasks = TaskStore(self.db, escalate=self._on_escalation, clock=clock)
        self._journal, self._policy, self._ledger, self._issuer = journal, policy, ledger, issuer
        self._worker, self._lease = worker_id, lease_s
        self._defs: dict[str, Responsibility] = {}
        for d in definitions:
            if not isinstance(d, Responsibility):
                raise TypeError("definitions must be Responsibility")
            if d.id in self._defs or d.id == ops_job:
                raise ValueError(f"duplicate responsibility {d.id!r}")
            self._defs[d.id] = d
        self._connectors: dict[str, Connector] = {}
        for k in connectors:
            if not isinstance(k, Connector) or k.identity in self._connectors:
                raise ValueError("connectors must be distinct Connector records")
            self._connectors[k.identity] = k
        for d in self._defs.values():
            for s in d.steps:
                if s.connector not in self._connectors:
                    raise ValueError(f"{d.id}.{s.id}: connector {s.connector!r} not configured")
        self._handles = {d.id: handle_for(d.job_spec) for d in self._defs.values()}
        self._ops = handle_for(JobSpec(ops_job, "josh"))
        for d in self._defs.values():
            self.tasks.register_job(d.id, d.cadence_s, tolerance=d.tolerance)
        self._alert_holds()

    @property
    def journal(self) -> EffectJournal:
        """Read-only: the journal is checked for its kill switch at assembly,
        so it cannot be swapped afterwards."""
        return self._journal

    def _now(self) -> float:
        return self._clock()

    # ================================================================ triggers
    def tick(self, now: float | None = None) -> list[Task]:
        """Submit the current slot of every schedule responsibility. Returns the
        tasks created by this call. Skipped entirely while the kill flag is set,
        so a stop does not leave a backlog of stale slots to replay."""
        now = self._now() if now is None else now
        if self.kill.is_set():
            return []
        created = []
        for d in self._defs.values():
            if not isinstance(d.trigger, Schedule):
                continue
            slot = d.trigger.slot(now)
            # ONE write transaction: the watermark check, its update and the
            # task insert commit together. Concurrent ticks (threads or
            # processes) serialize on the write lock, so a tick that decided
            # on an old watermark can never insert after a newer tick moved
            # it. There is no state between the two steps: a crash before
            # commit records neither, and the next tick submits its own
            # current slot (an earlier, missed slot is skipped, per the
            # late-slot rule).
            with self.db.tx() as c:
                wm = _schema.get_state(c, f"watermark:{d.id}")
                if wm is not None and slot < int(wm):
                    continue                      # late tick for a superseded slot
                _schema.set_state(c, f"watermark:{d.id}", str(slot))
                task, new = self.tasks.submit(
                    f"resp:{d.id}:slot:{slot}", d.id,
                    {"slot": slot, "scheduled_for": slot * d.trigger.every_s + d.trigger.offset_s},
                    job=d.id, now=now, tx=c)
            if new:
                created.append(task)
        return created

    def on_event(self, topic: str, event: Mapping[str, Any],
                 now: float | None = None) -> list[tuple[Task, bool]]:
        """Submit one task per responsibility subscribed to ``topic``. The
        trigger key comes from the event's subject fields, so a duplicate,
        reordered or late copy returns the existing task (``created=False``)
        and its payload is NOT replaced. Events are recorded even while the
        kill flag is set; they run after it is cleared."""
        now = self._now() if now is None else now
        out = []
        for d in self._defs.values():
            if isinstance(d.trigger, Event) and d.trigger.topic == topic:
                key = d.trigger.dedupe_key(event)
                out.append(self.tasks.submit(f"resp:{d.id}:event:{key}", d.id,
                                             {"topic": topic, "event": dict(event)},
                                             job=d.id, now=now))
        return out

    # ================================================================ running
    def run_once(self) -> RunResult | None:
        if self.kill.is_set():
            return None
        task = self.tasks.claim(self._worker, self._lease)
        if task is None:
            return None
        return self._run(task)

    def run_pending(self, max_runs: int = 1000) -> list[RunResult]:
        out = []
        for _ in range(max_runs):
            r = self.run_once()
            if r is None:
                break
            out.append(r)
        return out

    def _run(self, task: Task) -> RunResult:
        d = self._defs.get(task.kind)
        if d is None or task.job != d.id:
            # C2's claim has no kind filter; fail closed rather than guess.
            try:
                self.tasks.fail(task.id, self._worker, "unknown responsibility", retryable=False)
            except LeaseLost:
                return RunResult(task.id, None, OUT_LEASE_LOST)
            return RunResult(task.id, None, OUT_FAILED, "unknown_responsibility")
        cp = dict(task.checkpoint or {})
        hold = self._hold_of(task.id)
        if hold is not None:
            return self._park_held(d, task, cp, hold)
        # step id -> hash of the action an approval was requested for. E1 now
        # issues only for a task already waiting_approval, after this worker's
        # lease is gone, so the approval id itself is looked up by (task, hash).
        approvals: dict[str, str] = dict(cp.get("approvals", {}))
        index = int(cp.get("next", 0))
        try:
            self._report(d, task.id, RUNNING)
            self._reconcile_gate(task)
            results: dict[str, Any] = {}
            sources: set[str] = set()       # provenance of every earlier result
            for i, step in enumerate(d.steps):
                index = i
                ctx = StepContext(task.payload, MappingProxyType(dict(results)))
                done, result = self._already_done(task, step, approvals)
                if not done:
                    if self.kill.is_set():
                        raise _Interrupt()
                    self.tasks.heartbeat(task.id, self._worker, self._lease)
                    result = self._step(d, task, step, ctx, approvals, i,
                                        self._provenance(d, step, sources))
                results[step.id] = result
                sources |= step.result_sources(result)
                if done:
                    continue
                self.tasks.checkpoint(task.id, self._worker,
                                      {"next": i + 1, "approvals": approvals})
                self._report(d, task.id, RUNNING)
            index = len(d.steps)
            return self._finish(d, task, results)
        except LeaseLost:
            return RunResult(task.id, d.id, OUT_LEASE_LOST)
        except _Interrupt:
            return self._interrupt(d, task, index, approvals)
        except _Waiting as w:
            return RunResult(task.id, d.id, OUT_WAITING, str(w))
        except _Blocked as b:
            return self._retry(d, task, f"blocked: {b}", OUT_BLOCKED)
        except _Retry as r:
            return self._retry(d, task, str(r), OUT_RETRY)
        except _Fatal as f:
            return self._fatal(d, task, str(f))

    # ---------------------------------------------------------------- steps
    def _caller(self, d: Responsibility) -> Caller:
        return Caller(d.principal, (DelegationHop(f"responsibility:{d.id}", "cron",
                                                  d.capabilities()),))

    @staticmethod
    def _provenance(d: Responsibility, step: Step, sources: set[str]) -> tuple[str, ...]:
        """The trigger's provenance, plus every earlier result's provenance if
        this step's action may be derived from those results."""
        out = set(d.provenance)
        if step.derives_from_results:
            out |= sources
        return tuple(sorted(out))

    def _decide(self, d: Responsibility, step: Step, action, ctx: StepContext,
                provenance: tuple[str, ...], *,
                approval: Mapping[str, Any] | None = None, retry_intent=None):
        live = None
        if step.observe is not None and retry_intent is None:
            try:
                live = dict(step.observe(ctx))
            except Exception as exc:
                raise _Retry(f"live state unreadable for {step.id}: {type(exc).__name__}")
        return self._policy.decide(PolicyRequest(self._caller(d), step.capability, action,
                                                 approval=approval, live_state=live,
                                                 provenance=provenance,
                                                 retry_intent=retry_intent))

    def _build(self, step: Step, ctx: StepContext, task_id: str):
        try:
            action = step.build(ctx, task_id)
        except Exception as exc:
            raise _Fatal(f"step {step.id} could not bind its action: {type(exc).__name__}")
        if getattr(action, 'task_id', None) != task_id:
            raise _TaskBindingMismatch(f"step {step.id} task binding mismatch")
        return action

    def _already_done(self, task: Task, step: Step, approvals: Mapping[str, str]):
        eff = self._effect_of(task, step, approvals)
        if eff is not None and eff.status == SUCCEEDED:
            return True, eff.result
        return False, None

    def _effect_of(self, task: Task, step: Step, approvals: Mapping[str, str]) -> Effect | None:
        h = approvals.get(step.id)
        if h is None:
            return self._journal.for_step(task.id, step.id)
        record = self._approval_for(task.id, h)[2]
        return self._ledger.effect(record["nonce"]) if record else None

    def _step(self, d: Responsibility, task: Task, step: Step, ctx: StepContext,
              approvals: dict[str, str], index: int, provenance: tuple[str, ...]) -> Any:
        action = self._build(step, ctx, task.id)
        conn = self._connectors[step.connector]
        h = approvals.get(step.id)
        if h is None:
            eff = self._journal.for_step(task.id, step.id)
            dec = self._decide(d, step, action, ctx, provenance)
            if dec.outcome == ALLOW:
                if not self._dispatch_permitted():
                    raise _Interrupt()
                try:
                    eff = self._journal.record_intent(task.id, step.id, action)
                except EffectConflict:
                    raise _Fatal(f"step {step.id} is bound to a different action")
                return self._execute(eff, conn)
            if dec.reason_code in APPROVAL_CODES and eff is None:
                self._request_approval(task, step, action, approvals, index)
            raise _Fatal(f"policy denied {step.id}: {dec.reason_code}")
        if action.hash() != h:
            raise _Fatal(f"step {step.id} no longer binds the approved action")
        _, status, record = self._approval_for(task.id, h)
        if record is None:
            raise _Fatal(f"approval for {step.id} is {status or 'missing'}")
        eff = self._ledger.effect(record["nonce"])
        if eff is None:
            dec = self._decide(d, step, action, ctx, provenance, approval=record)
            if dec.outcome != ALLOW:
                raise _Fatal(f"policy denied approved {step.id}: {dec.reason_code}")
            eff = self._ledger.effect(record["nonce"])
        else:
            # Nonce already consumed (we crashed after C1 allowed it): C1 may
            # only route the recorded intent to reconciliation; C2 then decides.
            dec = self._decide(d, step, action, ctx, provenance, approval=record,
                               retry_intent=eff.intent())
            if dec.outcome != RECONCILE:
                raise _Fatal(f"policy denied resumed {step.id}: {dec.reason_code}")
        if eff is None or eff.task_id != task.id:
            raise _Fatal(f"no journaled intent for approved {step.id}")
        if not self._dispatch_permitted():
            raise _Interrupt()
        return self._execute(eff, conn)

    def _dispatch_permitted(self) -> bool:
        """Early kill check before a dispatch: a cheap pre-check only, not
        atomic on its own. The authoritative check is C2's ``kill_switch``
        (wire it with ``kill_switch_for``): C2 reads the kill flag inside the
        dispatch-claim transaction, which /stop cannot commit through, and
        again after the claim, before ``perform``, refusing or undoing the
        claim with ``EffectKilled`` (handled as an interrupt). A /stop after
        that last read does not stop the send in hand (see ``kill_switch_for``)."""
        return not self.kill.is_set()

    def _execute(self, eff: Effect, conn: Connector) -> Any:
        try:
            eff = self._journal.execute(eff.idempotency_key, conn.perform, conn.reconcile,
                                       owner=self._worker, connector=conn.identity)
        except EffectKilled:
            # C2 refused the dispatch under its claim lock (or undid the claim
            # before send): nothing was sent. Same as an interrupt.
            raise _Interrupt()
        except (EffectOutcomeUnknown, EffectInFlight) as exc:
            raise _Blocked(f"effect {eff.effect_id} unresolved ({type(exc).__name__})")
        except EffectConflict:
            raise _Fatal(f"effect {eff.effect_id} connector or adapter mismatch")
        if eff.status == EFFECT_FAILED:
            raise _Retry(f"effect {eff.effect_id} failed")
        if eff.status != SUCCEEDED:
            raise _Blocked(f"effect {eff.effect_id} is {eff.status}")
        return eff.result

    def _reconcile_gate(self, task: Task) -> None:
        """Before any step continues: every dispatched-but-unsettled effect of
        this task is reconciled through C2 (no dispatch). Anything still unknown
        or in flight blocks the task."""
        for eff in self._journal.unresolved():
            if eff.task_id != task.id or eff.status != UNKNOWN:
                continue
            conn = self._connectors.get(eff.connector or "")
            if conn is None:
                raise _Blocked(f"effect {eff.effect_id}: connector not configured")
            try:
                eff = self._journal.reconcile(eff.idempotency_key, conn.reconcile)
            except EffectInFlight:
                raise _Blocked(f"effect {eff.effect_id} still in flight")
            if eff.status == UNKNOWN:
                raise _Blocked(f"effect {eff.effect_id} outcome unknown")

    # ---------------------------------------------------------------- approvals
    def _approval_for(self, task_id: str, action_hash: str
                      ) -> tuple[str | None, str | None, dict | None]:
        """(approval id, status, signed record if approved) of THE request
        claimed for this task and action (``responsibility_approvals``; at most
        one exists). Read-only over E1's table: E1 has no API that returns the
        issued record (see README gaps)."""
        with self.db.read() as c:
            row = c.execute("SELECT a.approval_id, r.status, r.record"
                            " FROM responsibility_approvals a LEFT JOIN approval_requests r"
                            " ON r.approval_id = a.approval_id"
                            " WHERE a.task_id=? AND a.action_hash=?",
                            (task_id, action_hash)).fetchone()
        if row is None:
            return None, None, None
        aid, status, raw = row
        return aid, status, (json.loads(raw) if status == "approved" and raw else None)

    def _request_approval(self, task: Task, step: Step, action, approvals: dict[str, str],
                          index: int) -> None:
        """Checkpoint, park the task in waiting_approval, then ask E1 (which
        issues only for a waiting task). A crash or kill between parking and
        issuing leaves a waiting task with no request; ``poll_approvals``
        issues it. The task is never prompted twice for the same action."""
        approvals[step.id] = action.hash()
        self.tasks.checkpoint(task.id, self._worker,
                              {"next": index, "approvals": approvals, "waiting": step.id})
        self.tasks.wait_for_approval(task.id, self._worker)
        self._report(self._defs[task.kind], task.id, WAITING_APPROVAL)
        raise _Waiting(self._issue(task.id, step, action) or "")

    def _issue(self, task_id: str, step: Step, action) -> str | None:
        """Find-or-create the ONE approval request for this exact action, as a
        single BEGIN IMMEDIATE transaction: the claim lookup, E1's
        ``ApprovalIssuer.request`` (which writes in the caller's transaction)
        and the claim insert commit together or not at all. Concurrent workers
        or processes serialize on SQLite's write lock, so the second sees the
        first's claim; the primary key on (task, action hash) enforces it.

        Returns the claimed approval id whatever its status: an approval that
        was denied, expired, voided or cancelled is never re-issued for the
        same action (``poll_approvals`` ends the run instead). None if E1
        refuses (kill flag set, task no longer waiting)."""
        h = action.hash()
        try:
            with self.db.tx() as c:
                row = c.execute("SELECT approval_id FROM responsibility_approvals"
                                " WHERE task_id=? AND action_hash=?", (task_id, h)).fetchone()
                if row is not None:
                    return row[0]
                aid = self._issuer.request(c, action, summary=step.approval_summary,
                                           now=self._now())
                c.execute("INSERT INTO responsibility_approvals (task_id, action_hash,"
                          " approval_id, created_at) VALUES (?,?,?,?)",
                          (task_id, h, aid, self._now()))
                return aid
        except (Stopped, ApprovalRefusal):
            return None
        except sqlite3.IntegrityError:      # defensive: someone else's claim won
            return self._approval_for(task_id, h)[0]

    def _waiting_step(self, task: Task, *, report_binding_mismatch: bool = False) -> tuple[Responsibility, Step, Any] | None:
        """(definition, waiting step, its action rebuilt now) for a waiting task."""
        d = self._defs.get(task.kind)
        if d is None or task.state != WAITING_APPROVAL:
            return None
        cp = task.checkpoint or {}
        approvals = cp.get("approvals", {})
        results: dict[str, Any] = {}
        for step in d.steps:
            ctx = StepContext(task.payload, MappingProxyType(dict(results)))
            if step.id == cp.get("waiting"):
                try:
                    return d, step, self._build(step, ctx, task.id)
                except _TaskBindingMismatch:
                    if report_binding_mismatch:
                        raise
                    return None
                except Exception:
                    return None
            eff = self._effect_of(task, step, approvals)
            if eff is None or eff.status != SUCCEEDED:
                return None
            results[step.id] = eff.result
        return None

    def poll_approvals(self) -> list[RunResult]:
        """Move every waiting task on: approved -> queued (it resumes on the
        next claim); denied / expired / voided / cancelled -> failed. A waiting
        task with no request yet (crash or kill between parking and issuing)
        gets its request now, unless the kill flag is set."""
        now = self._now()
        out = []
        with self.db.read() as c:
            ids = [r[0] for r in c.execute(
                "SELECT id FROM tasks WHERE state=? ORDER BY created_at, id", (WAITING_APPROVAL,))]
        for tid in ids:
            task = self.tasks.get(tid)
            d = self._defs.get(task.kind) if task else None
            if d is None:
                continue
            if self._hold_of(tid) is not None:
                continue                      # operator decides; never prompt again
            cp = task.checkpoint or {}
            h = cp.get("approvals", {}).get(cp.get("waiting", ""))
            aid, status, _ = self._approval_for(tid, h) if h else (None, None, None)
            binding_mismatch = False
            if aid is None:
                try:
                    ws = self._waiting_step(task, report_binding_mismatch=True)
                except _TaskBindingMismatch:
                    ws = None
                    binding_mismatch = True
                if ws is not None and ws[2].hash() == h:
                    if self.kill.is_set() or self._issue(tid, ws[1], ws[2]) is not None:
                        continue                 # issued now, or after the flag clears
                # The waiting action can no longer be rebuilt as approved.
            pa = self._issuer.get(aid) if aid else None
            if pa is not None and pa.status == "pending" and now < pa.expires_at:
                self._report(d, tid, WAITING_APPROVAL)   # the 30 s ack may now be due
                continue
            if pa is not None and pa.status == "approved":
                t = self.tasks.approval_resolved(tid, True)
            else:
                why = ("task binding mismatch" if binding_mismatch else "missing") if pa is None else ("expired" if pa.status == "pending"
                                                    else pa.status)
                t = self.tasks.approval_resolved(tid, False, error=f"approval {why}")
            if t.state in TERMINAL:
                self._record_terminal(d, t)
            else:
                self._report(d, tid, t.state)
            out.append(RunResult(tid, d.id, QUEUED if t.state == QUEUED else t.state))
        return out

    def live_action(self, approval_id: str, task_id: str):
        """For E1's ``CommandRouter(live_action=...)``: the action the waiting
        step would bind *now*, rebuilt from the definition and earlier results,
        so E1 voids the approval if anything it was issued for has changed."""
        task = self.tasks.get(task_id)
        pa = self._issuer.get(approval_id)
        if task is None or pa is None or pa.task_id != task_id:
            return None
        ws = self._waiting_step(task)
        return ws[2] if ws is not None else None

    # ---------------------------------------------------------------- endings
    def _finish(self, d: Responsibility, task: Task, results: Mapping[str, Any]) -> RunResult:
        """Success becomes observable only together with the terminal state.

        ONE write transaction holds: the job result message (if the definition
        has a ``report``), C2's terminal ``complete`` (lease-checked inside it)
        and a pending ``responsibility_finals`` row. A crash, lease loss or kill
        flag before that commit leaves none of them, so no success is ever
        announced for a task C2 does not record as done; the task is rerun (no
        step repeats) or interrupted. The long-work "done" update and final
        message, which E1 writes in its own transactions, are sent only AFTER
        the commit, and the recovery sweep (``deliver_pending_finals``, run by
        ``report_progress`` and ``watchdog``) re-sends them idempotently if a
        crash lands between the commit and their delivery."""
        ctx = StepContext(task.payload, MappingProxyType(dict(results)))
        if self.kill.is_set():
            raise _Interrupt()
        text = d.report(ctx) if d.report is not None else None
        try:
            with self.db.tx() as c:
                if text:
                    self._handles[d.id].notify(
                        c, task.id, kind="job_result",
                        text=f"{text} Receipt: {receipt_link(task.id)}")
                self.tasks.complete(task.id, self._worker, tx=c)
                # Completion time = C2's committed terminal timestamp, read in
                # this same transaction (the same source the backfill uses).
                c.execute("INSERT OR IGNORE INTO responsibility_finals (task_id, responsibility,"
                          " outcome, created_at) SELECT id, ?, state, updated_at FROM tasks"
                          " WHERE id=? AND state=?", (d.id, task.id, DONE))
        except Stopped:
            raise _Interrupt()          # rolled back: not complete, nothing announced
        self._deliver_final(task.id)
        return RunResult(task.id, d.id, OUT_DONE)

    def _deliver_final(self, task_id: str) -> bool:
        """After the terminal commit: E1 long-work 'done' update and final
        message, then mark the final delivered, but ONLY on durable proof.

        What is owed is fixed at completion (``finals.created_at``): a final
        message if the run was long work (30 s or more since its first report),
        nothing for a short or silent run; for FAILED (not silent) also the
        failure alert with its receipt. The marker is set only when nothing
        is owed, or when E1's outbox holds every owed row (``work:...:final``,
        and for FAILED ``job:<id>:<task>``). ESCALATED is announced through the
        ops job's own durable escalation sweep.
        Any notifier failure (kill flag, a missing recipient address, anything
        else) leaves the marker pending for the next sweep; ``NotifierError``
        is never read as "already finished" on its own. A final whose
        responsibility definition is not loaded stays pending as well.
        Returns True once the final is (or already was) marked delivered."""
        with self.db.read() as c:
            row = c.execute("SELECT responsibility, outcome, created_at FROM"
                            " responsibility_finals WHERE task_id=? AND delivered_at IS NULL",
                            (task_id,)).fetchone()
        if row is None:
            return True
        name, outcome, completed_at = row
        d = self._defs.get(name)
        if d is None:
            return False                       # never mark what we cannot deliver
        owed = False
        alert_owed = outcome == FAILED and not self._handles[d.id].silent
        if alert_owed and not self._outbox_has(f"job:{d.id}:{task_id}"):
            if self.kill.is_set():
                return False
            try:
                with self.db.tx() as c:
                    self._handles[d.id].notify(c, task_id, kind="alert",
                                               text=f"{d.id} failed. Receipt: {receipt_link(task_id)}")
            except NotifierError:
                pass                           # judged below, by the outbox
        if not self._handles[d.id].silent:
            lw = self._long_work(d, task_id)
            owed = lw is not None and completed_at - lw[1] >= LONG_WORK_ACK_S
            if lw is not None:
                if self.kill.is_set():
                    return False
                now = self._now()
                for send in (lambda: lw[0].tick(outcome, now),
                             lambda: lw[0].finish(receipt_link(task_id), now, outcome=outcome)
                             if owed else None):
                    try:
                        send()
                    except NotifierError:     # incl. Stopped; judged below, by the outbox
                        pass
        if owed and not self._final_enqueued(d, task_id):
            return False                       # retried by the next sweep
        if alert_owed and not self._outbox_has(f"job:{d.id}:{task_id}"):
            return False
        with self.db.tx() as c:
            c.execute("UPDATE responsibility_finals SET delivered_at=? WHERE task_id=?"
                      " AND delivered_at IS NULL", (self._now(), task_id))
        return True

    def _final_enqueued(self, d: Responsibility, task_id: str) -> bool:
        return self._outbox_has(f"work:{d.id}:{task_id}:final")

    def _outbox_has(self, notification_id: str) -> bool:
        """Durable proof: E1's outbox holds this notification. Read-only over
        C3's table (no E1 API answers this; see README gaps)."""
        with self.db.read() as c:
            return c.execute("SELECT 1 FROM outbox WHERE notification_id=? LIMIT 1",
                             (notification_id,)).fetchone() is not None

    def deliver_pending_finals(self) -> int:
        """Recovery sweep: re-deliver every committed-but-undelivered final,
        including terminal failures whose finals row a crash prevented."""
        self._backfill_terminal_finals()
        with self.db.read() as c:
            ids = [r[0] for r in c.execute("SELECT task_id FROM responsibility_finals"
                                           " WHERE delivered_at IS NULL ORDER BY created_at")]
        return sum(1 for tid in ids if self._deliver_final(tid))

    # ---------------------------------------------------------------- holds (upgrade)
    def _hold_of(self, task_id: str):
        with self.db.read() as c:
            return c.execute("SELECT step_id, action_ref, reason FROM responsibility_holds"
                             " WHERE task_id=?", (task_id,)).fetchone()

    def holds(self) -> list[tuple[str, str, str]]:
        """(task id, step id, reason) of every task awaiting an operator."""
        with self.db.read() as c:
            return [tuple(r) for r in c.execute("SELECT task_id, step_id, reason FROM"
                                                " responsibility_holds ORDER BY created_at")]

    def _park_held(self, d: Responsibility, task: Task, cp: dict, hold) -> RunResult:
        """A held task that was queued/running is parked in waiting_approval on
        its held step, without any new prompt, until an operator resolves it."""
        step_id, _, reason = hold
        try:
            self.tasks.checkpoint(task.id, self._worker, {**cp, "waiting": step_id})
            self.tasks.wait_for_approval(task.id, self._worker)
        except LeaseLost:
            return RunResult(task.id, d.id, OUT_LEASE_LOST)
        self._alert_holds()
        return RunResult(task.id, d.id, OUT_HELD, reason)

    def _alert_holds(self) -> None:
        for task_id, step_id, reason in self.holds():
            task = self.tasks.get(task_id)
            if task is not None and task.kind in self._defs:
                self._alert(f"hold:{task_id}",
                            f"{task.kind}: approval for step {step_id} needs an operator"
                            f" ({reason}); no new prompt was sent."
                            f" Receipt: {receipt_link(task_id)}")

    def resolve_hold(self, task_id: str, approval_id: str | None) -> None:
        """Operator decision for a held task. ``approval_id``: claim that exact
        E1 request (it must belong to this task and the held action); the run
        then follows it like any claimed approval. ``None``: end the run
        (failed) without any approval. Nothing is ever re-issued."""
        hold = self._hold_of(task_id)
        if hold is None:
            raise KeyError(task_id)
        step_id, ref, _ = hold
        if approval_id is None:
            task = self.tasks.get(task_id)
            if task is not None and task.state == WAITING_APPROVAL:
                self.tasks.approval_resolved(task_id, False,
                                             error="held approval rejected by operator")
            with self.db.tx() as c:
                c.execute("DELETE FROM responsibility_holds WHERE task_id=?", (task_id,))
            return
        with self.db.tx() as c:
            row = c.execute("SELECT task_id, action_hash FROM approval_requests"
                            " WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None or row[0] != task_id or row[1] != ref:
                raise ValueError("approval does not belong to the held task and action")
            c.execute("INSERT OR REPLACE INTO responsibility_approvals (task_id, action_hash,"
                      " approval_id, created_at) VALUES (?,?,?,?)",
                      (task_id, ref, approval_id, self._now()))
            c.execute("DELETE FROM responsibility_holds WHERE task_id=?", (task_id,))

    def _record_terminal(self, d: Responsibility, t: Task) -> None:
        """After C2 committed a FAILED / ESCALATED state: record that its final
        messages are owed, then deliver them. C2's terminal state is itself the
        durable record, so a crash between that commit and this call is
        recovered by ``deliver_pending_finals`` (``_backfill_terminal_finals``)."""
        with self.db.tx() as c:
            # Completion time = C2's committed terminal timestamp (tasks.updated_at),
            # never the time this call happens to run: a delayed record must not
            # turn a short run into long work. Same source as the backfill.
            c.execute("INSERT OR IGNORE INTO responsibility_finals (task_id, responsibility,"
                      " outcome, created_at) SELECT id, ?, state, updated_at FROM tasks"
                      " WHERE id=? AND state IN (?,?)", (d.id, t.id, FAILED, ESCALATED))
        self._deliver_final(t.id)

    def _backfill_terminal_finals(self) -> None:
        """Every FAILED / ESCALATED task of these responsibilities without a
        finals row gets one (completion time = the task's last update)."""
        if not self._defs:
            return
        marks = ",".join("?" * len(self._defs))
        with self.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO responsibility_finals (task_id, responsibility,"
                      " outcome, created_at) SELECT t.id, t.kind, t.state, t.updated_at"
                      f" FROM tasks t WHERE t.state IN (?,?) AND t.kind IN ({marks})"
                      " AND NOT EXISTS (SELECT 1 FROM responsibility_finals f"
                      " WHERE f.task_id = t.id)", (FAILED, ESCALATED, *self._defs))

    def _retry(self, d: Responsibility, task: Task, error: str, outcome: str) -> RunResult:
        try:
            t = self.tasks.fail(task.id, self._worker, error, retryable=True)
        except LeaseLost:
            return RunResult(task.id, d.id, OUT_LEASE_LOST)
        if t.state in TERMINAL:
            self._record_terminal(d, t)
        else:
            self._report(d, task.id, t.state)
        return RunResult(task.id, d.id, OUT_ESCALATED if t.state == ESCALATED else outcome, error)

    def _fatal(self, d: Responsibility, task: Task, error: str) -> RunResult:
        try:
            t = self.tasks.fail(task.id, self._worker, error, retryable=False)
        except LeaseLost:
            return RunResult(task.id, d.id, OUT_LEASE_LOST)
        self._record_terminal(d, t)
        return RunResult(task.id, d.id, OUT_ESCALATED if t.state == ESCALATED else OUT_FAILED,
                         error)

    def _interrupt(self, d: Responsibility, task: Task, index: int,
                   approvals: Mapping[str, str]) -> RunResult:
        """Kill flag: record where to resume, then hand the task back to the
        queue with C2's lease-checked ``release``. A pause is not a failure:
        no counter moves, so repeated stops never escalate a task, and a
        result held back in ``_finish`` is delivered after the flag clears.
        The reported outcome is the state C2 actually recorded."""
        try:
            self.tasks.checkpoint(task.id, self._worker, {"next": index, "approvals":
                                                          dict(approvals), "interrupted": True})
            t = self.tasks.release(task.id, self._worker)
        except LeaseLost:
            return RunResult(task.id, d.id, OUT_LEASE_LOST)
        if t.state != QUEUED:          # defensive: never claim a resume C2 did not record
            return RunResult(task.id, d.id, t.state, "interrupt did not release the task")
        return RunResult(task.id, d.id, OUT_INTERRUPTED, f"resume at step {index}")

    # ---------------------------------------------------------------- long work (E1)
    def _long_work(self, d: Responsibility, task_id: str):
        """(fresh E1 LongWork for the task, its start time), or None if the run
        never reported. E1 reads its own progress (ack, update numbering, last
        state told) back from its durable outbox on every tick."""
        with self.db.read() as c:
            row = _schema.load_progress(c, task_id)
        return None if row is None else (self._handles[d.id].long_work(task_id, row[0]), row[0])

    def _report(self, d: Responsibility, task_id: str, state: str) -> None:
        """Hand a T16 state to E1's LongWork. E2 stores only the run's start
        time (the 30 s clock, so it survives a restart). Whether an ack or an
        update is still owed is decided by E1 from its durable outbox, never
        from local history: every report ticks E1, and E1 makes a repeated
        tick a no-op. A crash between pinning the start and E1's enqueue is
        therefore simply retried by the next report."""
        if self._handles[d.id].silent or self.kill.is_set():
            return
        now = self._now()
        with self.db.tx() as c:
            row = _schema.load_progress(c, task_id)
            if row is None:
                _schema.save_progress(c, task_id, now, [])
                started = now
            else:
                started = row[0]
        try:
            self._handles[d.id].long_work(task_id, started).tick(state, now)
        except NotifierError:
            pass    # Stopped, or the work already has its final message

    def report_progress(self) -> int:
        """Periodic long-work reporter (the C8 loop calls it on a cadence,
        like ``poll_approvals``): ticks every running or waiting task of these
        responsibilities, so the 30 s ack is sent even while a single long step
        or a pending approval produces no state change. Safe from any process:
        a tick only enqueues, idempotently. Returns the number of tasks ticked."""
        with self.db.read() as c:
            rows = c.execute("SELECT id, kind, state FROM tasks WHERE state IN (?,?)"
                             " ORDER BY created_at, id", (RUNNING, WAITING_APPROVAL)).fetchall()
        self.deliver_pending_finals()
        n = 0
        for tid, kind, state in rows:
            d = self._defs.get(kind)
            if d is not None:
                self._report(d, tid, state)
                n += 1
        return n

    # ================================================================ escalation / watchdog
    def _escalation_note(self, subject: str, ident: str) -> tuple[str, str] | None:
        if subject == "task":
            task = self.tasks.get(ident)
            if task is None or task.job not in self._defs:
                return None
            return (f"escalation:task:{ident}",
                    f"{task.job}: a run failed {ESCALATE_AFTER} times in a row and was"
                    f" escalated. Receipt: {receipt_link(ident)}")
        job = self.tasks.job(ident)
        if job is None or job.name not in self._defs or job.escalated_at is None:
            return None
        return (f"escalation:job:{ident}:{int(job.escalated_at)}",
                f"{ident}: {ESCALATE_AFTER} consecutive runs failed; needs attention.")

    def _alert(self, key: str, text: str) -> bool:
        try:
            with self.db.tx() as c:
                self._ops.notify(c, key, kind="alert", text=text, urgency=URGENT)
            return True
        except Stopped:
            return False

    def _on_escalation(self, e: Escalation) -> None:
        # C2 calls this after commit, at most once; watchdog() re-enqueues
        # (idempotently) whatever a crash in between lost.
        note = self._escalation_note(e.subject, e.id)
        if note is not None:
            self._alert(*note)

    def watchdog(self, now: float | None = None) -> list[str]:
        """C2 last-success watchdog for every responsibility, plus a durable
        re-enqueue of escalations. Returns the stale responsibility ids."""
        now = self._now() if now is None else now
        stale = [j for j in self.tasks.stale_jobs(now=now) if j.name in self._defs]
        self.deliver_pending_finals()
        self._alert_holds()
        for j in stale:
            limit = j.cadence_s * j.tolerance
            self._alert(f"watchdog:{j.name}:{int(now // limit)}",
                        f"{j.name}: no successful run within {int(limit)} s (watchdog).")
        with self.db.read() as c:
            esc_tasks = [r[0] for r in c.execute("SELECT id FROM tasks WHERE state=?",
                                                 (ESCALATED,))]
            esc_jobs = [r[0] for r in c.execute("SELECT name FROM jobs WHERE escalated_at"
                                                " IS NOT NULL")]
        for subject, ids in (("task", esc_tasks), ("job", esc_jobs)):
            for ident in ids:
                note = self._escalation_note(subject, ident)
                if note is not None:
                    self._alert(*note)
        return [j.name for j in stale]

    def recover(self) -> list[Effect]:
        """Startup sweep: reconcile (never dispatch) every unknown effect of a
        responsibility task. Returns those still unresolved; their tasks stay
        blocked until an authoritative reconcile resolves them."""
        left = []
        for eff in self._journal.unresolved():
            task = self.tasks.get(eff.task_id)
            if task is None or task.kind not in self._defs or eff.status != UNKNOWN:
                continue
            conn = self._connectors.get(eff.connector or "")
            try:
                if conn is not None:
                    eff = self._journal.reconcile(eff.idempotency_key, conn.reconcile)
            except EffectInFlight:
                pass
            if eff.status == UNKNOWN:
                left.append(eff)
        return left

