"""C2 merge evidence (decision T10): kill mid-task resumes; duplicate trigger
deduped; unknown-outcome effect reconciles before retry; 3 failures escalate;
last-success watchdog flags stale job; C0 approval conformance suite passes
against the production JournalApprovalLedger.

Offline only: the "external system" is a local SQLite file standing in for a
connector that deduplicates on the idempotency key and counts applications.
"""
import contextlib
import itertools
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_approval_contract as tac  # noqa: E402  (C0 conformance suite)
from runtime import (DONE, ESCALATED, FAILED, QUEUED, RUNNING, WAITING_APPROVAL,  # noqa: E402
                     Database, EffectConflict, EffectFailed, EffectInFlight, EffectJournal,
                     EffectOutcomeUnknown, InvalidTransition, JournalApprovalLedger, LeaseLost,
                     TaskStore, idempotency_key, task_id_for)
from runtime._contract import ApprovalRefused, BoundAction  # noqa: E402

T = 1_800_000_000.0
LEASE = 30.0
SPAWN = multiprocessing.get_context("spawn")


def action(task_id, n=1):
    return BoundAction(task_id, "test.fake", "write", "fake://external", {"n": n})


# -- fake external system ---------------------------------------------------
@contextlib.contextmanager
def _sql(path):
    c = sqlite3.connect(path, timeout=30)
    try:
        with c:
            yield c
    finally:
        c.close()


class External:
    """Counts how many times each idempotency key was applied (no dedupe on
    purpose, so any double execution is visible)."""

    def __init__(self, path):
        self.path = str(path)
        with _sql(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS applied (key TEXT PRIMARY KEY, n INTEGER NOT NULL)")

    def apply(self, key):
        with _sql(self.path) as c:
            c.execute("INSERT INTO applied VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET n = n + 1", (key,))

    def count(self, key=None):
        with _sql(self.path) as c:
            if key is None:
                return c.execute("SELECT COALESCE(SUM(n), 0) FROM applied").fetchone()[0]
            row = c.execute("SELECT n FROM applied WHERE key=?", (key,)).fetchone()
            return row[0] if row else 0

    def perform(self, effect):
        effect.action()                      # executors run only the journaled action
        self.apply(effect.idempotency_key)
        return {"applied": effect.idempotency_key}

    def reconcile(self, effect):
        return "succeeded" if self.count(effect.idempotency_key) else "failed"


# -- spawned workers (module level so `spawn` can import them) ---------------
def _worker_dies_after_effect(db, ext, ready):
    store, journal, x = TaskStore(db), EffectJournal(db), External(ext)
    t = store.claim("w1", LEASE, now=T)
    eff = journal.record_intent(t.id, "send", action(t.id), now=T)
    journal.execute(eff.idempotency_key, x.perform, x.reconcile, owner="w1", now=T)
    store.checkpoint(t.id, "w1", {"step": "sent"}, now=T)
    ready.set()
    time.sleep(600)                          # "mid-task": killed here before complete()


def _worker_dies_mid_dispatch(db, ext, ready, applied_before_death):
    store, x = TaskStore(db), External(ext)
    journal = EffectJournal(db, dispatch_timeout_s=20)
    t = store.claim("w1", LEASE, now=T)
    eff = journal.record_intent(t.id, "send", action(t.id), now=T)

    def perform(effect):
        if applied_before_death:
            x.apply(effect.idempotency_key)  # external write lands...
        ready.set()
        time.sleep(600)                      # ...and the worker dies before recording it

    journal.execute(eff.idempotency_key, perform, x.reconcile, owner="w1", now=T)


def _submit_worker(db, barrier, q):
    store = TaskStore(db)
    barrier.wait(30)
    task, created = store.submit("cron:digest:2026-11-06", "digest", {"day": "2026-11-06"}, now=T)
    q.put((task.id, created))


def _journal_race_worker(db, q):
    try:
        tac.verify(tac.record(), tac.ACTION, tac.NOW, JournalApprovalLedger(db), tac.AUTH,
                   f"eff-{os.getpid()}")
        q.put(True)
    except tac.ApprovalRefused:
        q.put(False)


class _TmpDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "potter.sqlite")
        self.ext = External(os.path.join(self.tmp.name, "external.sqlite"))
        Database(self.db)
        self.escalations = []
        self.store = TaskStore(self.db, escalate=self.escalations.append)
        self.journal = EffectJournal(self.db)


