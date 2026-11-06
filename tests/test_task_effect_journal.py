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
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_approval_contract as tac  # noqa: E402  (C0 conformance suite)
from runtime import (DONE, ESCALATED, FAILED, QUEUED, RUNNING, WAITING_APPROVAL,  # noqa: E402
                     ConnectorContract, Database, EffectConflict, TrustedIdempotentRegistry, EffectFailed, EffectInFlight, EffectJournal,
                     EffectOutcomeUnknown, InvalidTransition, JournalApprovalLedger, LeaseLost,
                     TaskStore, idempotency_key, task_id_for)
from runtime._contract import ApprovalRefused, BoundAction  # noqa: E402

T = 1_800_000_000.0
CONN = "fake.external"      # plain connector: no idempotency guarantee
DEDUPE = "fake.dedupe"      # connector whose stub provider dedupes on the key
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

    def __init__(self, path, stall=None, stall_after_apply=False):
        self.path = str(path)
        self.stall, self.stall_after_apply = stall, stall_after_apply   # (started, go) events
        with _sql(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS applied (key TEXT PRIMARY KEY, n INTEGER NOT NULL)")

    def _stall(self):
        started, go = self.stall
        started.set()
        if not go.wait(60):                  # killed here, or released to land late
            raise RuntimeError("never released")

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
        if self.stall and not self.stall_after_apply:
            self._stall()
        self.apply(effect.idempotency_key)
        if self.stall and self.stall_after_apply:
            self._stall()
        return {"applied": effect.idempotency_key}

    def reconcile(self, effect):
        return "succeeded" if self.count(effect.idempotency_key) else "failed"


class DedupingExternal(External):
    """A connector whose external system enforces the idempotency key: a key is
    applied at most once however often it is sent. Attempts are counted
    separately so a late duplicate is visible but harmless. Trust in that
    property comes only from a TrustedIdempotentRegistry entry, never from the
    class itself."""

    def __init__(self, path, **kw):
        super().__init__(path, **kw)
        with _sql(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, n INTEGER NOT NULL)")

    def apply(self, key):
        with _sql(self.path) as c:
            c.execute("INSERT INTO attempts VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET n = n + 1", (key,))
            c.execute("INSERT OR IGNORE INTO applied VALUES (?, 1)", (key,))

    def attempts(self, key):
        with _sql(self.path) as c:
            row = c.execute("SELECT n FROM attempts WHERE key=?", (key,)).fetchone()
            return row[0] if row else 0


def trusted(ext, identity=DEDUPE):
    """Configuration-time trust: the reviewed adapter is this stub's perform."""
    return TrustedIdempotentRegistry([ConnectorContract(identity, ext.perform,
                                                        "test: DedupingExternal dedupes on key")])


# -- spawned workers (module level so `spawn` can import them) ---------------
def _worker_dies_after_effect(db, ext, ready):
    store, journal, x = TaskStore(db), EffectJournal(db), External(ext)
    t = store.claim("w1", LEASE, now=T)
    eff = journal.record_intent(t.id, "send", action(t.id), now=T)
    journal.execute(eff.idempotency_key, x.perform, x.reconcile, owner="w1", connector=CONN, now=T)
    store.checkpoint(t.id, "w1", {"step": "sent"}, now=T)
    ready.set()
    time.sleep(600)                          # "mid-task": killed here before complete()


def _worker_dies_mid_dispatch(db, ext, ready, applied_before_death, dedupe=False):
    # The external write lands (or not), then the worker stalls and is killed
    # before it records the outcome.
    stall = (ready, threading.Event())      # never released
    x = (DedupingExternal if dedupe else External)(ext, stall=stall,
                                                    stall_after_apply=applied_before_death)
    store = TaskStore(db)
    journal = EffectJournal(db, dispatch_timeout_s=20,
                            trusted_idempotent=trusted(x) if dedupe else None)
    t = store.claim("w1", LEASE, now=T)
    eff = journal.record_intent(t.id, "send", action(t.id), now=T)
    journal.execute(eff.idempotency_key, x.perform, x.reconcile, owner="w1",
                    connector=DEDUPE if dedupe else CONN, now=T)


def _late_dispatcher(db, ext_path, dedupe, key, started, go, q):
    """Dispatcher A: claims the effect at T, then stalls past its dispatch
    timeout before its external call lands."""
    x = (DedupingExternal if dedupe else External)(ext_path, stall=(started, go))
    journal = EffectJournal(db, dispatch_timeout_s=20,
                            trusted_idempotent=trusted(x) if dedupe else None)
    try:
        q.put(journal.execute(key, x.perform, x.reconcile, owner="A",
                              connector=DEDUPE if dedupe else CONN, now=T).status)
    except Exception as exc:                 # pragma: no cover - reported to the parent
        q.put(repr(exc))


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
                                   self.ext.reconcile, owner="w2", connector=CONN, now=T + 32)
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
                                   self.ext.reconcile, owner="w2", connector=CONN, now=T + LEASE + 1)
        self.assertEqual((out.status, calls), ("succeeded", []))
        self.assertEqual(self.ext.count(eff.idempotency_key), 1)

    def test_killed_mid_dispatch_before_write_stays_unresolved_by_default(self):
        task = self._run_and_kill(_worker_dies_mid_dispatch, False)
        eff = self.journal.for_step(task.id, "send")
        self.assertEqual((eff.status, self.ext.count()), ("unknown", 0))
        self.store.claim("w2", LEASE, now=T + LEASE + 1)
        # The expired claim is not permission: "not applied yet" may still land.
        with self.assertRaisesRegex(EffectOutcomeUnknown, "fence"):
            self.journal.execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                 owner="w2", connector=CONN, now=T + LEASE + 1)
        self.assertEqual(self.ext.count(), 0)
        self.assertEqual([e.idempotency_key for e in self.journal.unresolved()], [eff.idempotency_key])

    def test_killed_mid_dispatch_before_write_retries_once_same_key_with_fence(self):
        task = self._run_and_kill(_worker_dies_mid_dispatch, False)
        eff = self.journal.for_step(task.id, "send")
        fenced = EffectJournal(self.db, fence=lambda e: e.in_flight_owner == "w1")
        out = fenced.execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                             owner="w2", connector=CONN, now=T + LEASE + 1)
        self.assertEqual((out.status, out.dispatches, out.idempotency_key),
                         ("succeeded", 2, eff.idempotency_key))
        self.assertEqual(self.ext.count(eff.idempotency_key), 1)

    def test_killed_mid_dispatch_before_write_retries_once_same_key_via_trusted_registry(self):
        task = self._run_and_kill(_worker_dies_mid_dispatch, False, True)
        eff = self.journal.for_step(task.id, "send")
        self.assertEqual((eff.status, eff.connector), ("unknown", DEDUPE))
        dedupe = DedupingExternal(self.ext.path)
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):   # no registry: refused
            self.journal.execute(eff.idempotency_key, dedupe.perform, dedupe.reconcile,
                                 owner="w2", connector=DEDUPE, now=T + LEASE + 1)
        journal = EffectJournal(self.db, trusted_idempotent=trusted(dedupe))
        out = journal.execute(eff.idempotency_key, dedupe.perform, dedupe.reconcile,
                              owner="w2", connector=DEDUPE, now=T + LEASE + 1)
        self.assertEqual((out.status, out.dispatches, out.idempotency_key),
                         ("succeeded", 2, eff.idempotency_key))
        self.assertEqual(dedupe.count(eff.idempotency_key), 1)

    def test_dead_dispatcher_in_flight_window_refuses_reconcile(self):
        self._run_and_kill(_worker_dies_mid_dispatch, True)
        eff = self.journal.unresolved()[0]
        with self.assertRaises(EffectInFlight):     # dispatch timeout (20 s) not yet over
            self.journal.execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                 owner="w2", connector=CONN, now=T + 5)
        self.assertEqual(self.ext.count(), 1)


