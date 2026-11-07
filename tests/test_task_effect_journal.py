"""C2 merge evidence (decision T10): kill mid-task resumes; duplicate trigger
deduped; unknown-outcome effect reconciles before retry; 3 failures escalate;
last-success watchdog flags stale job; C0 approval conformance suite passes
against the production JournalApprovalLedger.

Offline only: the "external system" is a local SQLite file standing in for a
connector that deduplicates on the idempotency key and counts applications.
"""
import contextlib
import hashlib
import importlib.util
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
                     ConnectorContract, Database, EffectConflict, JournalUnavailable, TrustedIdempotentRegistry, EffectFailed, EffectInFlight, EffectJournal,
                     EffectOutcomeUnknown, InvalidTransition, JournalApprovalLedger, LeaseLost,
                     TaskStore, idempotency_key, task_id_for)
from runtime import (AdapterVerificationError, IdempotencyScope, adapter_artifact_digest,  # noqa: E402
                     current_idempotency_scope)
from runtime._contract import ApprovalRefused, BoundAction  # noqa: E402

T = 1_800_000_000.0
CONN = "fake.external"      # plain connector: no idempotency guarantee
DEDUPE = "fake.dedupe"      # connector whose stub provider dedupes on the key
LEASE = 30.0
SPAWN = multiprocessing.get_context("spawn")


def action(task_id, n=1):
    return BoundAction(task_id, "test.fake", "write", "fake://external", {"n": n})


# -- fake external system ---------------------------------------------------
def scope_for(account, endpoint):
    cfg = hashlib.sha256(f"{account}|{endpoint}".encode()).hexdigest()
    return IdempotencyScope("fake-provider", account, endpoint, "per-account-key", "1", 86_400, cfg,
                            enforced_request_lifetime_s=300)