# -- kill mid-task -----------------------------------------------------------
class KillMidTask(_TmpDB):
    def _run_and_kill(self, target, *args):
        task, _ = self.store.submit("tg:msg:1", "reply", {"chat": 1}, now=T)
        ready = SPAWN.Event()
        p = SPAWN.Process(target=target, args=(self.db, self.ext.path, ready, *args))
        p.start()
        self.assertTrue(ready.wait(60), "worker never reached the kill point")
        p.kill()
        p.join(30)
        self.assertNotEqual(p.exitcode, 0)
        return task

    def test_killed_after_effect_resumes_without_reexecuting(self):
        task = self._run_and_kill(_worker_dies_after_effect)
        t = self.store.get(task.id)
        self.assertEqual((t.state, t.lease_owner), (RUNNING, "w1"))
        self.assertIsNone(self.store.claim("w2", LEASE, now=T + 10))   # lease still live
        resumed = self.store.claim("w2", LEASE, now=T + LEASE + 1)     # lease expired
        self.assertEqual(resumed.id, task.id)
        self.assertEqual(resumed.checkpoint, {"step": "sent"})
        self.assertEqual(resumed.attempts, 2)
        # The resumed worker re-derives the same step and key from persisted state.
        eff = self.journal.record_intent(task.id, "send", action(task.id), now=T + 32)
        self.assertEqual(eff.status, "succeeded")
        calls = []
        out = self.journal.execute(eff.idempotency_key, lambda e: calls.append(e),
                                   self.ext.reconcile, owner="w2", now=T + 32)
        self.assertEqual((out.status, calls, out.dispatches), ("succeeded", [], 1))
        self.store.complete(task.id, "w2", now=T + 33)
        self.assertEqual(self.store.get(task.id).state, DONE)
        self.assertEqual(self.ext.count(), 1)

    def test_killed_mid_dispatch_after_write_reconciles_succeeded(self):
        task = self._run_and_kill(_worker_dies_mid_dispatch, True)
        eff = self.journal.for_step(task.id, "send")
        self.assertEqual(eff.status, "unknown")       # never left as "pending"
        resumed = self.store.claim("w2", LEASE, now=T + LEASE + 1)
        self.assertEqual(resumed.id, task.id)
        calls = []
        out = self.journal.execute(eff.idempotency_key, lambda e: calls.append(e),
                                   self.ext.reconcile, owner="w2", now=T + LEASE + 1)
        self.assertEqual((out.status, calls), ("succeeded", []))
        self.assertEqual(self.ext.count(eff.idempotency_key), 1)

    def test_killed_mid_dispatch_before_write_retries_once_same_key(self):
        task = self._run_and_kill(_worker_dies_mid_dispatch, False)
        eff = self.journal.for_step(task.id, "send")
        self.assertEqual((eff.status, self.ext.count()), ("unknown", 0))
        self.store.claim("w2", LEASE, now=T + LEASE + 1)
        out = self.journal.execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                   owner="w2", now=T + LEASE + 1)
        self.assertEqual((out.status, out.dispatches), ("succeeded", 2))
        self.assertEqual(out.idempotency_key, eff.idempotency_key)
        self.assertEqual(self.ext.count(eff.idempotency_key), 1)

    def test_dead_dispatcher_in_flight_window_refuses_reconcile(self):
        self._run_and_kill(_worker_dies_mid_dispatch, True)
        eff = self.journal.unresolved()[0]
        with self.assertRaises(EffectInFlight):     # dispatch timeout (20 s) not yet over
            self.journal.execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                 owner="w2", now=T + 5)
        self.assertEqual(self.ext.count(), 1)