# -- late dispatcher (stalled past its claim, still alive) ---------------------
class LateDispatcher(_TmpDB):
    """Dolores review of af4960f: A claims the effect, stalls past
    dispatch_timeout_s, then its call lands. B must not double-apply."""

    def _race(self, dedupe):
        task, _ = self.store.submit("tg:msg:late", "reply", now=T)
        key = self.journal.record_intent(task.id, "send", action(task.id), now=T).idempotency_key
        ext = (DedupingExternal if dedupe else External)(os.path.join(self.tmp.name, f"late-{dedupe}.sqlite"))
        started, go, q = SPAWN.Event(), SPAWN.Event(), SPAWN.Queue()
        a = SPAWN.Process(target=_late_dispatcher, args=(self.db, ext.path, dedupe, key, started, go, q))
        a.start()
        self.addCleanup(lambda: (go.set(), a.join(30)))
        self.assertTrue(started.wait(60))
        b = EffectJournal(self.db, dispatch_timeout_s=20,
                          trusted_idempotent=trusted(ext) if dedupe else None)
        self.assertEqual(b.get(key).in_flight_owner, "A")
        return key, ext, b, go, a, q

    def test_default_refuses_redispatch_and_late_a_applies_once(self):
        key, ext, b, go, a, q = self._race(dedupe=False)
        calls = []
        with self.assertRaisesRegex(EffectOutcomeUnknown, "fence"):   # claim expired at T+20
            b.execute(key, lambda e: calls.append(e), ext.reconcile, owner="B", connector=CONN, now=T + 21)
        self.assertEqual(calls, [])
        eff = b.get(key)
        self.assertEqual((eff.status, eff.in_flight_owner), ("unknown", "A"))   # left unresolved
        self.assertIn(key, [e.idempotency_key for e in b.unresolved()])
        go.set()                                   # A's late call lands
        a.join(30)
        self.assertEqual(q.get(timeout=5), "succeeded")
        self.assertEqual(ext.count(key), 1)
        self.assertEqual((b.get(key).status, b.get(key).dispatches), ("succeeded", 1))

    def test_trusted_idempotent_connector_dedupes_late_a(self):
        key, ext, b, go, a, q = self._race(dedupe=True)
        out = b.execute(key, ext.perform, ext.reconcile, owner="B", connector=DEDUPE, now=T + 21)
        self.assertEqual((out.status, out.dispatches), ("succeeded", 2))
        go.set()                                   # A's late duplicate reaches the external system
        a.join(30)
        self.assertEqual(q.get(timeout=5), "succeeded")
        self.assertEqual((ext.count(key), ext.attempts(key)), (1, 2))   # sent twice, applied once
        self.assertEqual(b.get(key).dispatches, 2)


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
class _EffectFixture(_TmpDB):
    def setUp(self):
        super().setUp()
        self.task, _ = self.store.submit("tg:msg:2", "reply", now=T)
        self.eff = self.journal.record_intent(self.task.id, "send", action(self.task.id), now=T)
        self.key = self.eff.idempotency_key
        self.never_calls = []

    def _make_unknown(self, applied):
        def timeout(e):
            if applied:                      # the write landed but the response was lost
                self.ext.apply(e.idempotency_key)
            raise TimeoutError("connector timed out")
        with self.assertRaises(EffectOutcomeUnknown):
            self.journal.execute(self.key, timeout, self.ext.reconcile, owner="w1", connector=CONN, now=T)
        self.assertEqual(self.journal.get(self.key).status, "unknown")

    def _never(self, e):
        # Record rather than raise: an exception inside perform is itself an
        # "unknown outcome" and would mask an illegal dispatch.
        self.never_calls.append(e.idempotency_key)

    def tearDown(self):
        self.assertEqual(self.never_calls, [], "perform was called when it must not be")


