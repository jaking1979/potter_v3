"""Durable task store (package C2, decision T10).

* Dedupe: each task has a unique ``trigger_key``; its id is UUIDv5 of that key.
  Submitting the same trigger again (from any thread or process) returns the
  existing task and creates nothing.
* State machine: queued -> running -> (waiting_approval -> queued) ->
  done | failed | escalated. Illegal transitions raise InvalidTransition.
* Leases: a worker claims a task for ``lease_s`` seconds and must heartbeat.
  Every mutation by a worker re-checks, inside the write transaction, that it
  still holds an unexpired lease (LeaseLost otherwise). A task whose lease
  expired (worker killed) is reclaimed by the next ``claim`` and resumes from
  its persisted checkpoint; its effects are protected by the effect journal.
* Release: the live lease holder can hand a running task back to ``queued``
  without recording a failure (``release``): a pause such as a kill flag.
* Caller transactions: ``submit`` and ``complete`` accept ``tx=``, the
  caller's open ``Database.tx()`` transaction, so their writes commit or roll
  back with the caller's own. Time is still sampled under the write lock, all
  lease checks still run, and nothing is committed or rolled back for the caller.
* Escalation: ``escalate_after`` (3) consecutive failures of a task - an
  explicit ``fail`` or an expired lease - move it to ``escalated`` and fire the
  ``escalate`` callback once. A scheduled job escalates after 3 consecutive
  runs that end failed/escalated (including a denied or expired approval); a
  successful run resets it.
* Watchdog: ``stale_jobs`` lists scheduled jobs whose last success (or, if none,
  registration) is older than cadence x tolerance.

The escalate callback runs after the state change is committed, at most once
per escalation. A crash between commit and callback loses the notification
(the state is still ``escalated``); durable delivery is C3's outbox.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import sqlite3
import time
import uuid
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .db import Database, as_database

TASK_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:potter:v3:c2:task-store")

QUEUED, RUNNING, WAITING_APPROVAL = "queued", "running", "waiting_approval"
DONE, FAILED, ESCALATED = "done", "failed", "escalated"
STATES = (QUEUED, RUNNING, WAITING_APPROVAL, DONE, FAILED, ESCALATED)
TERMINAL = frozenset({DONE, FAILED, ESCALATED})
TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({RUNNING}),
    # RUNNING -> RUNNING is a reclaim after lease expiry; -> QUEUED is a retry.
    RUNNING: frozenset({RUNNING, QUEUED, WAITING_APPROVAL, DONE, FAILED, ESCALATED}),
    WAITING_APPROVAL: frozenset({QUEUED, FAILED}),
    DONE: frozenset(), FAILED: frozenset(), ESCALATED: frozenset(),
}
ESCALATE_AFTER = 3
# Bounds on operator-supplied durations. Values outside them (NaN, +/-inf,
# zero, negative, absurdly large) are rejected before any write.
MAX_CADENCE_S = 10 * 366 * 86_400.0     # ten years
MAX_TOLERANCE = 1_000.0
MAX_LEASE_S = 86_400.0                  # one day; heartbeat to hold longer


def _finite_number(name: str, v: Any, *, positive: bool = True, upper: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{name} must be a number")
    v = float(v)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite, got {v!r}")
    if positive and v <= 0:
        raise ValueError(f"{name} must be > 0, got {v!r}")
    if upper is not None and v > upper:
        raise ValueError(f"{name} must be <= {upper}, got {v!r}")
    return v


class TaskError(Exception):
    pass


class InvalidTransition(TaskError):
    pass


class LeaseLost(TaskError):
    """The caller no longer holds the task's lease; it must stop working on it."""


@dataclass(frozen=True)
class Task:
    id: str
    trigger_key: str
    kind: str
    job: str | None
    payload: Any
    state: str
    attempts: int
    consecutive_failures: int
    lease_owner: str | None
    lease_expires_at: float | None
    checkpoint: Any
    last_error: str | None
    updated_at: float | None = None   # journal time of the last write (rollback check)


@dataclass(frozen=True)
class Job:
    name: str
    cadence_s: float
    tolerance: float
    registered_at: float
    last_success_at: float | None
    consecutive_failures: int
    escalated_at: float | None

    fault: str | None = None   # set when the row (or the evaluation clock) is not valid

    def age(self, now: float) -> float:
        return now - (self.last_success_at if self.last_success_at is not None else self.registered_at)