# -- duplicate triggers ------------------------------------------------------
class DuplicateTrigger(_TmpDB):
    def test_same_trigger_key_is_one_task(self):
        a, created_a = self.store.submit("cron:digest:2026-11-06", "digest", {"x": 1}, now=T)
        b, created_b = self.store.submit("cron:digest:2026-11-06", "digest", {"x": 2}, now=T + 1)
        self.assertEqual((created_a, created_b), (True, False))
        self.assertEqual(a.id, b.id)
        self.assertEqual(b.payload, {"x": 1})        # the duplicate changes nothing
        self.assertEqual(a.id, task_id_for("cron:digest:2026-11-06"))

    def test_eight_concurrent_processes_create_exactly_one_task(self):
        barrier, q = SPAWN.Barrier(8), SPAWN.Queue()
        procs = [SPAWN.Process(target=_submit_worker, args=(self.db, barrier, q)) for _ in range(8)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        results = [q.get(timeout=5) for _ in procs]
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({tid for tid, _ in results}), 1)
        with _sql(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)


# -- effect journal: unknown outcomes ---------------------------------------
class UnknownOutcome(_TmpDB):
    def setUp(self):
        super().setUp()
        self.task, _ = self.store.submit("tg:msg:2", "reply", now=T)
        self.eff = self.journal.record_intent(self.task.id, "send", action(self.task.id), now=T)
        self.key = self.eff.idempotency_key

    def _make_unknown(self, applied):
        def timeout(e):
            if applied:                      # the write landed but the response was lost
                self.ext.apply(e.idempotency_key)
            raise TimeoutError("connector timed out")
        with self.assertRaises(EffectOutcomeUnknown):
            self.journal.execute(self.key, timeout, self.ext.reconcile, owner="w1", now=T)
        self.assertEqual(self.journal.get(self.key).status, "unknown")

    def _never(self, e):
        self.fail("perform must not be called before reconciliation")

    def test_intent_recorded_before_execution_with_deterministic_key(self):
        self.assertEqual(self.eff.status, "pending")
        self.assertEqual(self.key, idempotency_key(self.task.id, "send", action(self.task.id).hash()))
        seen = []

        def perform(e):
            seen.append(self.journal.get(e.idempotency_key).status)  # what a crash would leave
            return None
        self.journal.execute(self.key, perform, self.ext.reconcile, owner="w1", now=T)
        self.assertEqual(seen, ["unknown"])

    def test_retry_refused_until_reconciled(self):
        self._make_unknown(applied=False)
        for verdict in ("still_unknown", "maybe", None):
            with self.subTest(verdict=verdict), self.assertRaises(EffectOutcomeUnknown):
                self.journal.execute(self.key, self._never, lambda e, v=verdict: v,
                                     owner="w1", now=T + 1)

        def broken(e):
            raise ConnectionError("cannot read external state")
        with self.assertRaises(EffectOutcomeUnknown):
            self.journal.execute(self.key, self._never, broken, owner="w1", now=T + 1)
        self.assertEqual(self.journal.get(self.key).status, "unknown")

    def test_reconcile_succeeded_does_not_reexecute(self):
        self._make_unknown(applied=True)
        out = self.journal.execute(self.key, self._never, self.ext.reconcile, owner="w1", now=T + 1)
        self.assertEqual((out.status, out.dispatches), ("succeeded", 1))
        self.assertEqual(self.ext.count(self.key), 1)

    def test_reconcile_failed_allows_exactly_one_retry_with_same_key(self):
        self._make_unknown(applied=False)
        keys = []

        def perform(e):
            keys.append(e.idempotency_key)
            return self.ext.perform(e)
        out = self.journal.execute(self.key, perform, self.ext.reconcile, owner="w1", now=T + 1)
        self.assertEqual(keys, [self.key])
        self.assertEqual((out.status, out.dispatches), ("succeeded", 2))
        again = self.journal.execute(self.key, perform, self.ext.reconcile, owner="w1", now=T + 2)
        self.assertEqual((again.status, keys), ("succeeded", [self.key]))
        self.assertEqual(self.ext.count(), 1)

    def test_reconcile_without_dispatch(self):
        self._make_unknown(applied=True)
        self.assertEqual(self.journal.reconcile(self.key, self.ext.reconcile, now=T + 1).status,
                         "succeeded")
        self.assertEqual(self.journal.unresolved(), [])

    def test_definite_failure_retries_with_same_key(self):
        def reject(e):
            raise EffectFailed("422 invalid recipient")
        out = self.journal.execute(self.key, reject, self.ext.reconcile, owner="w1", now=T)
        self.assertEqual(out.status, "failed")
        out = self.journal.execute(self.key, self.ext.perform, self.ext.reconcile, owner="w1", now=T + 1)
        self.assertEqual((out.status, out.dispatches, out.idempotency_key), ("succeeded", 2, self.key))

    def test_concurrent_dispatch_refused(self):
        inner = []

        def perform(e):
            with self.assertRaises(EffectInFlight):
                self.journal.execute(self.key, self.ext.perform, self.ext.reconcile,
                                     owner="w2", now=T)
            inner.append(1)
            return self.ext.perform(e)
        self.journal.execute(self.key, perform, self.ext.reconcile, owner="w1", now=T)
        self.assertEqual((inner, self.ext.count()), ([1], 1))

    def test_tampered_journal_action_never_dispatched(self):
        with _sql(self.db) as c:
            c.execute("UPDATE effects SET action_canonical=replace(action_canonical, '1', '9')")
        with self.assertRaisesRegex(ApprovalRefused, "integrity"):
            self.journal.execute(self.key, self._never, self.ext.reconcile, owner="w1", now=T)
        self.assertEqual(self.journal.get(self.key).status, "pending")

    def test_step_cannot_be_rebound_to_another_action(self):
        self.assertEqual(self.journal.record_intent(self.task.id, "send", action(self.task.id)),
                         self.eff)
        with self.assertRaises(EffectConflict):
            self.journal.record_intent(self.task.id, "send", action(self.task.id, n=2))


