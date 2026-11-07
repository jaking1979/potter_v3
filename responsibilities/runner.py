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
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from notifier import JobHandle, JobSpec, NotifierError, Stopped
from notifier.issuer import ApprovalIssuer
from notifier.kill import KillFlag
from policy import ALLOW, RECONCILE, Caller, DelegationHop, PolicyEngine, PolicyRequest
from runtime import (EFFECT_FAILED, SUCCEEDED, UNKNOWN, Database, Effect, EffectConflict,
                     EffectInFlight, EffectJournal, EffectOutcomeUnknown, Escalation,
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


class _Waiting(Exception):
    pass


def receipt_link(task_id: str) -> str:
    return f"{RECEIPT_PREFIX}{task_id}"


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
        _schema.ensure(self.db)
        self.kill = KillFlag(self.db)
        self._clock = clock
        self.tasks = TaskStore(self.db, escalate=self._on_escalation, clock=clock)
        self.journal, self._policy, self._ledger, self._issuer = journal, policy, ledger, issuer
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
            with self.db.tx() as c:
                wm = _schema.get_state(c, f"watermark:{d.id}")
                if wm is not None and slot < int(wm):
                    continue                      # late tick for a superseded slot
                _schema.set_state(c, f"watermark:{d.id}", str(slot))
            task, new = self.tasks.submit(
                f"resp:{d.id}:slot:{slot}", d.id,
                {"slot": slot, "scheduled_for": slot * d.trigger.every_s + d.trigger.offset_s},
                job=d.id, now=now)
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
            return step.build(ctx, task_id)
        except Exception as exc:
            raise _Fatal(f"step {step.id} could not bind its action: {type(exc).__name__}")

    def _already_done(self, task: Task, step: Step, approvals: Mapping[str, str]):
        eff = self._effect_of(task, step, approvals)
        if eff is not None and eff.status == SUCCEEDED:
            return True, eff.result
        return False, None

    def _effect_of(self, task: Task, step: Step, approvals: Mapping[str, str]) -> Effect | None:
        aid = approvals.get(step.id)
        if aid is None:
            return self.journal.for_step(task.id, step.id)
        record = self._approval_record(aid)[1]
        return self._ledger.effect(record["nonce"]) if record else None

    def _step(self, d: Responsibility, task: Task, step: Step, ctx: StepContext,
              approvals: dict[str, str], index: int, provenance: tuple[str, ...]) -> Any:
        action = self._build(step, ctx, task.id)
        conn = self._connectors[step.connector]
        aid = approvals.get(step.id)
        if aid is None:
            eff = self.journal.for_step(task.id, step.id)
            dec = self._decide(d, step, action, ctx, provenance)
            if dec.outcome == ALLOW:
                if not self._dispatch_permitted():
                    raise _Interrupt()
                try:
                    eff = self.journal.record_intent(task.id, step.id, action)
                except EffectConflict:
                    raise _Fatal(f"step {step.id} is bound to a different action")
                return self._execute(eff, conn)
            if dec.reason_code in APPROVAL_CODES and eff is None:
                self._request_approval(task, step, action, approvals, index)
            raise _Fatal(f"policy denied {step.id}: {dec.reason_code}")
        status, record = self._approval_record(aid)
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
        """SEAM (E1 #28 / C2): the kill check right before a dispatch.

        Today this is a plain read of E1's kill flag, followed by a separate C2
        ``execute`` call. A ``/stop`` that commits between the two can still be
        followed by one dispatch. E1 (1b88a01) closed that window for notifier
        sends with a private gate (kill check + ``notifier_sending`` row in one
        transaction, which ``stop()`` waits on), but that gate does not cover
        connector effects and is not a public API. This dispatch must use either
        a public form of that gate, which ``stop()`` would also wait on, or a C2
        ``execute`` kill predicate evaluated inside its claim transaction. Until
        one exists, the window is open; see README."""
        return not self.kill.is_set()

    def _execute(self, eff: Effect, conn: Connector) -> Any:
        try:
            eff = self.journal.execute(eff.idempotency_key, conn.perform, conn.reconcile,
                                       owner=self._worker, connector=conn.identity)
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
        for eff in self.journal.unresolved():
            if eff.task_id != task.id or eff.status != UNKNOWN:
                continue
            conn = self._connectors.get(eff.connector or "")
            if conn is None:
                raise _Blocked(f"effect {eff.effect_id}: connector not configured")
            try:
                eff = self.journal.reconcile(eff.idempotency_key, conn.reconcile)
            except EffectInFlight:
                raise _Blocked(f"effect {eff.effect_id} still in flight")
            if eff.status == UNKNOWN:
                raise _Blocked(f"effect {eff.effect_id} outcome unknown")

    # ---------------------------------------------------------------- approvals
    def _approval_record(self, approval_id: str) -> tuple[str | None, dict | None]:
        """(status, signed record if approved). Read-only over E1's table: E1
        has no API that returns the issued record (see README gaps)."""
        with self.db.read() as c:
            row = c.execute("SELECT status, record FROM approval_requests WHERE approval_id=?",
                            (approval_id,)).fetchone()
        if row is None:
            return None, None
        status, raw = row
        return status, (json.loads(raw) if status == "approved" and raw else None)

    def _request_approval(self, task: Task, step: Step, action, approvals: dict[str, str],
                          index: int) -> None:
        with self.db.read() as c:
            row = c.execute("SELECT approval_id FROM approval_requests WHERE task_id=? AND"
                            " action_hash=? AND status IN ('pending','approved')"
                            " ORDER BY requested_at DESC LIMIT 1",
                            (task.id, action.hash())).fetchone()
        if row is not None:
            aid = row[0]          # issued before a crash; never prompt twice
        else:
            try:
                with self.db.tx() as c:
                    aid = self._issuer.request(c, action, summary=step.approval_summary,
                                               now=self._now())
            except Stopped:
                raise _Interrupt()
        approvals[step.id] = aid
        self.tasks.checkpoint(task.id, self._worker,
                              {"next": index, "approvals": approvals, "waiting": step.id})
        self.tasks.wait_for_approval(task.id, self._worker)
        self._report(self._defs[task.kind], task.id, WAITING_APPROVAL)
        raise _Waiting(aid)

    def poll_approvals(self) -> list[RunResult]:
        """Move every waiting task on: approved -> queued (it resumes on the
        next claim); denied / expired / voided / cancelled -> failed."""
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
            cp = task.checkpoint or {}
            aid = cp.get("approvals", {}).get(cp.get("waiting", ""))
            pa = self._issuer.get(aid) if aid else None
            if pa is not None and pa.status == "pending" and now < pa.expires_at:
                continue
            if pa is not None and pa.status == "approved":
                t = self.tasks.approval_resolved(tid, True)
            else:
                why = "missing" if pa is None else ("expired" if pa.status == "pending"
                                                    else pa.status)
                t = self.tasks.approval_resolved(tid, False, error=f"approval {why}")
            self._report(d, tid, t.state)
            if t.state in TERMINAL:
                self._final_messages(d, t, None)
            out.append(RunResult(tid, d.id, QUEUED if t.state == QUEUED else t.state))
        return out

    def live_action(self, approval_id: str, task_id: str):
        """For E1's ``CommandRouter(live_action=...)``: the action the waiting
        step would bind *now*, rebuilt from the definition and earlier results,
        so E1 voids the approval if anything it was issued for has changed."""
        task = self.tasks.get(task_id)
        d = self._defs.get(task.kind) if task else None
        if d is None or task.state != WAITING_APPROVAL:
            return None
        cp = task.checkpoint or {}
        approvals = cp.get("approvals", {})
        results: dict[str, Any] = {}
        for step in d.steps:
            ctx = StepContext(task.payload, MappingProxyType(dict(results)))
            if step.id == cp.get("waiting"):
                return step.build(ctx, task.id) if approvals.get(step.id) == approval_id else None
            eff = self._effect_of(task, step, approvals)
            if eff is None or eff.status != SUCCEEDED:
                return None
            results[step.id] = eff.result
        return None

    # ---------------------------------------------------------------- endings
    def _finish(self, d: Responsibility, task: Task, results: Mapping[str, Any]) -> RunResult:
        """Messages first, then complete: the enqueue is idempotent, so a crash
        in between re-runs no effect and re-enqueues nothing new. If the kill
        flag stops the result message, the task is NOT completed: it is
        interrupted at ``len(steps)``, so the run resumes after the flag is
        cleared, re-runs no step, and delivers the result then."""
        ctx = StepContext(task.payload, MappingProxyType(dict(results)))
        if self.kill.is_set():
            raise _Interrupt()
        self._report(d, task.id, DONE)
        text = d.report(ctx) if d.report is not None else None
        if not self._final_messages(d, task, text, outcome=DONE):
            raise _Interrupt()
        self.tasks.complete(task.id, self._worker)
        return RunResult(task.id, d.id, OUT_DONE)

    def _final_messages(self, d: Responsibility, task: Task, text: str | None, *,
                        outcome: str | None = None) -> bool:
        """Enqueue the final messages. False if the kill flag stopped them."""
        outcome = outcome or task.state
        receipt = receipt_link(task.id)
        try:
            if self._acked(task.id):
                lw = self._long_work(d, task.id)
                if lw is not None:
                    lw.finish(receipt, self._now(), outcome=outcome)
            handle = self._handles[d.id]
            with self.db.tx() as c:
                if outcome == DONE and text:
                    handle.notify(c, task.id, kind="job_result", text=f"{text} Receipt: {receipt}")
                elif outcome == FAILED:
                    handle.notify(c, task.id, kind="alert",
                                  text=f"{d.id} failed. Receipt: {receipt}")
                # ESCALATED: the escalation alert goes through the ops job.
        except Stopped:
            return False
        return True

    def _retry(self, d: Responsibility, task: Task, error: str, outcome: str) -> RunResult:
        try:
            t = self.tasks.fail(task.id, self._worker, error, retryable=True)
        except LeaseLost:
            return RunResult(task.id, d.id, OUT_LEASE_LOST)
        self._report(d, task.id, t.state)
        if t.state in TERMINAL:
            self._final_messages(d, t, None)
        return RunResult(task.id, d.id, OUT_ESCALATED if t.state == ESCALATED else outcome, error)

    def _fatal(self, d: Responsibility, task: Task, error: str) -> RunResult:
        try:
            t = self.tasks.fail(task.id, self._worker, error, retryable=False)
        except LeaseLost:
            return RunResult(task.id, d.id, OUT_LEASE_LOST)
        self._report(d, task.id, t.state)
        self._final_messages(d, t, None)
        return RunResult(task.id, d.id, OUT_ESCALATED if t.state == ESCALATED else OUT_FAILED,
                         error)

    def _interrupt(self, d: Responsibility, task: Task, index: int,
                   approvals: Mapping[str, str]) -> RunResult:
        """Kill flag: record where to resume, release the task back to the
        queue. C2 has no pause transition, so this is a retryable failure."""
        try:
            self.tasks.checkpoint(task.id, self._worker, {"next": index, "approvals":
                                                          dict(approvals), "interrupted": True})
            self.tasks.fail(task.id, self._worker, "interrupted: kill flag set", retryable=True)
        except LeaseLost:
            return RunResult(task.id, d.id, OUT_LEASE_LOST)
        return RunResult(task.id, d.id, OUT_INTERRUPTED, f"resume at step {index}")

    # ---------------------------------------------------------------- long work (E1)
    def _long_work(self, d: Responsibility, task_id: str):
        """A fresh E1 LongWork for the task. E1 reads its own progress (ack,
        update numbering, last state told) back from the outbox on every tick,
        so nothing is replayed here."""
        with self.db.read() as c:
            row = _schema.load_progress(c, task_id)
        return None if row is None else self._handles[d.id].long_work(task_id, row[0])

    def _acked(self, task_id: str) -> bool:
        with self.db.read() as c:
            row = _schema.load_progress(c, task_id)
        return bool(row) and any(at - row[0] >= LONG_WORK_ACK_S for _, at in row[1])

    def _report(self, d: Responsibility, task_id: str, state: str) -> None:
        """Hand a T16 state to E1's LongWork. The first report pins the run's
        start time, and every tick is logged here, so the ack timing survives a
        restart. E1 makes repeated ticks idempotent."""
        if self._handles[d.id].silent or self.kill.is_set():
            return
        now = self._now()
        with self.db.tx() as c:
            row = _schema.load_progress(c, task_id)
            started, history = row if row else (now, [])
            last = history[-1][0] if history else None
            acked = any(at - started >= LONG_WORK_ACK_S for _, at in history)
            if state == last and (acked or now - started < LONG_WORK_ACK_S):
                return
            history.append([state, now])
            _schema.save_progress(c, task_id, started, history)
        try:
            self._handles[d.id].long_work(task_id, started).tick(state, now)
        except NotifierError:
            pass    # Stopped, or the work already has its final message

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
        for eff in self.journal.unresolved():
            task = self.tasks.get(eff.task_id)
            if task is None or task.kind not in self._defs or eff.status != UNKNOWN:
                continue
            conn = self._connectors.get(eff.connector or "")
            try:
                if conn is not None:
                    eff = self.journal.reconcile(eff.idempotency_key, conn.reconcile)
            except EffectInFlight:
                pass
            if eff.status == UNKNOWN:
                left.append(eff)
        return left