def _job_fault(j: Job) -> str | None:
    """Why a persisted job row cannot be evaluated (e.g. written before
    validation existed, or edited directly), or None."""
    for name, v in (("cadence_s", j.cadence_s), ("tolerance", j.tolerance),
                    ("registered_at", j.registered_at), ("last_success_at", j.last_success_at)):
        if v is None and name == "last_success_at":
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return f"{name} is not finite: {v!r}"
    if not math.isfinite(j.cadence_s * j.tolerance):
        return "cadence_s x tolerance overflows"
    # The same limits registration enforces: a finite but over-limit row
    # (e.g. written before validation, or edited) is not silently trusted.
    if not (0 < j.cadence_s <= MAX_CADENCE_S) or not (1 <= j.tolerance <= MAX_TOLERANCE):
        return "cadence_s/tolerance out of range"
    return None


@dataclass(frozen=True)
class Escalation:
    subject: str            # "task" | "job"
    id: str                 # task id or job name
    failures: int
    last_error: str | None
    task_id: str            # the task whose failure triggered it


_TCOLS = ("id, trigger_key, kind, job, payload, state, attempts, consecutive_failures,"
          " lease_owner, lease_expires_at, checkpoint, last_error, updated_at")
_JCOLS = "name, cadence_s, tolerance, registered_at, last_success_at, consecutive_failures, escalated_at"


def task_id_for(trigger_key: str) -> str:
    return str(uuid.uuid5(TASK_NAMESPACE, trigger_key))