# -- escalation ---------------------------------------------------------------
class EscalationTests(_TmpDB):
    def test_three_failures_escalate_and_fire_once(self):
        task, _ = self.store.submit("job:x:1", "x", now=T)
        for i in range(3):
            t = self.store.claim("w", LEASE, now=T + i)
            self.assertEqual(t.id, task.id)
            out = self.store.fail(task.id, "w", f"boom {i}", now=T + i)
            self.assertEqual(out.state, QUEUED if i < 2 else ESCALATED)
            self.assertEqual(len(self.escalations), 0 if i < 2 else 1)
        e = self.escalations[0]
        self.assertEqual((e.subject, e.id, e.failures, e.last_error), ("task", task.id, 3, "boom 2"))
        self.assertIsNone(self.store.claim("w", LEASE, now=T + 10))
        with self.assertRaises(LeaseLost):
            self.store.fail(task.id, "w", "again", now=T + 10)
        self.assertEqual(len(self.escalations), 1)

    def test_worker_killing_task_escalates_via_expired_leases(self):
        task, _ = self.store.submit("poison", "x", now=T)
        now = T
        for _ in range(3):
            self.store.claim("w", LEASE, now=now)
            now += LEASE + 1                 # worker dies; lease expires
        self.assertIsNone(self.store.claim("w", LEASE, now=now))
        t = self.store.get(task.id)
        self.assertEqual((t.state, t.consecutive_failures), (ESCALATED, 3))
        self.assertEqual(len(self.escalations), 1)

    def test_job_escalates_after_three_failed_runs_and_resets_on_success(self):
        self.store.register_job("digest", 3600, now=T)
        for run in range(4):
            task, _ = self.store.submit(f"job:digest:{run}", "digest", job="digest", now=T + run)
            self.store.claim("w", LEASE, now=T + run)
            self.store.fail(task.id, "w", "upstream down", retryable=False, now=T + run)
            self.assertEqual(self.store.get(task.id).state, FAILED)
        self.assertEqual([(e.subject, e.id) for e in self.escalations], [("job", "digest")])
        task, _ = self.store.submit("job:digest:ok", "digest", job="digest", now=T + 10)
        self.store.claim("w", LEASE, now=T + 10)
        self.store.complete(task.id, "w", now=T + 10)
        j = self.store.job("digest")
        self.assertEqual((j.consecutive_failures, j.escalated_at, j.last_success_at), (0, None, T + 10))

    def test_default_escalate_is_noop(self):
        store = TaskStore(self.db)
        task, _ = store.submit("noop", "x", now=T)
        for i in range(3):
            store.claim("w", LEASE, now=T + i)
            store.fail(task.id, "w", "x", now=T + i)
        self.assertEqual(store.get(task.id).state, ESCALATED)