class UnknownOutcome(_EffectFixture):

    def test_intent_recorded_before_execution_with_deterministic_key(self):
        self.assertEqual(self.eff.status, "pending")
        self.assertEqual(self.key, idempotency_key(self.task.id, "send", action(self.task.id).hash()))
        seen = []

        def perform(e):
            seen.append(self.journal.get(e.idempotency_key).status)  # what a crash would leave
            return None
        self.journal.execute(self.key, perform, self.ext.reconcile, owner="w1", connector=CONN, now=T)
        self.assertEqual(seen, ["unknown"])

    def test_retry_refused_until_reconciled(self):
        self._make_unknown(applied=False)
        for verdict in ("still_unknown", "maybe", None):
            with self.subTest(verdict=verdict), self.assertRaises(EffectOutcomeUnknown):
                self.journal.execute(self.key, self._never, lambda e, v=verdict: v,
                                     owner="w1", connector=CONN, now=T + 1)

        def broken(e):
            raise ConnectionError("cannot read external state")
        with self.assertRaises(EffectOutcomeUnknown):
            self.journal.execute(self.key, self._never, broken, owner="w1", connector=CONN, now=T + 1)
        self.assertEqual(self.journal.get(self.key).status, "unknown")

    def test_reconcile_succeeded_does_not_reexecute(self):
        self._make_unknown(applied=True)
        out = self.journal.execute(self.key, self._never, self.ext.reconcile, owner="w1", connector=CONN, now=T + 1)
        self.assertEqual((out.status, out.dispatches), ("succeeded", 1))
        self.assertEqual(self.ext.count(self.key), 1)

    def test_reconcile_failed_without_guarantee_stays_unresolved(self):
        self._make_unknown(applied=False)    # a timed-out request may still land later
        with self.assertRaisesRegex(EffectOutcomeUnknown, "fence"):
            self.journal.execute(self.key, self._never, self.ext.reconcile, owner="w1", connector=CONN, now=T + 1)
        self.assertEqual(self.journal.reconcile(self.key, self.ext.reconcile, now=T + 1).status, "unknown")
        self.ext.apply(self.key)             # ...and it does: authoritative reconcile resolves it
        self.assertEqual(self.journal.reconcile(self.key, self.ext.reconcile, now=T + 2).status,
                         "succeeded")
        self.assertEqual(self.ext.count(), 1)

    def test_fence_seam_errors_and_non_true_refuse(self):
        self._make_unknown(applied=False)
        calls = []   # not self._never: an exception inside perform would mask a dispatch
        for fence in (lambda e: 1, lambda e: "yes", lambda e: None, lambda e: 1 / 0):
            with self.subTest(fence=fence), self.assertRaisesRegex(EffectOutcomeUnknown, "fence"):
                EffectJournal(self.db, fence=fence).execute(
                    self.key, calls.append, self.ext.reconcile, owner="w1", connector=CONN, now=T + 1)
        self.assertEqual(calls, [])
        self.assertEqual((self.journal.get(self.key).status, self.journal.get(self.key).dispatches),
                         ("unknown", 1))

    def test_reconcile_failed_allows_exactly_one_retry_with_same_key(self):
        keys, flaky = [], [True]

        def adapter(e):                      # the reviewed adapter for DEDUPE
            keys.append(e.idempotency_key)
            if flaky.pop() if flaky else False:
                raise TimeoutError("connector timed out")   # first call: ambiguous, not applied
            return self.ext.perform(e)
        journal = EffectJournal(self.db, trusted_idempotent=TrustedIdempotentRegistry(
            [ConnectorContract(DEDUPE, adapter, "test: provider dedupes on key")]))
        with self.assertRaises(EffectOutcomeUnknown):
            journal.execute(self.key, adapter, self.ext.reconcile, owner="w1", connector=DEDUPE, now=T)
        out = journal.execute(self.key, adapter, self.ext.reconcile, owner="w1", connector=DEDUPE, now=T + 1)
        self.assertEqual(keys, [self.key, self.key])
        self.assertEqual((out.status, out.dispatches), ("succeeded", 2))
        keys.clear()
        again = journal.execute(self.key, adapter, self.ext.reconcile, owner="w1", connector=DEDUPE, now=T + 2)
        self.assertEqual((again.status, keys), ("succeeded", []))
        self.assertEqual(self.ext.count(), 1)

    def test_reconcile_without_dispatch(self):
        self._make_unknown(applied=True)
        self.assertEqual(self.journal.reconcile(self.key, self.ext.reconcile, now=T + 1).status,
                         "succeeded")
        self.assertEqual(self.journal.unresolved(), [])

    def test_definite_failure_retries_with_same_key(self):
        def reject(e):
            raise EffectFailed("422 invalid recipient")
        out = self.journal.execute(self.key, reject, self.ext.reconcile, owner="w1", connector=CONN, now=T)
        self.assertEqual(out.status, "failed")
        out = self.journal.execute(self.key, self.ext.perform, self.ext.reconcile, owner="w1", connector=CONN, now=T + 1)
        self.assertEqual((out.status, out.dispatches, out.idempotency_key), ("succeeded", 2, self.key))

    def test_concurrent_dispatch_refused(self):
        inner = []

        def perform(e):
            with self.assertRaises(EffectInFlight):
                self.journal.execute(self.key, self.ext.perform, self.ext.reconcile,
                                     owner="w2", connector=CONN, now=T)
            inner.append(1)
            return self.ext.perform(e)
        self.journal.execute(self.key, perform, self.ext.reconcile, owner="w1", connector=CONN, now=T)
        self.assertEqual((inner, self.ext.count()), ([1], 1))

    def test_tampered_journal_action_never_dispatched(self):
        with _sql(self.db) as c:
            c.execute("UPDATE effects SET action_canonical=replace(action_canonical, '1', '9')")
        with self.assertRaisesRegex(ApprovalRefused, "integrity"):
            self.journal.execute(self.key, self._never, self.ext.reconcile, owner="w1", connector=CONN, now=T)
        self.assertEqual(self.journal.get(self.key).status, "pending")

    def test_step_cannot_be_rebound_to_another_action(self):
        self.assertEqual(self.journal.record_intent(self.task.id, "send", action(self.task.id)),
                         self.eff)
        with self.assertRaises(EffectConflict):
            self.journal.record_intent(self.task.id, "send", action(self.task.id, n=2))