SCOPE = scope_for("acct-1", "sqlite://fixed")


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

    def __init__(self, path, stall=None, stall_after_apply=False, account="acct-1"):
        self.path = str(path)
        self.account = account
        self.stall, self.stall_after_apply = stall, stall_after_apply   # (started, go) events
        with _sql(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS applied (key TEXT PRIMARY KEY, n INTEGER NOT NULL)")

    def idempotency_scope(self):
        """Read-only report of the namespace this adapter dedupes in now."""
        return scope_for(self.account, "sqlite://" + os.path.basename(self.path))

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


class FlakyDedupe(DedupingExternal):
    """Deduping stub whose first call times out before anything is applied."""

    def __init__(self, path, **kw):
        super().__init__(path, **kw)
        self.calls = 0

    def perform(self, effect):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("connector timed out")   # ambiguous; nothing applied
        return super().perform(effect)


def contract(identity, adapter, ref, *args, **kw):
    """A contract whose artifact digest is declared from the adapter's current
    source, as trusted configuration would after review."""
    return ConnectorContract(identity, adapter, ref, adapter_artifact_digest(adapter),
                             current_idempotency_scope(adapter), *args, **kw)


def trusted(ext, identity=DEDUPE):
    """Configuration-time trust: the reviewed adapter is this stub's perform."""
    return TrustedIdempotentRegistry([contract(identity, ext.perform,
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
        keys, flaky, ext = [], [True], self.ext

        class Adapter:                       # the reviewed adapter for DEDUPE
            def idempotency_scope(self):
                return ext.idempotency_scope()

            def perform(self, e):
                keys.append(e.idempotency_key)
                if flaky.pop() if flaky else False:
                    raise TimeoutError("connector timed out")   # first call: ambiguous, not applied
                return ext.perform(e)
        adapter = Adapter().perform
        journal = EffectJournal(self.db, trusted_idempotent=TrustedIdempotentRegistry(
            [contract(DEDUPE, adapter, "test: provider dedupes on key")]))
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
            contract(DEDUPE, dedupe.perform, "")

    def test_connector_identity_required_and_recorded(self):
        with self.assertRaises(ValueError):
            self.journal.execute(self.key, self._never, self.ext.reconcile, owner="w1",
                                 connector="", now=T)
        out = self.journal.execute(self.key, self.ext.perform, self.ext.reconcile, owner="w1",
                                   connector=CONN, now=T)
        self.assertEqual((out.status, out.connector), ("succeeded", CONN))


# -- contract rotation across restarts (Dolores review of 2461704) ------------
class ContractRotation(_EffectFixture):
    """Trust is bound to the exact contract recorded at first dispatch, so a
    registry rebuilt after a restart cannot retry under changed assumptions."""

    def setUp(self):
        super().setUp()
        self.ext_path = os.path.join(self.tmp.name, "provider.sqlite")

    def _journal(self, adapter, ref="contract:v1", **kw):
        return EffectJournal(self.db, trusted_idempotent=TrustedIdempotentRegistry(
            [contract(DEDUPE, adapter, ref, **kw)]))

    def _ambiguous_under_v1(self):
        a = FlakyDedupe(self.ext_path)
        j1 = self._journal(a.perform)
        with self.assertRaises(EffectOutcomeUnknown):
            j1.execute(self.key, a.perform, a.reconcile, owner="A", connector=DEDUPE, now=T)
        eff = j1.get(self.key)
        self.assertEqual((eff.status, eff.connector, eff.connector_digest),
                         ("unknown", DEDUPE, j1._trusted[DEDUPE].contract_digest))
        return eff

    def _restart_refused(self, journal, adapter):
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            journal.execute(self.key, adapter.perform, adapter.reconcile, owner="B",
                            connector=DEDUPE, now=T + 1)
        self.assertEqual(journal.reconcile(self.key, adapter.reconcile, now=T + 1).status, "unknown")
        self.assertIn(self.key, [e.idempotency_key for e in journal.unresolved()])
        self.assertEqual((adapter.attempts(self.key), journal.get(self.key).dispatches), (0, 1))

    def test_digest_is_canonical_and_covers_every_field(self):
        a = FlakyDedupe(self.ext_path)
        base = contract(DEDUPE, a.perform, "contract:v1")
        self.assertEqual(base.contract_digest,
                         contract(DEDUPE, FlakyDedupe(self.ext_path).perform,
                                           "contract:v1").contract_digest)   # same contract, new process
        variants = [contract("fake.other", a.perform, "contract:v1"),
                    contract(DEDUPE, a.perform, "contract:v2"),
                    contract(DEDUPE, DedupingExternal(self.ext_path).perform, "contract:v1"),
                    ConnectorContract(DEDUPE, a.perform, "contract:v1", "1" * 64, a.idempotency_scope()),
                    contract(DEDUPE, FlakyDedupe(self.ext_path, account="acct-2").perform, "contract:v1"),
                    contract(DEDUPE, a.perform, "contract:v1",
                                      idempotency_semantics="dedupes within 24 h")]
        self.assertEqual(len({v.contract_digest for v in variants} | {base.contract_digest}), 7)
        # Same inherited function, different adapter class: different contract.
        self.assertNotEqual(contract(DEDUPE, External(self.ext_path).perform, "c").contract_digest,
                            contract(DEDUPE, DedupingExternal(self.ext_path).perform, "c").contract_digest)
        with self.assertRaises(Exception):
            base.contract_digest = "0" * 64                       # immutable
        with self.assertRaises(TypeError):
            ConnectorContract(DEDUPE, a.perform, "contract:v1", "0" * 64, SCOPE, contract_digest="0" * 64)

    def test_restart_with_rotated_contract_ref_refused(self):
        self._ambiguous_under_v1()
        b = FlakyDedupe(self.ext_path)
        b.calls = 1                                   # would succeed if it were called
        self._restart_refused(self._journal(b.perform, ref="contract:v2"), b)

    def test_restart_with_different_adapter_refused(self):
        self._ambiguous_under_v1()
        b = DedupingExternal(self.ext_path)           # same identity, different adapter
        self._restart_refused(self._journal(b.perform), b)

    def test_restart_with_changed_semantics_refused(self):
        self._ambiguous_under_v1()
        b = FlakyDedupe(self.ext_path)
        b.calls = 1
        self._restart_refused(self._journal(b.perform, idempotency_semantics="dedupes within 24 h"), b)

    def test_restart_with_identical_contract_allowed(self):
        self._ambiguous_under_v1()
        b = FlakyDedupe(self.ext_path)                # fresh process, same reviewed contract
        b.calls = 1
        out = self._journal(b.perform).execute(self.key, b.perform, b.reconcile, owner="B",
                                               connector=DEDUPE, now=T + 1)
        self.assertEqual((out.status, out.dispatches), ("succeeded", 2))
        self.assertEqual((b.count(self.key), b.attempts(self.key)), (1, 1))

    def test_pre_migration_row_without_digest_refused(self):
        self._ambiguous_under_v1()
        with _sql(self.db) as c:                      # turn the file back into a c2.v1 journal
            c.execute("ALTER TABLE effects DROP COLUMN connector_digest")
            c.execute("UPDATE meta SET value='c2.v1' WHERE key='schema'")
        b = FlakyDedupe(self.ext_path)
        b.calls = 1
        journal = self._journal(b.perform)            # opening it migrates to c2.v2
        with _sql(self.db) as c:
            self.assertEqual(c.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0], "c2.v3")
        eff = journal.get(self.key)
        self.assertEqual((eff.status, eff.connector, eff.connector_digest), ("unknown", DEDUPE, None))
        self._restart_refused(journal, b)

    def test_untrusted_first_dispatch_never_gains_trust_later(self):
        a = FlakyDedupe(self.ext_path)
        with self.assertRaises(EffectOutcomeUnknown):   # dispatched while DEDUPE was not trusted
            self.journal.execute(self.key, a.perform, a.reconcile, owner="A", connector=DEDUPE, now=T)
        self.assertIsNone(self.journal.get(self.key).connector_digest)
        b = FlakyDedupe(self.ext_path)
        b.calls = 1
        self._restart_refused(self._journal(b.perform), b)

    def test_unknown_schema_version_refused(self):
        with _sql(self.db) as c:
            c.execute("UPDATE meta SET value='c2.v9' WHERE key='schema'")
        with self.assertRaises(JournalUnavailable):
            Database(self.db)


# -- same-name adapter replacement (Dolores review of e4ca880) ---------------
ADAPTER_SRC = """\
import os
import sqlite3

from runtime import IdempotencyScope

PATH = {path!r}


def idempotency_scope():
    return IdempotencyScope("fake-mail", "acct-1", "sqlite://provider", "per-account-key", "1",
                            86400, "{cfg}", enforced_request_lifetime_s=300)


def perform(effect):
    effect.action()
    if os.path.exists(PATH + ".timeout"):
        raise TimeoutError("connector timed out")   # ambiguous; nothing applied
    c = sqlite3.connect(PATH, timeout=30)
    try:
        with c:
            c.execute({sql!r}, (effect.idempotency_key,))
    finally:
        c.close()
    return {{"applied": effect.idempotency_key}}
"""
DEDUPING_SQL = "INSERT OR IGNORE INTO applied VALUES (?, 1)"
NON_DEDUPING_SQL = "INSERT INTO applied VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET n = n + 1"


class SameNameAdapterSwap(_EffectFixture):
    """The adapter body is replaced under the same module, function name and
    contract_ref; trust recorded before a restart must not carry over."""

    def setUp(self):
        super().setUp()
        self.provider = DedupingExternal(os.path.join(self.tmp.name, "provider.sqlite"))
        self.mod_dir = os.path.join(self.tmp.name, "adapters")
        os.makedirs(self.mod_dir)
        self.mod_path = os.path.join(self.mod_dir, "c2_mail_adapter.py")

    def _write(self, sql):
        with open(self.mod_path, "w") as f:
            f.write(ADAPTER_SRC.format(path=self.provider.path, sql=sql, cfg="c" * 64))

    def _load(self):
        """A fresh import of the module file, as after a process restart."""
        spec = importlib.util.spec_from_file_location("c2_mail_adapter", self.mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.perform

    def _journal(self, adapter, declared=None):
        declared = declared or adapter_artifact_digest(adapter)
        return EffectJournal(self.db, trusted_idempotent=TrustedIdempotentRegistry(
            [ConnectorContract(DEDUPE, adapter, "contract:mail-v1", declared, current_idempotency_scope(adapter))]))

    def _ambiguous_v1(self):
        self._write(DEDUPING_SQL)
        v1 = self._load()
        self.v1_artifact = adapter_artifact_digest(v1)
        open(self.provider.path + ".timeout", "w").close()
        j = self._journal(v1)
        with self.assertRaises(EffectOutcomeUnknown):
            j.execute(self.key, v1, self.provider.reconcile, owner="A", connector=DEDUPE, now=T)
        os.remove(self.provider.path + ".timeout")
        self.assertEqual(j.get(self.key).status, "unknown")
        return j.get(self.key)

    def test_adapter_name_unchanged_but_artifact_differs(self):
        self._write(DEDUPING_SQL)
        a = self._load()
        digest_a = adapter_artifact_digest(a)
        self._write(NON_DEDUPING_SQL)
        b = self._load()
        self.assertEqual(f"{a.__module__}.{a.__qualname__}", f"{b.__module__}.{b.__qualname__}")
        self.assertNotEqual(digest_a, adapter_artifact_digest(b))
        # The still-loaded old code no longer matches the rewritten file: refused,
        # rather than borrowing the new file's digest.
        with self.assertRaisesRegex(AdapterVerificationError, "differs from its source"):
            adapter_artifact_digest(a)

    def test_changed_body_with_stale_declared_digest_fails_registry(self):
        self._ambiguous_v1()
        self._write(NON_DEDUPING_SQL)
        v2 = self._load()
        with self.assertRaises(AdapterVerificationError):
            self._journal(v2, declared=self.v1_artifact)   # config still names the reviewed v1

    def test_changed_body_redeclared_refuses_historical_retry(self):
        self._ambiguous_v1()
        self._write(NON_DEDUPING_SQL)
        v2 = self._load()
        j = self._journal(v2)                              # registry builds, but it is a new contract
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            j.execute(self.key, v2, self.provider.reconcile, owner="B", connector=DEDUPE, now=T + 1)
        self.assertEqual(j.reconcile(self.key, self.provider.reconcile, now=T + 1).status, "unknown")
        self.assertEqual((self.provider.count(self.key), j.get(self.key).dispatches), (0, 1))

    def test_identical_bytes_after_restart_allowed(self):
        self._ambiguous_v1()
        self._write(DEDUPING_SQL)                          # same bytes rewritten
        v1_again = self._load()
        self.assertEqual(adapter_artifact_digest(v1_again), self.v1_artifact)
        out = self._journal(v1_again).execute(self.key, v1_again, self.provider.reconcile,
                                              owner="B", connector=DEDUPE, now=T + 1)
        self.assertEqual((out.status, out.dispatches), ("succeeded", 2))
        self.assertEqual(self.provider.count(self.key), 1)

    def test_unverifiable_adapters_rejected(self):
        exec_ns = {}
        exec(compile("def perform(effect):\n    return None\n", "<repl>", "exec"), exec_ns)
        for adapter in (len, exec_ns["perform"], sqlite3.connect):
            with self.subTest(adapter=adapter):
                with self.assertRaises(AdapterVerificationError):
                    adapter_artifact_digest(adapter)
                with self.assertRaises(AdapterVerificationError):
                    TrustedIdempotentRegistry([ConnectorContract(DEDUPE, adapter, "c", "0" * 64, SCOPE)])
        with self.assertRaises(ValueError):
            ConnectorContract(DEDUPE, self.provider.perform, "c", "not-a-digest", SCOPE)


# -- provider idempotency namespace (Dolores review of d8ea587) ---------------
class NamespacedProvider:
    """A provider that dedupes per (account, key): the same key sent to two
    accounts is applied twice."""

    def __init__(self, path):
        self.path = str(path)
        with _sql(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS applied (account TEXT, key TEXT,"
                      " PRIMARY KEY (account, key))")

    def apply(self, account, key):
        with _sql(self.path) as c:
            c.execute("INSERT OR IGNORE INTO applied VALUES (?, ?)", (account, key))

    def count(self, key):
        with _sql(self.path) as c:
            return c.execute("SELECT COUNT(*) FROM applied WHERE key=?", (key,)).fetchone()[0]

    def reconcile_for(self, account):
        def reconcile(effect):
            with _sql(self.path) as c:
                hit = c.execute("SELECT 1 FROM applied WHERE account=? AND key=?",
                                (account, effect.idempotency_key)).fetchone()
            return "succeeded" if hit else "failed"
        return reconcile


class NamespacedAdapter:
    def __init__(self, provider, account, endpoint="https://api.example.invalid/v1"):
        self.provider, self.account, self.endpoint = provider, account, endpoint
        self.timeout_next = False
        self.broken_report = False

    def idempotency_scope(self):
        if self.broken_report:
            raise RuntimeError("config unavailable")
        return scope_for(self.account, self.endpoint)

    def perform(self, effect):
        effect.action()
        if self.timeout_next:
            self.timeout_next = False
            raise TimeoutError("connector timed out")   # ambiguous; nothing applied yet
        self.provider.apply(self.account, effect.idempotency_key)
        return {"account": self.account}


class IdempotencyScopeBinding(_EffectFixture):
    """The trusted contract binds the provider namespace; a retry may not land
    in a different one."""

    def setUp(self):
        super().setUp()
        self.provider = NamespacedProvider(os.path.join(self.tmp.name, "ns-provider.sqlite"))

    def _journal(self, adapter, scope=None):
        return EffectJournal(self.db, trusted_idempotent=TrustedIdempotentRegistry([
            ConnectorContract(DEDUPE, adapter.perform, "contract:ns-v1",
                              adapter_artifact_digest(adapter.perform),
                              scope or adapter.idempotency_scope())]))

    def _ambiguous_in(self, account):
        a = NamespacedAdapter(self.provider, account)
        a.timeout_next = True
        j = self._journal(a)
        with self.assertRaises(EffectOutcomeUnknown):
            j.execute(self.key, a.perform, self.provider.reconcile_for(account), owner="A",
                      connector=DEDUPE, now=T)
        return a, j

    def test_retry_into_new_namespace_refused_while_old_call_lands(self):
        self._ambiguous_in("acct-1")
        b = NamespacedAdapter(self.provider, "acct-2")      # restarted, pointed at another tenant
        j2 = self._journal(b)                              # registry rebuilt with the new scope
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            j2.execute(self.key, b.perform, self.provider.reconcile_for("acct-2"), owner="B",
                       connector=DEDUPE, now=T + 1)
        self.provider.apply("acct-1", self.key)            # A's original call lands, late
        self.assertEqual(self.provider.count(self.key), 1)  # one application, not one per namespace
        # A registry still declaring the old scope cannot even be built for B.
        with self.assertRaisesRegex(AdapterVerificationError, "idempotency scope"):
            self._journal(b, scope=scope_for("acct-1", b.endpoint))

    def test_config_mutated_while_running_refused(self):
        a = NamespacedAdapter(self.provider, "acct-1")
        j = self._journal(a)
        a.account = "acct-2"                               # config changed after registration
        with self.assertRaisesRegex(EffectConflict, "idempotency scope"):
            j.execute(self.key, a.perform, self.provider.reconcile_for("acct-2"), owner="A",
                      connector=DEDUPE, now=T)
        self.assertEqual((j.get(self.key).status, self.provider.count(self.key)), ("pending", 0))

    def test_config_mutated_after_ambiguous_dispatch_refused(self):
        a, j = self._ambiguous_in("acct-1")
        a.account = "acct-2"
        with self.assertRaisesRegex(EffectConflict, "idempotency scope"):
            j.execute(self.key, a.perform, self.provider.reconcile_for("acct-2"), owner="A",
                      connector=DEDUPE, now=T + 1)
        # Even a "not applied" read cannot clear it while the scope differs.
        self.assertEqual(j.reconcile(self.key, self.provider.reconcile_for("acct-1"), now=T + 1).status,
                         "unknown")
        self.assertEqual((j.get(self.key).dispatches, self.provider.count(self.key)), (1, 0))

    def test_adapter_that_cannot_report_scope_refused(self):
        a = NamespacedAdapter(self.provider, "acct-1")
        a.broken_report = True
        with self.assertRaisesRegex(AdapterVerificationError, "idempotency scope"):
            self._journal(a, scope=scope_for("acct-1", a.endpoint))
        a.broken_report = False
        a2, j = self._ambiguous_in("acct-1")
        a2.broken_report = True                            # starts failing after registration
        with self.assertRaisesRegex(EffectConflict, "idempotency scope"):
            j.execute(self.key, a2.perform, self.provider.reconcile_for("acct-1"), owner="A",
                      connector=DEDUPE, now=T + 1)

        class NoReport:
            def perform(self, e):
                return None
        with self.assertRaisesRegex(AdapterVerificationError, "cannot report"):
            current_idempotency_scope(NoReport().perform)

    def test_same_scope_after_restart_allowed(self):
        self._ambiguous_in("acct-1")
        b = NamespacedAdapter(self.provider, "acct-1")      # restarted, same namespace
        out = self._journal(b).execute(self.key, b.perform, self.provider.reconcile_for("acct-1"),
                                       owner="B", connector=DEDUPE, now=T + 1)
        self.assertEqual((out.status, out.dispatches), ("succeeded", 2))
        self.provider.apply("acct-1", self.key)            # late duplicate of A: deduped
        self.assertEqual(self.provider.count(self.key), 1)

    def test_scope_validation(self):
        for bad in ({"retention_window_s": 0}, {"retention_window_s": True},
                    {"config_hash": "x"}, {"account": ""}):
            fields = dict(provider="p", account="a", endpoint="e", dedupe_scope="s",
                          dedupe_version="1", retention_window_s=60, config_hash="0" * 64)
            fields.update(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                IdempotencyScope(**fields)
        with self.assertRaises(TypeError):
            ConnectorContract(DEDUPE, NamespacedAdapter(self.provider, "a").perform, "c",
                              "0" * 64, {"account": "a"})


# -- finite provider retention (Dolores review of fdd3137) --------------------
class WindowedProvider:
    """A synthetic provider with a finite dedupe memory and (optionally) an
    enforced request lifetime: it rejects requests older than ``lifetime``
    and remembers an applied key for ``retention`` seconds."""

    def __init__(self, path, retention, lifetime=None):
        self.path, self.retention, self.lifetime = str(path), retention, lifetime
        with _sql(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS applied (key TEXT PRIMARY KEY, at REAL, n INTEGER)")
            c.execute("CREATE TABLE IF NOT EXISTS log (key TEXT, at REAL, outcome TEXT)")

    def deliver(self, key, sent_at, at):
        """A request sent at ``sent_at`` reaches the provider at ``at``."""
        with _sql(self.path) as c:
            if self.lifetime is not None and at - sent_at > self.lifetime:
                outcome = "rejected-expired"
            else:
                row = c.execute("SELECT at, n FROM applied WHERE key=?", (key,)).fetchone()
                if row and at - row[0] <= self.retention:
                    outcome = "deduped"
                else:
                    c.execute("INSERT INTO applied VALUES (?, ?, 1) ON CONFLICT(key) DO UPDATE"
                              " SET at=excluded.at, n=n+1", (key, at))
                    outcome = "applied"
            c.execute("INSERT INTO log VALUES (?, ?, ?)", (key, at, outcome))
        return outcome

    def applications(self, key):
        with _sql(self.path) as c:
            return [r[0] for r in c.execute(
                "SELECT at FROM log WHERE key=? AND outcome='applied' ORDER BY at", (key,))]

    def reconcile(self, effect):
        return "succeeded" if self.applications(effect.idempotency_key) else "failed"


class WindowedAdapter:
    """Sends at the simulated time in ``clock[0]``. With ``stall`` set, the
    request leaves but the call times out (an ambiguous outcome); the test
    then decides when, or whether, that original request lands."""

    def __init__(self, provider, clock, scope):
        self.provider, self.clock, self.scope = provider, clock, scope
        self.stall = False

    def idempotency_scope(self):
        return self.scope

    def perform(self, effect):
        effect.action()
        if self.stall:
            self.stall = False
            raise TimeoutError("connector timed out")
        return {"outcome": self.provider.deliver(effect.idempotency_key, self.clock[0], self.clock[0])}


class RetentionWindow(_EffectFixture):
    """A finite dedupe window only excludes a duplicate if every attempt is
    provably inside it: provider-enforced request lifetime L and retention
    R >= L + (retry - first_dispatch) + margin, checked at retry time."""

    def _run(self, retention, lifetime, retry_at, margin=0.0, first_at=1000.0):
        provider = WindowedProvider(os.path.join(self.tmp.name, f"w-{retry_at}.sqlite"),
                                    retention, lifetime)
        scope = IdempotencyScope("fake-windowed", "acct-1", "https://api.example.invalid", "per-key",
                                 "1", retention, "d" * 64, enforced_request_lifetime_s=lifetime)
        clock = [first_at]

        def journal():
            a = WindowedAdapter(provider, clock, scope)
            return a, EffectJournal(self.db, retention_margin_s=margin, trusted_idempotent=TrustedIdempotentRegistry(
                [ConnectorContract(DEDUPE, a.perform, "contract:windowed-v1",
                                   adapter_artifact_digest(a.perform), scope)]))
        a, ja = journal()
        a.stall = True                                     # A's request leaves at first_at, then stalls
        with self.assertRaises(EffectOutcomeUnknown):
            ja.execute(self.key, a.perform, provider.reconcile, owner="A", connector=DEDUPE, now=first_at)
        self.assertEqual(ja.get(self.key).first_dispatched_at, first_at)
        clock[0] = retry_at
        b, jb = journal()                                  # restarted dispatcher B
        try:
            out = jb.execute(self.key, b.perform, provider.reconcile, owner="B", connector=DEDUPE,
                             now=retry_at)
        except EffectOutcomeUnknown:
            out = None
        return provider, jb, out

    def test_dolores_scenario_no_enforced_lifetime_refused(self):
        # retention 1 s, no enforced lifetime: B at t1002 must not retry.
        provider, jb, out = self._run(retention=1, lifetime=None, retry_at=1002.0)
        self.assertIsNone(out)
        self.assertEqual(jb.get(self.key).status, "unknown")
        self.assertEqual(provider.deliver(self.key, 1000.0, 1004.0), "applied")   # A lands at t1004
        self.assertEqual(provider.applications(self.key), [1004.0])               # exactly once
        # Sanity: had B applied at t1002, A at t1004 would have been a second application.
        self.assertEqual(provider.deliver("probe", 1002.0, 1002.0), "applied")
        self.assertEqual(provider.deliver("probe", 1000.0, 1004.0), "applied")
        self.assertEqual(len(provider.applications("probe")), 2)

    def test_enforced_lifetime_but_retention_too_short_refused(self):
        provider, jb, out = self._run(retention=1, lifetime=5, retry_at=1002.0)
        self.assertIsNone(out)
        self.assertEqual(jb.reconcile(self.key, provider.reconcile, now=1002.0).status, "unknown")
        self.assertEqual(provider.applications(self.key), [])

    def test_window_boundary_inclusive(self):
        # R=100, L=5, margin=10, first=1000: allowed iff retry <= 1085.
        provider, jb, out = self._run(retention=100, lifetime=5, retry_at=1085.0, margin=10.0)
        self.assertEqual((out.status, out.dispatches), ("succeeded", 2))
        self.assertEqual(provider.deliver(self.key, 1000.0, 1090.0), "rejected-expired")  # late A
        self.assertEqual(provider.applications(self.key), [1085.0])

    def test_window_just_past_boundary_refused(self):
        provider, jb, out = self._run(retention=100, lifetime=5, retry_at=1085.5, margin=10.0)
        self.assertIsNone(out)
        self.assertEqual(provider.applications(self.key), [])

    def test_margin_is_applied(self):
        provider, jb, out = self._run(retention=100, lifetime=5, retry_at=1085.0, margin=10.5)
        self.assertIsNone(out)

    def test_valid_window_positive_control_late_original_deduped(self):
        # Long retention: B retries at t1002; A's original lands at t1004 (within L) and is deduped.
        provider, jb, out = self._run(retention=3600, lifetime=60, retry_at=1002.0, margin=1.0)
        self.assertEqual(out.status, "succeeded")
        self.assertEqual(provider.deliver(self.key, 1000.0, 1004.0), "deduped")
        self.assertEqual(provider.applications(self.key), [1002.0])

    def test_window_anchored_to_first_dispatch_not_latest(self):
        # R=100, L=5: A at t1000 and an allowed retry B at t1050 both time out.
        # C at t1096 must be measured from t1000 (5+96 > 100), not from t1050.
        provider = WindowedProvider(os.path.join(self.tmp.name, "anchor.sqlite"), 100, 5)
        scope = IdempotencyScope("fake-windowed", "acct-1", "e", "per-key", "1", 100, "d" * 64,
                                 enforced_request_lifetime_s=5)
        clock = [1000.0]
        a = WindowedAdapter(provider, clock, scope)
        j = EffectJournal(self.db, retention_margin_s=0, trusted_idempotent=TrustedIdempotentRegistry(
            [ConnectorContract(DEDUPE, a.perform, "c", adapter_artifact_digest(a.perform), scope)]))
        for t in (1000.0, 1050.0):                         # two ambiguous dispatches
            clock[0], a.stall = t, True
            with self.assertRaises(EffectOutcomeUnknown):
                j.execute(self.key, a.perform, provider.reconcile, owner="A", connector=DEDUPE, now=t)
        self.assertEqual((j.get(self.key).dispatches, j.get(self.key).first_dispatched_at), (2, 1000.0))
        clock[0] = 1096.0
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            j.execute(self.key, a.perform, provider.reconcile, owner="C", connector=DEDUPE, now=1096.0)
        self.assertEqual(provider.applications(self.key), [])

    def test_missing_first_dispatch_time_refused(self):
        provider = WindowedProvider(os.path.join(self.tmp.name, "m.sqlite"), 3600, 60)
        scope = IdempotencyScope("fake-windowed", "acct-1", "e", "per-key", "1", 3600, "d" * 64,
                                 enforced_request_lifetime_s=60)
        a = WindowedAdapter(provider, [1000.0], scope)
        reg = TrustedIdempotentRegistry([ConnectorContract(DEDUPE, a.perform, "c",
                                                           adapter_artifact_digest(a.perform), scope)])
        j = EffectJournal(self.db, trusted_idempotent=reg, retention_margin_s=0)
        a.stall = True
        with self.assertRaises(EffectOutcomeUnknown):
            j.execute(self.key, a.perform, provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        with _sql(self.db) as c:                           # as for a row migrated from c2.v2
            c.execute("UPDATE effects SET first_dispatched_at=NULL")
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            j.execute(self.key, a.perform, provider.reconcile, owner="B", connector=DEDUPE, now=1001.0)
        self.assertEqual(provider.applications(self.key), [])

    def test_validation(self):
        for bad in (0, -1, True, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                IdempotencyScope("p", "a", "e", "s", "1", 60, "0" * 64, enforced_request_lifetime_s=bad)
        with self.assertRaises(ValueError):
            EffectJournal(self.db, retention_margin_s=-1)
        s1 = IdempotencyScope("p", "a", "e", "s", "1", 60, "0" * 64)
        s2 = IdempotencyScope("p", "a", "e", "s", "1", 60, "0" * 64, enforced_request_lifetime_s=5)
        self.assertNotEqual(s1.canonical(), s2.canonical())   # lifetime is part of the digest

    def test_migration_from_c2_v2_adds_first_dispatch_column(self):
        with _sql(self.db) as c:
            c.execute("ALTER TABLE effects DROP COLUMN first_dispatched_at")
            c.execute("UPDATE meta SET value='c2.v2' WHERE key='schema'")
        Database(self.db)
        with _sql(self.db) as c:
            self.assertEqual(c.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0], "c2.v3")
            cols = [r[1] for r in c.execute("PRAGMA table_info(effects)")]
        self.assertIn("first_dispatched_at", cols)
        self.assertIsNone(self.journal.get(self.key).first_dispatched_at)


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