# -- state machine / leases ----------------------------------------------------
class StateMachine(_TmpDB):
    def test_approval_wait_and_resume(self):
        task, _ = self.store.submit("a", "send", now=T)
        self.store.claim("w", LEASE, now=T)
        self.assertEqual(self.store.wait_for_approval(task.id, "w", now=T).state, WAITING_APPROVAL)
        self.assertIsNone(self.store.claim("w2", LEASE, now=T + 1000))   # not claimable while waiting
        self.assertEqual(self.store.approval_resolved(task.id, True, now=T + 1).state, QUEUED)
        self.assertEqual(self.store.claim("w2", LEASE, now=T + 2).id, task.id)
        self.store.complete(task.id, "w2", now=T + 3)
        with self.assertRaises(InvalidTransition):
            self.store.approval_resolved(task.id, True, now=T + 4)

    def test_denied_approval_fails_task(self):
        task, _ = self.store.submit("b", "send", now=T)
        self.store.claim("w", LEASE, now=T)
        self.store.wait_for_approval(task.id, "w", now=T)
        t = self.store.approval_resolved(task.id, False, now=T + 1)
        self.assertEqual((t.state, t.last_error), (FAILED, "approval denied"))

    def test_lease_is_checked_inside_every_mutation(self):
        task, _ = self.store.submit("c", "x", now=T)
        self.store.claim("w", LEASE, now=T)
        self.store.heartbeat(task.id, "w", LEASE, now=T + 20)            # extends to T+50
        self.assertIsNone(self.store.claim("w2", LEASE, now=T + 40))
        for op in (lambda: self.store.complete(task.id, "w2", now=T + 41),
                   lambda: self.store.checkpoint(task.id, "w", {}, now=T + 51),
                   lambda: self.store.heartbeat(task.id, "w", LEASE, now=T + 51)):
            with self.assertRaises(LeaseLost):
                op()
        self.assertEqual(self.store.claim("w2", LEASE, now=T + 51).lease_owner, "w2")
        with self.assertRaises(LeaseLost):
            self.store.complete(task.id, "w", now=T + 52)

    def test_durability_settings(self):
        db = Database(self.db)
        with db.read() as c:
            self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(c.execute("PRAGMA synchronous").fetchone()[0], 2)   # FULL

    def test_state_survives_restart(self):
        task, _ = self.store.submit("d", "x", {"k": "v"}, now=T)
        self.store.claim("w", LEASE, now=T)
        self.store.checkpoint(task.id, "w", {"i": 3}, now=T)
        fresh = TaskStore(self.db)                                       # "restart"
        t = fresh.get(task.id)
        self.assertEqual((t.state, t.payload, t.checkpoint), (RUNNING, {"k": "v"}, {"i": 3}))


# -- watchdog ------------------------------------------------------------------
class Watchdog(_TmpDB):
    def _succeed(self, job, at):
        task, _ = self.store.submit(f"job:{job}:{at}", job, job=job, now=at)
        self.store.claim("w", LEASE, now=at)
        self.store.complete(task.id, "w", now=at)

    def test_flags_stale_job_not_fresh_one(self):
        self.store.register_job("fresh", 3600, tolerance=1.5, now=T - 20_000)
        self.store.register_job("stale", 3600, tolerance=1.5, now=T - 20_000)
        self._succeed("stale", T - 6000)
        self._succeed("fresh", T - 100)
        self.assertEqual([j.name for j in self.store.stale_jobs(T)], ["stale"])
        self.assertEqual([j.name for j in self.store.stale_jobs(T + 5000)], ["stale"])
        self.assertEqual([j.name for j in self.store.stale_jobs(T + 5401)], ["fresh", "stale"])

    def test_boundary_and_never_succeeded(self):
        self.store.register_job("new", 600, tolerance=2, now=T)
        self.assertEqual(self.store.stale_jobs(T + 1200), [])
        self.assertEqual([j.name for j in self.store.stale_jobs(T + 1201)], ["new"])

    def test_max_age_caps_threshold(self):
        self.store.register_job("daily", 86_400, tolerance=1.5, now=T)
        self.assertEqual(self.store.stale_jobs(T + 7200), [])
        self.assertEqual([j.name for j in self.store.stale_jobs(T + 7200, max_age=3600)], ["daily"])