# -- trusted idempotent registry (Dolores review of 5c9e125) ------------------
class TrustedIdempotentSeam(_EffectFixture):
    """Re-dispatch after an ambiguous outcome is enabled only by configuration
    fixed at construction, never by a caller's flag or a callable's attribute."""

    def test_declared_attribute_cannot_enable_redispatch(self):
        self._make_unknown(applied=False)

        def fn(e):
            self.never_calls.append(e.idempotency_key)
        fn.enforces_idempotency_key = True

        class Claims:
            enforces_idempotency_key = True

            def __call__(s, e):
                self.never_calls.append(e.idempotency_key)

            def perform(s, e):
                self.never_calls.append(e.idempotency_key)

        other = DedupingExternal(os.path.join(self.tmp.name, "o.sqlite"))
        journals = (self.journal, EffectJournal(self.db, trusted_idempotent=trusted(other)))
        for journal in journals:
            for perform in (fn, Claims(), Claims().perform):
                with self.subTest(perform=perform), self.assertRaises(EffectOutcomeUnknown):
                    journal.execute(self.key, perform, self.ext.reconcile, owner="w1",
                                    connector=CONN, now=T + 1)
        self.assertEqual(self.journal.get(self.key).status, "unknown")

    def test_no_caller_flag_is_accepted(self):
        self._make_unknown(applied=False)
        for flag in ("enforces_idempotency_key", "allow_failed", "trusted", "idempotent"):
            with self.subTest(flag=flag):
                with self.assertRaises(TypeError):
                    self.journal.reconcile(self.key, self.ext.reconcile, now=T + 1, **{flag: True})
                with self.assertRaises(TypeError):
                    self.journal.execute(self.key, self._never, self.ext.reconcile, owner="w1",
                                         connector=CONN, now=T + 1, **{flag: True})
        self.assertEqual(self.journal.get(self.key).status, "unknown")

    def test_unregistered_identity_refused(self):
        self._make_unknown(applied=False)    # recorded connector: CONN, not in the registry
        dedupe = DedupingExternal(os.path.join(self.tmp.name, "d.sqlite"))
        journal = EffectJournal(self.db, trusted_idempotent=trusted(dedupe))
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            journal.execute(self.key, self._never, self.ext.reconcile, owner="w1",
                            connector=CONN, now=T + 1)
        self.assertEqual(journal.reconcile(self.key, self.ext.reconcile, now=T + 1).status, "unknown")
        # Naming a trusted identity now cannot re-label the effect's earlier dispatch.
        with self.assertRaises(EffectConflict):
            journal.execute(self.key, dedupe.perform, dedupe.reconcile, owner="w1",
                            connector=DEDUPE, now=T + 1)
        self.assertEqual(self.journal.get(self.key).connector, CONN)

    def test_trusted_identity_requires_the_registered_adapter(self):
        dedupe = DedupingExternal(os.path.join(self.tmp.name, "d.sqlite"))
        journal = EffectJournal(self.db, trusted_idempotent=trusted(dedupe))
        with self.assertRaisesRegex(EffectConflict, "registered adapter"):
            journal.execute(self.key, self._never, dedupe.reconcile, owner="w1",
                            connector=DEDUPE, now=T)
        self.assertEqual(journal.get(self.key).status, "pending")   # nothing claimed

    def test_registry_is_frozen_and_typed(self):
        dedupe = DedupingExternal(os.path.join(self.tmp.name, "d.sqlite"))
        reg = trusted(dedupe)
        with self.assertRaises(TypeError):
            reg["x"] = reg[DEDUPE]
        with self.assertRaises(AttributeError):
            reg._entries = {}
        with self.assertRaises(TypeError):
            reg._entries["x"] = reg[DEDUPE]
        with self.assertRaises(TypeError):
            EffectJournal(self.db, trusted_idempotent={DEDUPE: reg[DEDUPE]})
        with self.assertRaises(ValueError):
            TrustedIdempotentRegistry([reg[DEDUPE], reg[DEDUPE]])
        with self.assertRaises(TypeError):
            TrustedIdempotentRegistry([(DEDUPE, dedupe.perform)])
        with self.assertRaises(ValueError):
            ConnectorContract(DEDUPE, dedupe.perform, "")

    def test_connector_identity_required_and_recorded(self):
        with self.assertRaises(ValueError):
            self.journal.execute(self.key, self._never, self.ext.reconcile, owner="w1",
                                 connector="", now=T)
        out = self.journal.execute(self.key, self.ext.perform, self.ext.reconcile, owner="w1",
                                   connector=CONN, now=T)
        self.assertEqual((out.status, out.connector), ("succeeded", CONN))


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

    def test_job_escalates_after_three_denied_approvals(self):
        """Codex P2 on af4960f: approval-ended runs count toward job escalation."""
        self.store.register_job("digest", 3600, now=T)
        for run in range(3):
            task, _ = self.store.submit(f"job:digest:appr:{run}", "digest", job="digest", now=T + run)
            self.store.claim("w", LEASE, now=T + run)
            self.store.wait_for_approval(task.id, "w", now=T + run)
            out = self.store.approval_resolved(task.id, False, error="approval expired", now=T + run)
            self.assertEqual(out.state, FAILED)  # a human decision fails the task; it isn't escalated
            self.assertEqual(len(self.escalations), 0 if run < 2 else 1)
        e = self.escalations[0]
        self.assertEqual((e.subject, e.id, e.failures, e.last_error), ("job", "digest", 3, "approval expired"))
        self.assertEqual(self.store.job("digest").consecutive_failures, 3)

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
            journal.execute(eff.idempotency_key, ext.perform, ext.reconcile, owner="w", connector=CONN, now=T)
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
