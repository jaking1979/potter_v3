"""C2 merge evidence (decision T10): kill mid-task resumes; duplicate trigger
deduped; unknown-outcome effect reconciles before retry; 3 failures escalate;
last-success watchdog flags stale job; C0 approval conformance suite passes
against the production JournalApprovalLedger.

Offline only: the "external system" is a local SQLite file standing in for a
connector that deduplicates on the idempotency key and counts applications.
"""
import contextlib
import functools
import json
import math
import hashlib
import hmac
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
from runtime import (AdapterVerificationError, DeadlineTicketInvalid, DispatchDeadlineSigner,  # noqa: E402
                     EffectKilled, IdempotencyScope, adapter_artifact_digest,
                     current_idempotency_scope)
from runtime._contract import ApprovalRefused, BoundAction  # noqa: E402
from runtime.db import SCHEMA_VERSION  # noqa: E402
import runtime.effects as runtime_effects  # noqa: E402

T = 1_800_000_000.0
# Test-only deadline key held by the journal and the stub providers; adapters
# never see it. Test journals may pin explicit times (trusted test clock).
TEST_SIGNER = DispatchDeadlineSigner(b"test-only-deadline-signing-key", key_id="test")
TestJournal = functools.partial(EffectJournal, trusted_test_clock=True, deadline_signer=TEST_SIGNER)
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
    store, journal, x = TaskStore(db), TestJournal(db), External(ext)
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
    journal = TestJournal(db, dispatch_timeout_s=20,
                            trusted_idempotent=trusted(x) if dedupe else None)
    t = store.claim("w1", LEASE, now=T)
    eff = journal.record_intent(t.id, "send", action(t.id), now=T)
    journal.execute(eff.idempotency_key, x.perform, x.reconcile, owner="w1",
                    connector=DEDUPE if dedupe else CONN, now=T)