# -- C0 approval conformance suite against the production ledger ---------------
_PATCHED = ("MemoryApprovalLedger", "SqliteApprovalLedger", "_race_worker")


def _conformance(base):
    class Conformance(base):
        def setUp(self):
            saved = {n: getattr(tac, n) for n in _PATCHED}
            self.addCleanup(lambda: [setattr(tac, n, v) for n, v in saved.items()])
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            self.c2_ledgers, n = [], itertools.count()

            def fresh():  # stands in for the in-memory ledger: a new durable journal
                led = JournalApprovalLedger(os.path.join(tmp.name, f"ledger-{next(n)}.sqlite"))
                self.c2_ledgers.append(led)
                return led
            tac.MemoryApprovalLedger = fresh
            tac.SqliteApprovalLedger = JournalApprovalLedger
            tac._race_worker = _journal_race_worker
            super().setUp()

    Conformance.__name__ = Conformance.__qualname__ = f"C0Conformance_{base.__name__}"
    return Conformance


for _name in dir(tac):
    _obj = getattr(tac, _name)
    if isinstance(_obj, type) and issubclass(_obj, unittest.TestCase) and _obj.__module__ == tac.__name__:
        _cls = _conformance(_obj)
        globals()[_cls.__name__] = _cls
del _name, _obj


class ConformanceHarnessIsLive(unittest.TestCase):
    """The patched suite really runs against JournalApprovalLedger."""

    def test_patched_suite_uses_journal(self):
        case = C0Conformance_ApprovalContract("test_replay_refused")  # noqa: F821
        result = unittest.TestResult()
        seen = []
        orig = case.setUp

        def spy():
            orig()
            case.addCleanup(lambda: seen.extend(
                (type(led), "a" * 32 in led, led.effect("a" * 32).status) for led in case.c2_ledgers))
        case.setUp = spy
        case.run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual(seen, [(JournalApprovalLedger, True, "pending")])
        self.assertIs(tac.MemoryApprovalLedger, sys.modules["approval"].MemoryApprovalLedger)  # restored

    def test_consume_and_intent_are_one_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "j.sqlite")
            led = JournalApprovalLedger(db)
            intent = tac.verify(tac.record(), tac.ACTION, tac.NOW, led, tac.AUTH, "eff-1")
            journal = EffectJournal(db)
            [eff] = journal.unresolved()
            self.assertEqual(eff.intent(), intent)
            self.assertEqual(eff.approval_id, "appr-0001")
            # Same step cannot be bound to a second approval, and a refused
            # consume leaves no orphan nonce.
            other = tac.record(nonce="b" * 32)
            with self.assertRaisesRegex(ApprovalRefused, "replay"):
                tac.verify(other, tac.ACTION, tac.NOW, led, tac.AUTH, "eff-1")
            self.assertNotIn("b" * 32, led)
            # Executing the approved effect resolves it; pending() then clears.
            ext = External(os.path.join(d, "ext.sqlite"))
            journal.execute(eff.idempotency_key, ext.perform, ext.reconcile, owner="w", now=T)
            self.assertIsNone(led.pending("a" * 32))
            self.assertIn("a" * 32, led)

    def test_tampered_intent_refused_at_consume(self):
        with tempfile.TemporaryDirectory() as d:
            led = JournalApprovalLedger(os.path.join(d, "j.sqlite"))
            good = tac.verify(tac.record(), tac.ACTION, tac.NOW, tac.MemoryApprovalLedger(),
                              tac.AUTH, "eff-1")
            bad = type(good)(good.effect_id, good.task_id, good.action_hash, good.approval_id,
                             good.action_canonical.replace("7pm", "9pm"))
            with self.assertRaisesRegex(ApprovalRefused, "integrity"):
                led.consume("c" * 32, bad)
            self.assertNotIn("c" * 32, led)


if __name__ == "__main__":
    unittest.main()