def _dump(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _task(row: tuple) -> Task:
    r = list(row)
    r[4] = json.loads(r[4])
    r[10] = json.loads(r[10]) if r[10] is not None else None
    return Task(*r)


def _load(c: sqlite3.Connection, task_id: str) -> Task | None:
    row = c.execute(f"SELECT {_TCOLS} FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _task(row) if row else None


def _check(src: str, dst: str) -> None:
    if dst not in TRANSITIONS[src]:
        raise InvalidTransition(f"{src} -> {dst} is not allowed")


class TaskStore:
    def __init__(self, db: Database | str | Path, *,
                 escalate: Callable[[Escalation], None] | None = None,
                 escalate_after: int = ESCALATE_AFTER,
                 clock: Callable[[], float] = time.time):
        if escalate_after < 1:
            raise ValueError("escalate_after must be >= 1")
        self.db = as_database(db)
        self._escalate = escalate or (lambda e: None)   # seam; default no-op
        self._after = escalate_after
        self._clock = clock

    def _now(self, now: float | None) -> float:
        """The clock reading for a write. A non-finite reading (from the clock
        or passed explicitly) is refused before anything is written."""
        t = self._clock() if now is None else now
        return _finite_number("clock reading", t, positive=False)

    @staticmethod
    def _lease(lease_s: Any) -> float:
        return _finite_number("lease_s", lease_s, upper=MAX_LEASE_S)

    # -- jobs --------------------------------------------------------------
    def register_job(self, name: str, cadence_s: float, *, tolerance: float = 1.5,
                     now: float | None = None) -> Job:
        """Declare a scheduled job's cadence. Idempotent; re-registering updates
        cadence/tolerance but keeps history. Both must be finite, positive and
        bounded (and their product finite); an invalid update leaves the row
        unchanged."""
        cadence_s = _finite_number("cadence_s", cadence_s, upper=MAX_CADENCE_S)
        tolerance = _finite_number("tolerance", tolerance, upper=MAX_TOLERANCE)
        if tolerance < 1:
            raise ValueError("tolerance must be >= 1")
        if not math.isfinite(cadence_s * tolerance):
            raise ValueError("cadence_s x tolerance overflows")
        with self.db.tx() as c:
            now = self._now(now)   # sampled under the write lock, not before it
            c.execute("INSERT INTO jobs (name, cadence_s, tolerance, registered_at) VALUES (?,?,?,?)"
                      " ON CONFLICT(name) DO UPDATE SET cadence_s=excluded.cadence_s,"
                      " tolerance=excluded.tolerance", (name, cadence_s, tolerance, now))
            return Job(*c.execute(f"SELECT {_JCOLS} FROM jobs WHERE name=?", (name,)).fetchone())

    def job(self, name: str) -> Job | None:
        with self.db.read() as c:
            row = c.execute(f"SELECT {_JCOLS} FROM jobs WHERE name=?", (name,)).fetchone()
        return Job(*row) if row else None

    def stale_jobs(self, now: float | None = None, max_age: float | None = None) -> list[Job]:
        """Jobs whose last success (or registration, if never) is older than
        cadence x tolerance; ``max_age``, if given, caps that threshold.

        Fail visible: a job whose persisted row cannot be evaluated (non-finite
        or out-of-range values) is returned with ``fault`` set, and every other
        job is still evaluated. If the evaluation clock itself is not finite,
        no age can be computed, so every job is returned with ``fault`` set."""
        t = self._clock() if now is None else now
        clock_ok = (not isinstance(t, bool) and isinstance(t, (int, float)) and math.isfinite(t))
        if max_age is not None:
            max_age = _finite_number("max_age", max_age)
        with self.db.read() as c:
            jobs = [Job(*r) for r in c.execute(f"SELECT {_JCOLS} FROM jobs ORDER BY name")]
        out = []
        for j in jobs:
            fault = None if clock_ok else f"evaluation clock is not finite: {t!r}"
            fault = fault or _job_fault(j)
            if fault:
                out.append(dataclasses.replace(j, fault=fault))
                continue
            limit = j.cadence_s * j.tolerance
            if max_age is not None:
                limit = min(limit, max_age)
            if j.age(t) > limit:
                out.append(j)
        return out

    # -- submission / claiming --------------------------------------------
    def submit(self, trigger_key: str, kind: str, payload: Any = None, *,
               job: str | None = None, now: float | None = None,
               tx: sqlite3.Connection | None = None) -> tuple[Task, bool]:
        """Create the task for ``trigger_key`` unless it exists. Returns
        (task, created). Payloads are JSON and must never contain secrets.
        ``tx``: the caller's open write transaction (see ``_in``), so the insert
        commits or rolls back with the caller's own writes."""
        if not trigger_key or not kind:
            raise ValueError("trigger_key and kind required")
        tid = task_id_for(trigger_key)
        with self._in(tx) as c:
            now = self._now(now)   # sampled under the write lock, not before it
            cur = c.execute("INSERT OR IGNORE INTO tasks (id, trigger_key, kind, job, payload, state,"
                            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                            (tid, trigger_key, kind, job, _dump(payload), QUEUED, now, now))
            return _load(c, tid), cur.rowcount == 1

    def claim(self, worker_id: str, lease_s: float, *, now: float | None = None) -> Task | None:
        """Lease the oldest runnable task: queued, or running with an expired
        lease (its worker died). Reclaiming counts as a failure, so a task that
        keeps killing its worker escalates instead of looping forever.

        A persisted lease that is not a finite time within MAX_LEASE_S of now
        (e.g. +inf written before validation existed; NaN is stored as NULL) is
        treated as EXPIRED: the task is reclaimable rather than stuck forever."""
        if not worker_id:
            raise ValueError("worker_id required")
        lease_s = self._lease(lease_s)
        fired: list[Escalation] = []
        claimed = None
        with self.db.tx() as c:
            now = self._now(now)   # sampled under the write lock, not before it
            while claimed is None:
                row = c.execute(
                    "SELECT id, state FROM tasks WHERE state=? OR (state=? AND (lease_expires_at IS NULL"
                    " OR lease_expires_at<=? OR lease_expires_at>?))"
                    " ORDER BY created_at, id LIMIT 1",
                    (QUEUED, RUNNING, now, now + MAX_LEASE_S)).fetchone()
                if row is None:
                    break
                tid, state = row
                if state == RUNNING:   # expired lease: the previous worker is presumed dead
                    t = _load(c, tid)
                    if self._record_failure(c, t, f"lease of {t.lease_owner} expired",
                                            retryable=True, now=now, fired=fired) != QUEUED:
                        continue
                c.execute("UPDATE tasks SET state=?, lease_owner=?, lease_expires_at=?,"
                          " attempts=attempts+1, updated_at=? WHERE id=?",
                          (RUNNING, worker_id, now + lease_s, now, tid))
                claimed = _load(c, tid)
        self._fire(fired)   # only reached once the transaction committed
        return claimed

    # -- lease-holder operations ------------------------------------------
    def _owned(self, c: sqlite3.Connection, task_id: str, worker_id: str, now: float) -> Task:
        t = _load(c, task_id)
        if t is None:
            raise KeyError(task_id)
        exp = t.lease_expires_at
        live = (isinstance(exp, (int, float)) and math.isfinite(exp)
                and now < exp <= now + MAX_LEASE_S)
        if t.updated_at is not None and now < t.updated_at:
            # The clock reads earlier than this task's last write: a rolled-back
            # clock could make an expired lease look live, so refuse.
            raise LeaseLost(f"clock rollback: {now} is before {task_id}'s last update {t.updated_at}")
        if t.state != RUNNING or t.lease_owner != worker_id or not live:
            raise LeaseLost(f"{worker_id} does not hold a live lease on {task_id} (state {t.state})")
        return t

    def heartbeat(self, task_id: str, worker_id: str, lease_s: float, *,
                  now: float | None = None) -> Task:
        lease_s = self._lease(lease_s)        # validated before any write
        with self.db.tx() as c:
            now = self._now(now)   # sampled under the write lock, not before it
            self._owned(c, task_id, worker_id, now)
            c.execute("UPDATE tasks SET lease_expires_at=?, updated_at=? WHERE id=?",
                      (now + lease_s, now, task_id))
            return _load(c, task_id)

    def checkpoint(self, task_id: str, worker_id: str, data: Any, *,
                   now: float | None = None) -> Task:
        """Persist progress so a resumed worker continues from here."""
        with self.db.tx() as c:
            now = self._now(now)   # sampled under the write lock, not before it
            self._owned(c, task_id, worker_id, now)
            c.execute("UPDATE tasks SET checkpoint=?, updated_at=? WHERE id=?",
                      (_dump(data), now, task_id))
            return _load(c, task_id)

    def wait_for_approval(self, task_id: str, worker_id: str, *, now: float | None = None) -> Task:
        with self.db.tx() as c:
            now = self._now(now)   # sampled under the write lock, not before it
            self._owned(c, task_id, worker_id, now)
            return self._move(c, task_id, RUNNING, WAITING_APPROVAL, now)

    def approval_resolved(self, task_id: str, approved: bool, *, error: str | None = None,
                          now: float | None = None) -> Task:
        """Approved -> back to queued to resume; denied/expired -> failed. A
        denied or expired approval ends the run, so it counts toward the job's
        consecutive failed runs (and job escalation) like any terminal failure."""
        fired: list[Escalation] = []
        with self.db.tx() as c:
            now = self._now(now)   # sampled under the write lock, not before it
            t = _load(c, task_id)
            if t is None:
                raise KeyError(task_id)
            if t.state != WAITING_APPROVAL:
                raise InvalidTransition(f"{task_id} is {t.state}, not waiting for approval")
            if approved:
                out = self._move(c, task_id, WAITING_APPROVAL, QUEUED, now)
            else:
                self._record_failure(c, t, error or "approval denied", retryable=False,
                                     now=now, fired=fired, final=FAILED)
                out = _load(c, task_id)
        self._fire(fired)   # only reached once the transaction committed
        return out

    def release(self, task_id: str, worker_id: str, *, now: float | None = None) -> Task:
        """Hand a running task back to ``queued`` WITHOUT recording a failure:
        a pause (e.g. a kill flag), not an error. Only the live lease holder
        may release: the lease (owner, expiry, clock rollback) is checked inside
        the write transaction, with time sampled under the lock. Task and job
        failure counters are untouched; the next claim resumes from the
        persisted checkpoint."""
        with self.db.tx() as c:
            now = self._now(now)   # sampled under the write lock, not before it
            self._owned(c, task_id, worker_id, now)
            return self._move(c, task_id, RUNNING, QUEUED, now)

    def complete(self, task_id: str, worker_id: str, *, now: float | None = None,
                 tx: sqlite3.Connection | None = None) -> Task:
        """``tx``: the caller's open write transaction (see ``_in``), so the
        terminal state commits atomically with the caller's own writes (e.g. a
        finals row). The lease is checked inside it, at lock time."""
        with self._in(tx) as c:
            now = self._now(now)   # sampled under the write lock, not before it
            t = self._owned(c, task_id, worker_id, now)
            out = self._move(c, task_id, RUNNING, DONE, now)
            c.execute("UPDATE tasks SET consecutive_failures=0 WHERE id=?", (task_id,))
            if t.job is not None:
                c.execute("UPDATE jobs SET last_success_at=?, consecutive_failures=0,"
                          " escalated_at=NULL WHERE name=?", (now, t.job))
            return out

    def fail(self, task_id: str, worker_id: str, error: str, *, retryable: bool = True,
             now: float | None = None) -> Task:
        """Record a failed attempt. Retryable -> queued again, until
        ``escalate_after`` consecutive failures -> escalated. Not retryable ->
        failed."""
        fired: list[Escalation] = []
        with self.db.tx() as c:
            now = self._now(now)   # sampled under the write lock, not before it
            t = self._owned(c, task_id, worker_id, now)
            self._record_failure(c, t, error, retryable=retryable, now=now, fired=fired)
            out = _load(c, task_id)
        self._fire(fired)   # only reached once the transaction committed
        return out

    def get(self, task_id: str) -> Task | None:
        with self.db.read() as c:
            return _load(c, task_id)

    def by_trigger(self, trigger_key: str) -> Task | None:
        return self.get(task_id_for(trigger_key))

    # -- internals ---------------------------------------------------------
    def _in(self, tx: sqlite3.Connection | None):
        """The caller's open write transaction, or a fresh one of our own.

        A caller-supplied ``tx`` must be an open transaction on THIS database
        (normally ``Database.tx()``, i.e. BEGIN IMMEDIATE). Before anything is
        read or the clock is sampled, a no-op write makes sure the write lock is
        held: with BEGIN IMMEDIATE it already is; a deferred transaction takes
        it here, or fails if its snapshot is stale. The caller owns the
        transaction: nothing is committed or rolled back on its behalf, and any
        exception propagates so the caller's own writes roll back with ours."""
        if tx is None:
            return self.db.tx()
        if not isinstance(tx, sqlite3.Connection) or not tx.in_transaction:
            raise ValueError("tx must be an open write transaction (Database.tx())")
        files = {os.path.realpath(r[2]) for r in tx.execute("PRAGMA database_list") if r[1] == "main"}
        if files != {os.path.realpath(self.db.path)}:
            raise ValueError("tx belongs to a different database")
        tx.execute("UPDATE meta SET value=value WHERE key='schema'")   # hold the write lock now
        return contextlib.nullcontext(tx)


    def _move(self, c: sqlite3.Connection, task_id: str, src: str, dst: str, now: float,
              error: str | None = None) -> Task:
        _check(src, dst)
        keep_lease = dst == RUNNING
        cur = c.execute("UPDATE tasks SET state=?, updated_at=?, last_error=COALESCE(?, last_error),"
                        " lease_owner=CASE WHEN ? THEN lease_owner END,"
                        " lease_expires_at=CASE WHEN ? THEN lease_expires_at END"
                        " WHERE id=? AND state=?",
                        (dst, now, error, keep_lease, keep_lease, task_id, src))
        if cur.rowcount != 1:
            raise InvalidTransition(f"{task_id} is not in state {src}")
        return _load(c, task_id)

    def _record_failure(self, c: sqlite3.Connection, t: Task, error: str, *, retryable: bool,
                        now: float, fired: list[Escalation], final: str | None = None) -> str:
        """``final`` forces the task's end state (a denied approval is a human
        decision: the task fails, it is not escalated) while job accounting
        still applies."""
        failures = t.consecutive_failures + 1
        if final is not None:
            dst = final
        else:
            dst = ESCALATED if failures >= self._after else (QUEUED if retryable else FAILED)
        self._move(c, t.id, t.state, dst, now, error=error)
        c.execute("UPDATE tasks SET consecutive_failures=? WHERE id=?", (failures, t.id))
        if dst == ESCALATED:
            fired.append(Escalation("task", t.id, failures, error, t.id))
        if dst in TERMINAL and t.job is not None:
            c.execute("UPDATE jobs SET consecutive_failures=consecutive_failures+1 WHERE name=?",
                      (t.job,))
            jf, esc = c.execute("SELECT consecutive_failures, escalated_at FROM jobs WHERE name=?",
                                (t.job,)).fetchone()
            if jf >= self._after and esc is None:
                c.execute("UPDATE jobs SET escalated_at=? WHERE name=?", (now, t.job))
                fired.append(Escalation("job", t.job, jf, error, t.id))
        return dst

    def _fire(self, fired: list[Escalation]) -> None:
        for e in fired:
            self._escalate(e)