def _late_dispatcher(db, ext_path, dedupe, key, started, go, q):
    """Dispatcher A: claims the effect at T, then stalls past its dispatch
    timeout before its external call lands."""
    x = (DedupingExternal if dedupe else External)(ext_path, stall=(started, go))
    journal = TestJournal(db, dispatch_timeout_s=20,
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
        self.journal = TestJournal(self.db)


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
        fenced = TestJournal(self.db, fence=lambda e: e.in_flight_owner == "w1")
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
        journal = TestJournal(self.db, trusted_idempotent=trusted(dedupe))
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
        b = TestJournal(self.db, dispatch_timeout_s=20,
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
                TestJournal(self.db, fence=fence).execute(
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
        journal = TestJournal(self.db, trusted_idempotent=TrustedIdempotentRegistry(
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
        journals = (self.journal, TestJournal(self.db, trusted_idempotent=trusted(other)))
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
        journal = TestJournal(self.db, trusted_idempotent=trusted(dedupe))
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
        journal = TestJournal(self.db, trusted_idempotent=trusted(dedupe))
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
            TestJournal(self.db, trusted_idempotent={DEDUPE: reg[DEDUPE]})
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
        return TestJournal(self.db, trusted_idempotent=TrustedIdempotentRegistry(
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
            self.assertEqual(c.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0], SCHEMA_VERSION)
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
        return TestJournal(self.db, trusted_idempotent=TrustedIdempotentRegistry(
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
        return TestJournal(self.db, trusted_idempotent=TrustedIdempotentRegistry([
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
    """A synthetic provider with a finite dedupe memory. With
    ``enforces_deadlines`` it verifies each request's signed dispatch ticket
    and rejects a bad signature or a request arriving after its deadline;
    otherwise it accepts any request whenever it arrives."""

    def __init__(self, path, retention, enforces_deadlines=True, verifier=TEST_SIGNER):
        self.path, self.retention = str(path), retention
        self.enforces, self.verifier = enforces_deadlines, verifier
        with _sql(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS applied (key TEXT PRIMARY KEY, at REAL, n INTEGER)")
            c.execute("CREATE TABLE IF NOT EXISTS log (key TEXT, at REAL, outcome TEXT)")

    def deliver(self, key, ticket, at):
        """A request carrying ``ticket`` reaches the provider at ``at``."""
        outcome = None
        if self.enforces and not self.verifier.admits(ticket, key, at):   # fail closed
            if not (isinstance(at, (int, float)) and math.isfinite(at)):
                outcome = "rejected-bad-clock"
            else:
                try:
                    body = self.verifier.verify(ticket)
                    outcome = "rejected-bad-ticket" if body["key"] != key else "rejected-expired"
                except DeadlineTicketInvalid:
                    outcome = "rejected-bad-ticket"
        with _sql(self.path) as c:
            if outcome is None:
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
    """Sends ``effect.dispatch_ticket`` at the simulated time in ``clock[0]``.
    With ``stall`` set, the request leaves but the call times out (ambiguous);
    the ticket is kept in ``tickets`` so the test decides when, or whether,
    that original request lands. ``park`` runs between claim and send, to model
    a dispatcher that stalls after its claim. A provider rejection is definite."""

    def __init__(self, provider, clock, scope):
        self.provider, self.clock, self.scope = provider, clock, scope
        self.stall, self.park, self.tickets = False, None, []

    def idempotency_scope(self):
        return self.scope

    def perform(self, effect):
        effect.action()
        self.tickets.append(effect.dispatch_ticket)
        if self.park is not None:
            park, self.park = self.park, None
            park()
        if self.stall:
            self.stall = False
            raise TimeoutError("connector timed out")
        outcome = self.provider.deliver(effect.idempotency_key, effect.dispatch_ticket, self.clock[0])
        if outcome.startswith("rejected"):
            raise EffectFailed(outcome)            # the provider refused it: it will never apply
        return {"outcome": outcome}


def windowed_scope(retention, lifetime):
    return IdempotencyScope("fake-windowed", "acct-1", "https://api.example.invalid", "per-key",
                            "1", retention, "d" * 64, enforced_request_lifetime_s=lifetime)


def windowed_journal(db, provider, sim_time, scope, **kw):
    """``sim_time`` is the adapter's simulated send clock; ``kw`` goes to the journal."""
    a = WindowedAdapter(provider, sim_time, scope)
    kw.setdefault("retention_margin_s", 0)
    return a, TestJournal(db, trusted_idempotent=TrustedIdempotentRegistry(
        [ConnectorContract(DEDUPE, a.perform, "contract:windowed-v1",
                           adapter_artifact_digest(a.perform), scope)]), **kw)


class RetentionWindow(_EffectFixture):
    """A finite dedupe window only excludes a duplicate if every attempt's
    acceptance envelope is inside it: retention >= latest signed deadline
    - earliest claim + margin, with the retry's deadline = its claim + L."""

    def _run(self, retention, lifetime, retry_at, margin=0.0, first_at=1000.0):
        provider = WindowedProvider(os.path.join(self.tmp.name, f"w-{retry_at}.sqlite"), retention,
                                    enforces_deadlines=lifetime is not None)
        scope, clock = windowed_scope(retention, lifetime), [first_at]
        a, ja = windowed_journal(self.db, provider, clock, scope, retention_margin_s=margin)
        a.stall = True                                     # A's request leaves at first_at, then stalls
        with self.assertRaises(EffectOutcomeUnknown):
            ja.execute(self.key, a.perform, provider.reconcile, owner="A", connector=DEDUPE, now=first_at)
        self.assertEqual(ja.get(self.key).first_dispatched_at, first_at)
        clock[0] = retry_at
        b, jb = windowed_journal(self.db, provider, clock, scope, retention_margin_s=margin)  # restart
        try:
            out = jb.execute(self.key, b.perform, provider.reconcile, owner="B", connector=DEDUPE,
                             now=retry_at)
        except EffectOutcomeUnknown:
            out = None
        return provider, jb, out, a

    def test_dolores_scenario_no_enforced_lifetime_refused(self):
        # retention 1 s, no enforced deadline: B at t1002 must not retry.
        provider, jb, out, a = self._run(retention=1, lifetime=None, retry_at=1002.0)
        self.assertIsNone(out)
        self.assertEqual(jb.get(self.key).status, "unknown")
        self.assertEqual((a.tickets, jb.get(self.key).unbounded_dispatch), ([None], 1))
        self.assertEqual(provider.deliver(self.key, None, 1004.0), "applied")    # A lands at t1004
        self.assertEqual(provider.applications(self.key), [1004.0])              # exactly once
        # Sanity: had B applied at t1002, A at t1004 would have been a second application.
        self.assertEqual(provider.deliver("probe", None, 1002.0), "applied")
        self.assertEqual(provider.deliver("probe", None, 1004.0), "applied")
        self.assertEqual(len(provider.applications("probe")), 2)

    def test_enforced_lifetime_but_retention_too_short_refused(self):
        provider, jb, out, a = self._run(retention=1, lifetime=5, retry_at=1002.0)
        self.assertIsNone(out)
        self.assertEqual(jb.reconcile(self.key, provider.reconcile, now=1002.0).status, "unknown")
        self.assertEqual(provider.applications(self.key), [])

    def test_window_boundary_inclusive(self):
        # R=100, L=5, margin=10, first claim 1000: allowed iff (retry+5) - 1000 + 10 <= 100.
        provider, jb, out, a = self._run(retention=100, lifetime=5, retry_at=1085.0, margin=10.0)
        self.assertEqual((out.status, out.dispatches, out.dispatch_deadline), ("succeeded", 2, 1090.0))
        self.assertEqual(provider.deliver(self.key, a.tickets[0], 1090.0), "rejected-expired")  # late A
        self.assertEqual(provider.applications(self.key), [1085.0])

    def test_window_just_past_boundary_refused(self):
        provider, jb, out, a = self._run(retention=100, lifetime=5, retry_at=1085.5, margin=10.0)
        self.assertIsNone(out)
        self.assertEqual(provider.applications(self.key), [])

    def test_margin_is_applied(self):
        provider, jb, out, a = self._run(retention=100, lifetime=5, retry_at=1085.0, margin=10.5)
        self.assertIsNone(out)

    def test_valid_window_positive_control_late_original_deduped(self):
        # Long retention: B retries at t1002; A's original (deadline t1060) lands at t1004, deduped.
        provider, jb, out, a = self._run(retention=3600, lifetime=60, retry_at=1002.0, margin=1.0)
        self.assertEqual(out.status, "succeeded")
        self.assertEqual(provider.deliver(self.key, a.tickets[0], 1004.0), "deduped")
        self.assertEqual(provider.applications(self.key), [1002.0])

    def test_window_anchored_to_first_claim_and_latest_deadline(self):
        # R=100, L=5: A at t1000 and an allowed retry B at t1050 both time out.
        # C at t1096 would stretch the envelope to [1000, 1101]: 101 > 100.
        provider = WindowedProvider(os.path.join(self.tmp.name, "anchor.sqlite"), 100)
        clock = [1000.0]
        a, j = windowed_journal(self.db, provider, clock, windowed_scope(100, 5))
        for t in (1000.0, 1050.0):                         # two ambiguous dispatches
            clock[0], a.stall = t, True
            with self.assertRaises(EffectOutcomeUnknown):
                j.execute(self.key, a.perform, provider.reconcile, owner="A", connector=DEDUPE, now=t)
        eff = j.get(self.key)
        self.assertEqual((eff.dispatches, eff.first_dispatched_at, eff.max_dispatch_deadline),
                         (2, 1000.0, 1055.0))
        clock[0] = 1096.0
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            j.execute(self.key, a.perform, provider.reconcile, owner="C", connector=DEDUPE, now=1096.0)
        self.assertEqual(provider.applications(self.key), [])

    def test_missing_recorded_bounds_refused(self):
        for column in ("first_dispatched_at", "max_dispatch_deadline"):
            with self.subTest(column=column):
                provider = WindowedProvider(os.path.join(self.tmp.name, f"m-{column}.sqlite"), 3600)
                a, j = windowed_journal(self.db, provider, [1000.0], windowed_scope(3600, 60))
                key = j.record_intent(self.task.id, f"send-{column}", action(self.task.id),
                                      now=1000.0).idempotency_key
                a.stall = True
                with self.assertRaises(EffectOutcomeUnknown):
                    j.execute(key, a.perform, provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
                with _sql(self.db) as c:                   # as for a row migrated from before c2.v5
                    c.execute(f"UPDATE effects SET {column}=NULL WHERE idempotency_key=?", (key,))
                with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
                    j.execute(key, a.perform, provider.reconcile, owner="B", connector=DEDUPE, now=1001.0)
                self.assertEqual(provider.applications(key), [])

    def test_validation(self):
        for bad in (0, -1, True, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                IdempotencyScope("p", "a", "e", "s", "1", 60, "0" * 64, enforced_request_lifetime_s=bad)
        with self.assertRaises(ValueError):
            TestJournal(self.db, retention_margin_s=-1)
        s1 = IdempotencyScope("p", "a", "e", "s", "1", 60, "0" * 64)
        s2 = IdempotencyScope("p", "a", "e", "s", "1", 60, "0" * 64, enforced_request_lifetime_s=5)
        self.assertNotEqual(s1.canonical(), s2.canonical())   # lifetime is part of the digest

    def test_migration_from_c2_v2_adds_columns(self):
        with _sql(self.db) as c:
            for col in ("first_dispatched_at", "dispatch_deadline", "dispatch_ticket",
                        "max_dispatch_deadline", "last_claim_at", "unbounded_dispatch"):
                c.execute(f"ALTER TABLE effects DROP COLUMN {col}")
            c.execute("UPDATE meta SET value='c2.v2' WHERE key='schema'")
        Database(self.db)
        with _sql(self.db) as c:
            self.assertEqual(c.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0], SCHEMA_VERSION)
            cols = [r[1] for r in c.execute("PRAGMA table_info(effects)")]
        for col in ("first_dispatched_at", "max_dispatch_deadline", "unbounded_dispatch"):
            self.assertIn(col, cols)
        eff = self.journal.get(self.key)
        self.assertEqual((eff.first_dispatched_at, eff.unbounded_dispatch), (None, 0))


# -- persisted FAILED verdicts must be re-proven (Dolores review of b55df07) -
class FailedVerdictReproof(_EffectFixture):
    """A FAILED verdict accepted on the trusted-window basis is not timeless:
    every dispatch claim re-runs the full check at the claim's own time."""

    def setUp(self):
        super().setUp()
        self.provider = WindowedProvider(os.path.join(self.tmp.name, "fv.sqlite"), 100)
        self.scope = windowed_scope(100, 5)
        self.clock = [1000.0]

    def _journal(self, **kw):
        return windowed_journal(self.db, self.provider, self.clock, self.scope, **kw)

    def _ambiguous_then_reconciled(self, journal=None):
        a, j = journal or self._journal()
        a.stall = True                                     # original leaves at t1000, times out
        with self.assertRaises(EffectOutcomeUnknown):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        eff = j.reconcile(self.key, self.provider.reconcile, now=1001.0)   # inside the window
        self.assertEqual((eff.status, eff.failed_basis), ("failed", "window"))
        return a, j

    def _retry(self, j, a, at):
        self.clock[0] = at
        return j.execute(self.key, a.perform, self.provider.reconcile, owner="B", connector=DEDUPE, now=at)

    def test_dolores_repro_late_original_then_outside_window_refused(self):
        a, j = self._ambiguous_then_reconciled()
        self.assertEqual(self.provider.deliver(self.key, a.tickets[0], 1004.0), "applied")   # lands
        with self.assertRaisesRegex(EffectOutcomeUnknown, "window"):
            self._retry(j, a, 1200.0)
        eff = j.get(self.key)
        self.assertEqual((eff.status, eff.failed_basis, eff.dispatches), ("unknown", None, 1))
        self.assertEqual(self.provider.applications(self.key), [1004.0])            # once, not twice

    def test_restart_after_reconcile_outside_window_refused(self):
        self._ambiguous_then_reconciled()
        b, j2 = self._journal()                                                      # process restart
        with self.assertRaisesRegex(EffectOutcomeUnknown, "window"):
            self._retry(j2, b, 1200.0)
        self.assertEqual(j2.get(self.key).status, "unknown")
        self.assertEqual(self.provider.applications(self.key), [])

    def test_in_window_positive_control(self):
        a, j = self._ambiguous_then_reconciled()
        self.provider.deliver(self.key, a.tickets[0], 1004.0)                        # original lands
        out = self._retry(j, a, 1050.0)                    # envelope [1000, 1055]: 55 <= 100
        self.assertEqual((out.status, out.dispatches), ("succeeded", 2))
        self.assertEqual(self.provider.applications(self.key), [1004.0])            # retry deduped

    def test_claim_time_not_entry_time_is_checked(self):
        t = [1001.0]
        a, j = self._ambiguous_then_reconciled(self._journal(clock=lambda: t[0]))

        def slow_reconcile(effect):                        # not consulted: the row is already FAILED
            return "failed"
        # Entry reads the clock at t1001; the claim happens at t1200 (a slow caller in between).
        orig_load = runtime_effects.load_effect

        def late_load(c, key):
            t[0] = 1200.0
            return orig_load(c, key)
        runtime_effects.load_effect = late_load
        try:
            with self.assertRaisesRegex(EffectOutcomeUnknown, "window"):
                j.execute(self.key, a.perform, slow_reconcile, owner="B", connector=DEDUPE)
        finally:
            runtime_effects.load_effect = orig_load
        self.assertEqual(self.provider.applications(self.key), [])

    def test_slow_reconcile_in_unknown_path_checked_at_claim(self):
        t = [1000.0]
        a, j = self._journal(clock=lambda: t[0])
        a.stall = True
        with self.assertRaises(EffectOutcomeUnknown):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="A", connector=DEDUPE)
        t[0] = 1001.0                                      # inside the window at entry...

        def slow_reconcile(effect):
            t[0] = 1200.0                                  # ...but the read takes until t1200
            return "failed"
        with self.assertRaisesRegex(EffectOutcomeUnknown, "window"):
            j.execute(self.key, a.perform, slow_reconcile, owner="B", connector=DEDUPE)
        self.assertEqual((j.get(self.key).status, self.provider.applications(self.key)), ("unknown", []))

    def test_fence_based_failed_keeps_current_behaviour(self):
        a, j = self._journal(fence=lambda e: True)
        a.stall = True
        with self.assertRaises(EffectOutcomeUnknown):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        self.assertEqual(j.reconcile(self.key, self.provider.reconcile, now=1001.0).failed_basis, "fence")
        out = self._retry(j, a, 1200.0)                    # fenced: no window re-check
        self.assertEqual(out.status, "succeeded")

    def test_definite_failure_needs_no_window(self):
        def reject(effect):
            raise EffectFailed("422 invalid recipient")   # provider rejected: never applies
        out = self.journal.execute(self.key, reject, self.provider.reconcile, owner="A", connector=CONN, now=1000.0)
        self.assertEqual((out.status, out.failed_basis), ("failed", "definite"))
        out = self.journal.execute(self.key, self.ext.perform, self.ext.reconcile, owner="A",
                                   connector=CONN, now=99_999.0)
        self.assertEqual(out.status, "succeeded")

    def test_migrated_failed_row_without_basis_is_reproven(self):
        self._ambiguous_then_reconciled()
        with _sql(self.db) as c:                           # as for a FAILED row migrated from c2.v3
            c.execute("UPDATE effects SET failed_basis=NULL")
        b, j2 = self._journal()
        with self.assertRaisesRegex(EffectOutcomeUnknown, "window"):
            self._retry(j2, b, 1200.0)
        self.assertEqual(self.provider.applications(self.key), [])


# -- signed deadline bound at the claim (Dolores review of f58814e) ----------
class DeadlineBinding(_EffectFixture):
    """The window is only provable if each request carries a provider-enforced
    deadline fixed at its claim, in the same transaction as the check."""

    def setUp(self):
        super().setUp()
        self.provider = WindowedProvider(os.path.join(self.tmp.name, "db.sqlite"), 100)
        self.scope = windowed_scope(100, 5)
        self.clock = [1000.0]

    def _journal(self, **kw):
        kw.setdefault("dispatch_timeout_s", 20)
        return windowed_journal(self.db, self.provider, self.clock, self.scope, **kw)

    def test_claim_binds_signed_deadline_from_claim_time(self):
        a, j = self._journal()
        out = j.execute(self.key, a.perform, self.provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        self.assertEqual((out.status, out.dispatch_deadline, out.max_dispatch_deadline), ("succeeded", 1005.0, 1005.0))
        body = TEST_SIGNER.verify(a.tickets[0])
        self.assertEqual((body["key"], body["n"], body["deadline"]), (self.key, 1, 1005.0))

    def test_t1000_t1050_t1200_schedule_refuses_duplicate(self):
        """A claims at t1000 and parks before sending; B passes the check at
        t1050 and applies; A resumes at t1200 and sends. Before this change both
        applied; now A's request carries the t1005 deadline and is rejected."""
        a, ja = self._journal()
        b, jb = self._journal()
        seen = {}

        def a_parked():                                    # A is stalled between claim and send
            self.clock[0] = 1050.0
            seen["b"] = jb.execute(self.key, b.perform, self.provider.reconcile, owner="B",
                                   connector=DEDUPE, now=1050.0)
            self.clock[0] = 1200.0                         # A resumes
        a.park = a_parked
        out = ja.execute(self.key, a.perform, self.provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        self.assertEqual((seen["b"].status, seen["b"].dispatch_deadline), ("succeeded", 1055.0))
        self.assertEqual(out.status, "succeeded")          # A's late settle does not overwrite B
        self.assertEqual(self.provider.applications(self.key), [1050.0])
        with _sql(self.provider.path) as c:
            log = c.execute("SELECT at, outcome FROM log ORDER BY at").fetchall()
        self.assertEqual(log, [(1050.0, "applied"), (1200.0, "rejected-expired")])

    def test_retry_dispatcher_parked_through_its_own_expiry(self):
        a, ja = self._journal()
        a.stall = True
        with self.assertRaises(EffectOutcomeUnknown):
            ja.execute(self.key, a.perform, self.provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        b, jb = self._journal()

        def b_parked():
            self.clock[0] = 1100.0                         # B stalls past its t1055 deadline
        b.park = b_parked
        out = jb.execute(self.key, b.perform, self.provider.reconcile, owner="B", connector=DEDUPE, now=1050.0)
        self.assertEqual((out.status, out.failed_basis), ("failed", "definite"))   # provider refused it
        self.assertEqual(self.provider.deliver(self.key, a.tickets[0], 1101.0), "rejected-expired")
        self.assertEqual(self.provider.applications(self.key), [])
        # Every earlier request is past its deadline; a later dispatch applies exactly once.
        self.clock[0] = 1120.0
        c, jc = self._journal()
        out = jc.execute(self.key, c.perform, self.provider.reconcile, owner="C", connector=DEDUPE, now=1120.0)
        self.assertEqual(out.status, "succeeded")
        self.assertEqual(self.provider.applications(self.key), [1120.0])

    def test_adapter_cannot_mint_a_later_deadline(self):
        a, j = self._journal()
        forged = []

        class Forger(WindowedAdapter):
            def perform(s, effect):
                d = json.loads(effect.dispatch_ticket)
                d["deadline"] = 10_000.0                   # push its own deadline out
                forged.append(s.provider.deliver(effect.idempotency_key, json.dumps(d), s.clock[0]))
                other = DispatchDeadlineSigner(b"adapter-made-up-key-0123", key_id="test")
                forged.append(s.provider.deliver(effect.idempotency_key,
                                                 other.sign(effect.idempotency_key, 1, 10_000.0), s.clock[0]))
                raise EffectFailed("forgery rejected")
        f = Forger(self.provider, self.clock, self.scope)
        jf = TestJournal(self.db, retention_margin_s=0, trusted_idempotent=TrustedIdempotentRegistry(
            [ConnectorContract(DEDUPE, f.perform, "c", adapter_artifact_digest(f.perform), self.scope)]))
        jf.execute(self.key, f.perform, self.provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        self.assertEqual(forged, ["rejected-bad-ticket", "rejected-bad-ticket"])
        self.assertEqual(self.provider.applications(self.key), [])

    def test_transaction_lock_delay_is_measured_at_claim(self):
        """B passes the check at entry (t1050), then waits for the journal's
        write lock until t1200; the claim reads the clock after the lock and
        refuses."""
        t = [1000.0]
        a, j = self._journal(clock=lambda: t[0])
        a.stall = True
        with self.assertRaises(EffectOutcomeUnknown):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="A", connector=DEDUPE)
        t[0] = 1001.0
        self.assertEqual(j.reconcile(self.key, self.provider.reconcile).failed_basis, "window")
        t[0] = 1050.0                                      # in the window when B starts
        locked, release = threading.Event(), threading.Event()

        def hold_lock():
            c = sqlite3.connect(self.db, timeout=30, isolation_level=None)
            try:
                c.execute("BEGIN IMMEDIATE")
                locked.set()
                release.wait(30)
                t[0] = 1200.0                              # time passes while B is blocked
                c.execute("COMMIT")
            finally:
                c.close()
        holder = threading.Thread(target=hold_lock)
        holder.start()
        self.assertTrue(locked.wait(30))
        threading.Timer(0.2, release.set).start()
        with self.assertRaisesRegex(EffectOutcomeUnknown, "window"):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="B", connector=DEDUPE)
        holder.join(30)
        self.assertEqual((j.get(self.key).status, self.provider.applications(self.key)), ("unknown", []))

    def test_clock_rollback_refused(self):
        a, j = self._journal()
        a.stall = True
        with self.assertRaises(EffectOutcomeUnknown):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        # Unknown path: the clock now reads earlier than the recorded claim.
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="B", connector=DEDUPE, now=990.0)
        # FAILED path: accepted at t1001, then the clock rolls back before the claim.
        self.assertEqual(j.reconcile(self.key, self.provider.reconcile, now=1001.0).failed_basis, "window")
        with self.assertRaisesRegex(EffectOutcomeUnknown, "window"):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="B", connector=DEDUPE, now=995.0)
        self.assertEqual(self.provider.applications(self.key), [])

    def test_explicit_now_is_for_a_trusted_test_clock_only(self):
        runtime = EffectJournal(self.db)                   # a runtime journal: real clock only
        with self.assertRaisesRegex(ValueError, "trusted test clock"):
            runtime.execute(self.key, self.ext.perform, self.ext.reconcile, owner="A",
                            connector=CONN, now=1000.0)
        with self.assertRaisesRegex(ValueError, "trusted test clock"):
            runtime.reconcile(self.key, self.ext.reconcile, now=1000.0)
        self.assertEqual(runtime.get(self.key).status, "pending")
        # Even a trusted test clock cannot use a stale time against recorded claims.
        a, j = self._journal()
        a.stall = True
        with self.assertRaises(EffectOutcomeUnknown):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="B", connector=DEDUPE, now=999.0)

    def test_no_signer_means_no_window_trust(self):
        a = WindowedAdapter(self.provider, self.clock, self.scope)
        j = EffectJournal(self.db, trusted_test_clock=True, retention_margin_s=0,   # no deadline_signer
                          trusted_idempotent=TrustedIdempotentRegistry(
                              [ConnectorContract(DEDUPE, a.perform, "c", adapter_artifact_digest(a.perform),
                                                 self.scope)]))
        a.stall = True
        with self.assertRaises(EffectOutcomeUnknown):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        eff = j.get(self.key)
        self.assertEqual((eff.dispatch_ticket, eff.unbounded_dispatch), (None, 1))
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="B", connector=DEDUPE, now=1001.0)
        # A later journal WITH a signer cannot retroactively bound that dispatch.
        b, j2 = self._journal()
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            j2.execute(self.key, b.perform, self.provider.reconcile, owner="B", connector=DEDUPE, now=1001.0)

    def test_recorded_later_deadline_extends_the_envelope(self):
        # An earlier request whose recorded deadline is later than the retry's
        # own (e.g. a ticket minted under a longer lifetime) still bounds the window.
        a, j = self._journal()
        a.stall = True
        with self.assertRaises(EffectOutcomeUnknown):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        with _sql(self.db) as c:
            c.execute("UPDATE effects SET max_dispatch_deadline=1500.0")
        # Retry at t1010: its own deadline t1015 fits (15 <= 100), the recorded t1500 does not.
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            j.execute(self.key, a.perform, self.provider.reconcile, owner="B", connector=DEDUPE, now=1010.0)
        self.assertEqual(self.provider.applications(self.key), [])

    def test_an_earlier_unbounded_dispatch_blocks_window_trust_forever(self):
        # Dispatch 1 has no deadline (no signer) and is ambiguous; a fence lets
        # dispatch 2 go out WITH a deadline, also ambiguous. Dispatch 1's request
        # may still land at any time, so no window can cover a third attempt.
        a = WindowedAdapter(self.provider, self.clock, self.scope)
        reg = TrustedIdempotentRegistry([ConnectorContract(DEDUPE, a.perform, "c",
                                                           adapter_artifact_digest(a.perform), self.scope)])
        unsigned = EffectJournal(self.db, trusted_test_clock=True, retention_margin_s=0,
                                 trusted_idempotent=reg, fence=lambda e: True)
        signed_fenced = TestJournal(self.db, retention_margin_s=0, trusted_idempotent=reg,
                                    fence=lambda e: True)
        signed = TestJournal(self.db, retention_margin_s=0, trusted_idempotent=reg)
        a.stall = True
        with self.assertRaises(EffectOutcomeUnknown):
            unsigned.execute(self.key, a.perform, self.provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        self.assertEqual(unsigned.reconcile(self.key, self.provider.reconcile, now=1001.0).failed_basis, "fence")
        a.stall = True
        with self.assertRaises(EffectOutcomeUnknown):
            signed_fenced.execute(self.key, a.perform, self.provider.reconcile, owner="B", connector=DEDUPE,
                                  now=1002.0)
        eff = signed.get(self.key)
        self.assertEqual((eff.dispatches, eff.max_dispatch_deadline, eff.unbounded_dispatch), (2, 1007.0, 1))
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            signed.execute(self.key, a.perform, self.provider.reconcile, owner="C", connector=DEDUPE, now=1003.0)
        self.assertEqual(self.provider.applications(self.key), [])

    def test_signer_and_ticket_validation(self):
        with self.assertRaises(ValueError):
            DispatchDeadlineSigner(b"short")
        with self.assertRaises(TypeError):
            EffectJournal(self.db, deadline_signer="key")
        for bad in (None, "not json", "{}", json.dumps({"key": "k", "n": 1, "deadline": 1.0, "kid": "test",
                                                         "sig": "0" * 64})):
            with self.subTest(bad=bad), self.assertRaises(DeadlineTicketInvalid):
                TEST_SIGNER.verify(bad)


# -- E2 kill seam checked atomically with the claim ---------------------------
class KillSwitch(_EffectFixture):
    def _journal(self, kill):
        return TestJournal(self.db, kill_switch=kill)

    def test_killed_at_claim_dispatches_nothing(self):
        for kill in (lambda e: True, lambda e: None, lambda e: 1 / 0):     # True, non-bool, error
            with self.subTest(kill=kill), self.assertRaises(EffectKilled):
                self._journal(kill).execute(self.key, self._never, self.ext.reconcile, owner="A",
                                            connector=CONN, now=T)
        eff = self.journal.get(self.key)
        self.assertEqual((eff.status, eff.dispatches, eff.in_flight_owner), ("pending", 0, None))

    def test_kill_is_evaluated_under_the_claim_lock(self):
        probes = []

        def kill(eff):
            c = sqlite3.connect(self.db, timeout=0, isolation_level=None)
            try:
                c.execute("BEGIN IMMEDIATE")               # another writer cannot get in
                probes.append("got lock")
                c.execute("ROLLBACK")
            except sqlite3.OperationalError as exc:
                probes.append(str(exc))
            finally:
                c.close()
            return False
        out = self._journal(kill).execute(self.key, self.ext.perform, self.ext.reconcile, owner="A",
                                          connector=CONN, now=T)
        self.assertEqual(out.status, "succeeded")
        # The claim-time check runs while the claim transaction holds the
        # write lock; the second, pre-send check runs after that commit.
        self.assertEqual(len(probes), 2, probes)
        self.assertIn("locked", probes[0])
        self.assertEqual(probes[1], "got lock")

    def test_kill_flipped_after_claim_before_send_reverts_claim(self):
        calls = []

        def kill(eff):
            calls.append(eff.status)
            return len(calls) > 1                          # allowed at claim, killed before send
        with self.assertRaisesRegex(EffectKilled, "before send"):
            self._journal(kill).execute(self.key, self._never, self.ext.reconcile, owner="A",
                                        connector=CONN, now=T)
        eff = self.journal.get(self.key)
        self.assertEqual(calls, ["pending", "unknown"])
        self.assertEqual((eff.status, eff.in_flight_owner, eff.dispatch_ticket), ("pending", None, None))
        out = self.journal.execute(self.key, self.ext.perform, self.ext.reconcile, owner="A",
                                   connector=CONN, now=T + 1)
        self.assertEqual((out.status, self.ext.count()), ("succeeded", 1))


# -- non-finite inputs (Dolores review of 0185f96) -----------------------------
NONFINITE = (float("nan"), float("inf"), float("-inf"))


class NonFiniteJobs(_TmpDB):
    def test_register_rejects_non_finite_and_out_of_range(self):
        bad = [*NONFINITE, 0, -1, True, "60", 1e300]
        for v in bad:
            with self.subTest(cadence=v), self.assertRaises(ValueError):
                self.store.register_job("j", v, now=T)
            with self.subTest(tolerance=v), self.assertRaises(ValueError):
                self.store.register_job("j", 60, tolerance=v, now=T)
        with self.assertRaises(ValueError):
            self.store.register_job("j", 60, tolerance=0.5, now=T)
        self.assertIsNone(self.store.job("j"))                     # nothing was written

    def test_invalid_update_leaves_valid_row_unchanged(self):
        self.store.register_job("j", 60, tolerance=2, now=T)
        for v in NONFINITE:
            with self.subTest(v=v):
                with self.assertRaises(ValueError):
                    self.store.register_job("j", v, now=T + 1)
                with self.assertRaises(ValueError):
                    self.store.register_job("j", 60, tolerance=v, now=T + 1)
        j = TaskStore(self.db).job("j")                            # after a restart
        self.assertEqual((j.cadence_s, j.tolerance), (60.0, 2.0))
        self.assertEqual([x.name for x in TaskStore(self.db).stale_jobs(T + 121)], ["j"])

    def test_persisted_bad_row_is_flagged_and_others_still_evaluated(self):
        self.store.register_job("fresh", 3600, now=T)
        self.store.register_job("stale", 60, now=T - 10_000)
        self.store.register_job("inf-cadence", 60, now=T)
        self.store.register_job("overflow", 60, now=T)
        self.store.register_job("inf-tol", 60, now=T)
        with _sql(self.db) as c:                                   # rows written before validation
            c.execute("UPDATE jobs SET cadence_s=? WHERE name='inf-cadence'", (float("inf"),))
            c.execute("UPDATE jobs SET cadence_s=1e308, tolerance=1e10 WHERE name='overflow'")
            # +inf passes the table's CHECK (tolerance >= 1); -inf and NaN cannot be stored.
            c.execute("UPDATE jobs SET tolerance=? WHERE name='inf-tol'", (float("inf"),))
        out = {j.name: j.fault for j in TaskStore(self.db).stale_jobs(T + 100)}   # restart
        self.assertEqual(set(out), {"stale", "inf-cadence", "overflow", "inf-tol"})
        self.assertIsNone(out["stale"])                            # a normal stale job, no fault
        self.assertIn("not finite", out["inf-cadence"])
        self.assertIn("overflows", out["overflow"])
        self.assertIn("not finite", out["inf-tol"])
        self.assertNotIn("fresh", out)

    def test_non_finite_evaluation_clock_flags_every_job(self):
        self.store.register_job("a", 60, now=T)
        self.store.register_job("b", 3600, now=T)
        for t in NONFINITE:
            with self.subTest(t=t):
                out = self.store.stale_jobs(t)
                self.assertEqual([j.name for j in out], ["a", "b"])
                self.assertTrue(all("clock" in j.fault for j in out))
                out = TaskStore(self.db, clock=lambda t=t: t).stale_jobs()
                self.assertEqual(len(out), 2)
        with self.assertRaises(ValueError):
            self.store.stale_jobs(T, max_age=float("inf"))

    def test_finite_positive_control(self):
        self.store.register_job("j", 60, tolerance=1.5, now=T)
        self.assertEqual(self.store.stale_jobs(T + 90), [])
        self.assertEqual([j.name for j in self.store.stale_jobs(T + 91)], ["j"])


class NonFiniteLeases(_TmpDB):
    def setUp(self):
        super().setUp()
        self.task, _ = self.store.submit("lease", "x", now=T)

    def test_claim_rejects_invalid_lease_before_any_write(self):
        for v in (*NONFINITE, 0, -5, True, 10 * 86_400):
            with self.subTest(lease=v), self.assertRaises(ValueError):
                self.store.claim("w", v, now=T)
        t = self.store.get(self.task.id)
        self.assertEqual((t.state, t.attempts, t.lease_owner), (QUEUED, 0, None))

    def test_heartbeat_rejects_invalid_lease_before_any_write(self):
        self.store.claim("w", LEASE, now=T)
        for v in (*NONFINITE, 0, 10 * 86_400):
            with self.subTest(lease=v), self.assertRaises(ValueError):
                self.store.heartbeat(self.task.id, "w", v, now=T + 1)
        self.assertEqual(self.store.get(self.task.id).lease_expires_at, T + LEASE)

    def test_invalid_clock_during_claim_writes_nothing(self):
        for t in NONFINITE:
            with self.subTest(t=t):
                with self.assertRaises(ValueError):
                    TaskStore(self.db, clock=lambda t=t: t).claim("w", LEASE)
                with self.assertRaises(ValueError):
                    self.store.claim("w", LEASE, now=t)
        self.assertEqual(self.store.get(self.task.id).state, QUEUED)

    def test_persisted_non_finite_lease_is_reclaimable_after_restart(self):
        for stored in (float("inf"), float("nan"), T + 10 * 86_400):  # NaN is stored as NULL
            with self.subTest(stored=stored):
                store = TaskStore(self.db, escalate=self.escalations.append, escalate_after=10)
                t = store.claim("w", LEASE, now=T)
                self.assertIsNotNone(t)
                with _sql(self.db) as c:
                    c.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (stored, t.id))
                restarted = TaskStore(self.db, escalate_after=10)
                with self.assertRaises(LeaseLost):                 # the holder cannot keep it
                    restarted.heartbeat(t.id, "w", LEASE, now=T + 1)
                again = restarted.claim("w2", LEASE, now=T + 1)    # treated as expired
                self.assertEqual((again.id, again.lease_owner, again.lease_expires_at),
                                 (t.id, "w2", T + 1 + LEASE))
                restarted.fail(t.id, "w2", "reset for next case", now=T + 1)

    def test_finite_positive_control(self):
        self.store.claim("w", LEASE, now=T)
        self.store.heartbeat(self.task.id, "w", LEASE, now=T + 10)
        self.assertIsNone(self.store.claim("w2", LEASE, now=T + 39))
        self.assertEqual(self.store.claim("w2", LEASE, now=T + 41).lease_owner, "w2")


class LeaseClockUnderLock(_TmpDB):
    """Trusted time is sampled after the write lock is held (Dolores review
    of 0564323): a lease operation that waits on the lock past its lease's
    expiry is refused, and the reclaim at that time succeeds."""

    def setUp(self):
        super().setUp()
        self.t = [T]
        self.timed = TaskStore(self.db, clock=lambda: self.t[0], escalate_after=10)

    def _blocked(self, op, advance_to):
        """Run ``op`` while another connection holds the write lock; the clock
        advances to ``advance_to`` before the lock is released."""
        locked, release = threading.Event(), threading.Event()

        def holder():
            c = sqlite3.connect(self.db, timeout=30, isolation_level=None)
            try:
                c.execute("BEGIN IMMEDIATE")
                locked.set()
                release.wait(30)
                self.t[0] = advance_to
                c.execute("COMMIT")
            finally:
                c.close()
        th = threading.Thread(target=holder)
        th.start()
        self.assertTrue(locked.wait(30))
        threading.Timer(0.2, release.set).start()
        try:
            return op()
        finally:
            th.join(30)

    def test_lease_ops_waiting_on_the_lock_past_expiry_are_refused(self):
        ops = {"heartbeat": lambda tid: self.timed.heartbeat(tid, "w", LEASE),
               "checkpoint": lambda tid: self.timed.checkpoint(tid, "w", {"i": 1}),
               "complete": lambda tid: self.timed.complete(tid, "w"),
               "fail": lambda tid: self.timed.fail(tid, "w", "boom"),
               "wait_for_approval": lambda tid: self.timed.wait_for_approval(tid, "w")}
        for i, (name, op) in enumerate(ops.items()):
            with self.subTest(op=name):
                base = T + i * 1000
                self.t[0] = base
                task, _ = self.timed.submit(f"lock:{name}", "x")
                self.assertEqual(self.timed.claim("w", LEASE).id, task.id)   # lease to base+30
                self.t[0] = base + 20                      # the worker calls in time...
                with self.assertRaises(LeaseLost):         # ...but gets the lock at base+40
                    self._blocked(lambda: op(task.id), advance_to=base + 40)
                again = self.timed.claim("w2", LEASE)      # the reclaim at base+40 succeeds
                self.assertEqual((again.id, again.lease_owner), (task.id, "w2"))
                self.assertEqual(again.lease_expires_at, base + 40 + LEASE)
                self.timed.complete(task.id, "w2")

    def test_lock_wait_within_the_lease_still_succeeds(self):
        task, _ = self.timed.submit("lock:ok", "x")
        self.timed.claim("w", LEASE)
        self.t[0] = T + 5
        out = self._blocked(lambda: self.timed.heartbeat(task.id, "w", LEASE), advance_to=T + 20)
        self.assertEqual(out.lease_expires_at, T + 20 + LEASE)       # extended from lock time

    def test_claim_samples_after_the_lock(self):
        task, _ = self.timed.submit("lock:claim", "x")
        self.timed.claim("w", LEASE)
        self.t[0] = T + 20                                 # still leased when B starts...
        got = self._blocked(lambda: self.timed.claim("w2", LEASE), advance_to=T + 40)
        self.assertEqual((got.id, got.lease_owner), (task.id, "w2"))  # ...expired once it holds the lock

    def test_clock_rollback_refused_for_lease_holder(self):
        task, _ = self.store.submit("rollback", "x", now=T)
        self.store.claim("w", LEASE, now=T)
        self.store.heartbeat(task.id, "w", LEASE, now=T + 10)       # last write at T+10, lease to T+40
        for name, op in (("heartbeat", lambda: self.store.heartbeat(task.id, "w", LEASE, now=T + 5)),
                         ("checkpoint", lambda: self.store.checkpoint(task.id, "w", {}, now=T + 9)),
                         ("complete", lambda: self.store.complete(task.id, "w", now=T + 1))):
            with self.subTest(op=name), self.assertRaisesRegex(LeaseLost, "rollback"):
                op()
        t = TaskStore(self.db).get(task.id)                # restart: nothing changed
        self.assertEqual((t.state, t.lease_expires_at, t.updated_at), (RUNNING, T + 40, T + 10))
        self.assertEqual(self.store.complete(task.id, "w", now=T + 11).state, DONE)   # forward clock ok


class JobUpperLimits(_TmpDB):
    def test_finite_over_limit_rows_flagged_after_restart(self):
        from runtime.tasks import MAX_CADENCE_S, MAX_TOLERANCE
        for name in ("huge-cadence", "huge-tolerance", "bounded", "stale"):
            self.store.register_job(name, 60, now=T)
        with _sql(self.db) as c:                           # finite rows above the limits
            c.execute("UPDATE jobs SET cadence_s=1e20, tolerance=1 WHERE name='huge-cadence'")
            c.execute("UPDATE jobs SET tolerance=2000 WHERE name='huge-tolerance'")
            c.execute("UPDATE jobs SET cadence_s=?, tolerance=? WHERE name='bounded'",
                      (MAX_CADENCE_S, MAX_TOLERANCE))      # exactly at the limits: valid
        out = {j.name: j.fault for j in TaskStore(self.db).stale_jobs(T + 100)}   # restart
        self.assertEqual(out, {"huge-cadence": "cadence_s/tolerance out of range",
                               "huge-tolerance": "cadence_s/tolerance out of range",
                               "stale": None})             # bounded is valid and not stale
        with self.assertRaises(ValueError):                # registration enforces the same limits
            self.store.register_job("x", 1e20, now=T)
        with self.assertRaises(ValueError):
            self.store.register_job("x", 60, tolerance=2000, now=T)
        self.assertEqual(self.store.register_job("x", MAX_CADENCE_S, tolerance=MAX_TOLERANCE,
                                                 now=T).cadence_s, MAX_CADENCE_S)


class NonFiniteDeadlines(_EffectFixture):
    def _raw_ticket(self, body):
        """A ticket MAC'd exactly as the old signer did (NaN/Infinity allowed)."""
        msg = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = hmac.new(b"test-only-deadline-signing-key", msg, hashlib.sha256).hexdigest()
        return json.dumps({**body, "sig": sig}, sort_keys=True, separators=(",", ":"))

    def test_sign_rejects_non_finite_and_malformed(self):
        for v in NONFINITE:
            with self.subTest(deadline=v), self.assertRaises(ValueError):
                TEST_SIGNER.sign("k", 1, v)
        for args in (("", 1, 1.0), ("k", 0, 1.0), ("k", True, 1.0), ("k", "1", 1.0), ("k", 1, "1.0"),
                     ("k", 1, True)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                TEST_SIGNER.sign(*args)

    def test_verify_rejects_non_finite_even_with_valid_mac(self):
        for v in NONFINITE:
            ticket = self._raw_ticket({"key": "k", "n": 1, "deadline": v, "kid": "test"})
            with self.subTest(v=v), self.assertRaises(DeadlineTicketInvalid):
                TEST_SIGNER.verify(ticket)
            self.assertFalse(TEST_SIGNER.admits(ticket, "k", 0.0))

    def test_overflowing_literal_and_stray_constants_rejected(self):
        # "1e400" is valid JSON with no NaN/Infinity token, yet parses to inf.
        good = json.loads(TEST_SIGNER.sign("k", 1, 9.0))
        overflow = json.dumps({**good, "deadline": 1.0}).replace('"deadline": 1.0', '"deadline": 1e400')
        with self.assertRaises(DeadlineTicketInvalid):
            TEST_SIGNER.verify(overflow)
        self.assertFalse(TEST_SIGNER.admits(overflow, "k", 0.0))
        # A NaN token anywhere in the ticket, even in an unsigned extra field, is refused.
        stray = TEST_SIGNER.sign("k", 1, 9.0)[:-1] + ',"x":NaN}'
        with self.assertRaises(DeadlineTicketInvalid):
            TEST_SIGNER.verify(stray)
        self.assertEqual(TEST_SIGNER.verify(TEST_SIGNER.sign("k", 1, 9.0))["deadline"], 9.0)

    def test_verify_checks_field_types(self):
        for body in ({"key": 7, "n": 1, "deadline": 9.0, "kid": "test"},
                     {"key": "k", "n": "1", "deadline": 9.0, "kid": "test"},
                     {"key": "k", "n": True, "deadline": 9.0, "kid": "test"},
                     {"key": "k", "n": 1, "deadline": "9", "kid": "test"},
                     {"key": "k", "n": 1, "deadline": 9.0, "kid": 1}):
            with self.subTest(body=body), self.assertRaises(DeadlineTicketInvalid):
                TEST_SIGNER.verify(self._raw_ticket(body))
        with self.assertRaises(DeadlineTicketInvalid):
            TEST_SIGNER.verify("[1, 2]")

    def test_reference_provider_fails_closed_on_non_finite_clock(self):
        ticket = TEST_SIGNER.sign(self.key, 1, 1005.0)
        provider = WindowedProvider(os.path.join(self.tmp.name, "nf.sqlite"), 100)
        for at in (*NONFINITE, None, "1000"):
            with self.subTest(at=at):
                self.assertFalse(TEST_SIGNER.admits(ticket, self.key, at))
        self.assertEqual(provider.deliver(self.key, ticket, float("nan")), "rejected-bad-clock")
        self.assertEqual(provider.applications(self.key), [])

    def test_persisted_non_finite_ticket_rejected_after_restart(self):
        provider = WindowedProvider(os.path.join(self.tmp.name, "pt.sqlite"), 100)
        bad = self._raw_ticket({"key": self.key, "n": 1, "deadline": float("inf"), "kid": "test"})
        with _sql(self.db) as c:                                   # a ticket minted before the fix
            c.execute("UPDATE effects SET dispatch_ticket=? WHERE idempotency_key=?", (bad, self.key))
        stored = TestJournal(self.db).get(self.key).dispatch_ticket  # restart
        self.assertEqual(provider.deliver(self.key, stored, 1e12), "rejected-bad-ticket")
        self.assertEqual(provider.applications(self.key), [])

    def test_invalid_journal_clock_during_claim_writes_nothing(self):
        for t in NONFINITE:
            with self.subTest(t=t):
                j = TestJournal(self.db, clock=lambda t=t: t)
                with self.assertRaisesRegex(ValueError, "finite"):
                    j.execute(self.key, self._never, self.ext.reconcile, owner="A", connector=CONN)
                with self.assertRaisesRegex(ValueError, "finite"):
                    self.journal.execute(self.key, self._never, self.ext.reconcile, owner="A",
                                         connector=CONN, now=t)
        eff = self.journal.get(self.key)
        self.assertEqual((eff.status, eff.dispatches, eff.dispatch_ticket), ("pending", 0, None))

    def test_persisted_non_finite_bounds_never_window_trusted(self):
        provider = WindowedProvider(os.path.join(self.tmp.name, "pb.sqlite"), 100)
        a, j = windowed_journal(self.db, provider, [1000.0], windowed_scope(100, 5))
        a.stall = True
        with self.assertRaises(EffectOutcomeUnknown):
            j.execute(self.key, a.perform, provider.reconcile, owner="A", connector=DEDUPE, now=1000.0)
        with _sql(self.db) as c:
            c.execute("UPDATE effects SET max_dispatch_deadline=?", (float("-inf"),))
        with self.assertRaisesRegex(EffectOutcomeUnknown, "trusted"):
            j.execute(self.key, a.perform, provider.reconcile, owner="B", connector=DEDUPE, now=1001.0)

    def test_finite_controls_including_a_normally_expired_ticket(self):
        ticket = TEST_SIGNER.sign(self.key, 1, 1005.0)
        self.assertEqual(TEST_SIGNER.verify(ticket)["deadline"], 1005.0)
        self.assertTrue(TEST_SIGNER.admits(ticket, self.key, 1005.0))      # at the deadline
        self.assertFalse(TEST_SIGNER.admits(ticket, self.key, 1005.001))   # normally expired
        self.assertFalse(TEST_SIGNER.admits(ticket, "other-key", 1000.0))
        provider = WindowedProvider(os.path.join(self.tmp.name, "fc.sqlite"), 100)
        self.assertEqual(provider.deliver(self.key, ticket, 1006.0), "rejected-expired")
        self.assertEqual(provider.deliver(self.key, ticket, 1004.0), "applied")


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
            journal = TestJournal(db)
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
