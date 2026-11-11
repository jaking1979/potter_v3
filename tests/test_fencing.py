"""C4 merge evidence (decision T35 item 10): a stale-epoch consequential call is
refused; a returning node starts fenced; a promoted node starts with effects
paused.

Offline only. Two "containers" are two spawned processes, each with its own
journal SQLite file in a temp dir. Shared files stand in for the epoch
authority and for the external system (StubConnector). The replica is a
directory of snapshot files written by FileCopyReplicator (no Litestream).
"""
import contextlib
import importlib.util
import multiprocessing
import threading
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_policy_engine as tpe  # noqa: E402  (C1 fixtures: registry, approvals)
from runtime.fencing import _pass_gate  # noqa: E402  (test seam helper)
from runtime import (ACTIVE, FENCED, PAUSED, AuthorityUnavailable, BoundaryVerificationError,
                     Database, EffectFailed, EffectJournal, FencedBoundaryContract,
                     FencedBoundaryRegistry, FencingScope, boundary_artifact_digest,
                     EffectConflict, EffectOutcomeUnknown,  # noqa: E402
                     EpochAuthority, FencedEffects, FileCopyReplicator, FencingToken, Node,
                     NodeNotActive, PromotionConflict, StaleEpoch, StaleEpochRefused,
                     StubConnector, UnresolvedEffects, owner_epoch)
from runtime._contract import BoundAction  # noqa: E402

SPAWN = multiprocessing.get_context("spawn")
TASK = "task-c4"
WAIT = 60


@contextlib.contextmanager
def failing(ext, message="timeout"):
    """The stub's reviewed adapter raises ConnectionError before the boundary:
    an ambiguous transport failure (a data seam, not injected code)."""
    ext.fail_before_apply = message
    try:
        yield
    finally:
        ext.fail_before_apply = None


class Parked:
    """Runs ``call`` in a thread that parks at the stub's Event gate, so the
    test thread can act (as node B) while A is stalled. No sleeps."""

    def __init__(self, ext, call, gate="gate_before_apply"):
        self.ext, self.call, self.gate = ext, call, gate
        self.reached, self.resume = threading.Event(), threading.Event()
        self.result = self.error = None

    def __enter__(self):
        setattr(self.ext, self.gate, (self.reached, self.resume))

        def run():
            try:
                self.result = self.call()
            except BaseException as e:
                self.error = e
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        if not self.reached.wait(WAIT):
            raise AssertionError("dispatch never reached the gate")
        return self

    def __exit__(self, *exc):
        self.resume.set()
        self.thread.join(WAIT)
        setattr(self.ext, self.gate, None)
        return False


# "Trusted configuration": the reviewed stub implementation's artifact digest,
# pinned once when the test module is imported (before any mutation).
STUB_ARTIFACT = boundary_artifact_digest(StubConnector.perform)


def fenced(ext):
    """The registry a node needs before its fence can vouch for the stub."""
    return FencedBoundaryRegistry([ext.contract(STUB_ARTIFACT)])


def action(task_id=TASK, n=1):
    return BoundAction(task_id, "gmail.send", "send", "fake://external", {"n": n})


def paths(tmp):
    return SimpleNamespace(auth=os.path.join(tmp, "authority.sqlite"),
                           a=os.path.join(tmp, "node-a.sqlite"),
                           b=os.path.join(tmp, "node-b.sqlite"),
                           replica=os.path.join(tmp, "replica"))


def effect_rows(db_path):
    c = sqlite3.connect(db_path)
    try:
        return c.execute("SELECT * FROM effects ORDER BY idempotency_key").fetchall()
    finally:
        c.close()


# -- spawned nodes (module level so `spawn` can import them) ------------------
def _node_a(p, mode, stalled, resume, q):
    """Primary at epoch 1. Stalls either before calling execute or inside
    perform (after the journal claim, before the external call)."""
    try:
        auth = EpochAuthority(p.auth)
        ext = StubConnector(auth)
        node = Node("node-a", auth, p.a, boundaries=fenced(ext))
        node.promote()
        node.unpause(ext.reconcile)
        eff = node.journal.record_intent(TASK, "send", action())
        rep = FileCopyReplicator(p.replica)
        rep.replicate(p.a)
        fx = FencedEffects(node)
        out = {"epoch": node.epoch, "key": eff.idempotency_key}

        if mode == "before_claim":
            stalled.set()
            resume.wait(WAIT)
            out["state_before"] = node.state           # still believes it is ACTIVE
            try:
                fx.execute(eff.idempotency_key, ext.perform, ext.reconcile, connector=ext.identity)
                out["first"] = "dispatched"
            except StaleEpoch as e:
                out["first"] = type(e).__name__
        else:
            ext.gate_before_apply = (stalled, resume)   # GC pause / VM freeze, in the adapter
            try:
                fx.execute(eff.idempotency_key, ext.perform, ext.reconcile, connector=ext.identity)
                out["first"] = "dispatched"
            except StaleEpoch as e:
                out["first"] = type(e).__name__

        out["status_after"] = node.journal.get(eff.idempotency_key).status
        out["state_after"] = node.state
        try:                                           # it tries again: journal side
            fx.execute(eff.idempotency_key, ext.perform, ext.reconcile, connector=ext.identity)
            out["second"] = "dispatched"
        except (StaleEpoch, NodeNotActive) as e:
            out["second"] = type(e).__name__
        q.put(out)
    except BaseException as e:  # surface failures to the parent
        q.put({"error": repr(e)})
        raise


def _node_b(p, q):
    """Standby: restore A's replica, take over at epoch 2, reconcile, unpause, run."""
    try:
        auth = EpochAuthority(p.auth)
        ext = StubConnector(auth)
        FileCopyReplicator(p.replica).restore(p.b)
        node = Node("node-b", auth, p.b, boundaries=fenced(ext))
        out = {"start": node.state, "rejoin": node.rejoin()}
        out["epoch"] = node.promote()
        out["after_promote"] = node.state
        fx = FencedEffects(node)
        key = node.journal.for_step(TASK, "send").idempotency_key
        out["restored_status"] = node.journal.get(key).status
        try:
            fx.execute(key, ext.perform, ext.reconcile, connector=ext.identity)
            out["paused_dispatch"] = "dispatched"
        except NodeNotActive as e:
            out["paused_dispatch"] = e.state
        out["unpause"] = node.unpause(ext.reconcile)
        out["after_unpause"] = node.state
        out["final"] = fx.execute(key, ext.perform, ext.reconcile, connector=ext.identity).status
        q.put(out)
    except BaseException as e:
        q.put({"error": repr(e)})
        raise


def _promote_race(auth_path, barrier, q, i):
    auth = EpochAuthority(auth_path)
    seen = auth.current().epoch
    barrier.wait(WAIT)
    try:
        q.put((i, auth.promote(f"node-{i}", seen)))
    except PromotionConflict:
        q.put((i, None))


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = paths(self.tmp.name)
        self.auth = EpochAuthority(self.p.auth)
        self.ext = StubConnector(self.auth)

    def active_node(self, node_id="node-a", db=None):
        node = Node(node_id, self.auth, db or self.p.a, boundaries=fenced(self.ext))
        node.promote()
        node.unpause(self.ext.reconcile)
        return node

    @staticmethod
    def get(q):
        out = q.get(timeout=WAIT)
        if "error" in out:
            raise AssertionError(f"child failed: {out['error']}")
        return out


# -- 1. stalled old leader resumes after takeover -----------------------------
class StalledLeaderTakeover(_Tmp):
    def _run(self, mode):
        stalled, resume, qa, qb = SPAWN.Event(), SPAWN.Event(), SPAWN.Queue(), SPAWN.Queue()
        a = SPAWN.Process(target=_node_a, args=(self.p, mode, stalled, resume, qa))
        a.start()
        try:
            self.assertTrue(stalled.wait(WAIT), "node A never stalled")
            FileCopyReplicator(self.p.replica).replicate(self.p.a)   # ships A's current journal
            b = SPAWN.Process(target=_node_b, args=(self.p, qb))
            b.start()
            out_b = self.get(qb)
            b.join(WAIT)
            resume.set()
            out_a = self.get(qa)
            a.join(WAIT)
        finally:
            resume.set()
            for proc in (a,):
                if proc.is_alive():
                    proc.kill()
        return out_a, out_b

    def _assert_takeover(self, out_a, out_b):
        self.assertEqual(out_a["epoch"], 1)
        self.assertEqual(out_b["start"], FENCED)
        self.assertFalse(out_b["rejoin"])               # restored replica cannot rejoin
        self.assertEqual(out_b["epoch"], 2)
        self.assertEqual(out_b["after_promote"], PAUSED)
        self.assertEqual(out_b["paused_dispatch"], PAUSED)
        self.assertEqual(out_b["after_unpause"], ACTIVE)
        self.assertEqual(out_b["final"], "succeeded")
        # exactly one external effect, landed by the new primary
        self.assertEqual(self.ext.count(), 1)
        self.assertEqual(self.ext.applied(), [(out_a["key"], "node-b", 2)])
        self.assertEqual(self.auth.current().epoch, 2)
        self.assertEqual(self.auth.current().holder, "node-b")

    def test_stall_inside_perform_refused_at_connector_then_journal(self):
        out_a, out_b = self._run("in_perform")
        self._assert_takeover(out_a, out_b)
        # B's replica had A's claim as unknown, owned by epoch 1: released + reconciled
        self.assertEqual(out_b["restored_status"], "unknown")
        self.assertEqual(out_b["unpause"]["released"], [out_a["key"]])
        self.assertEqual(out_b["unpause"]["resolved"], [out_a["key"]])
        # connector side: A's epoch-1 token refused at the boundary
        self.assertEqual(out_a["first"], "StaleEpochRefused")
        refusals = self.ext.refusals()
        self.assertEqual([(k, n, e) for k, n, e, _ in refusals], [(out_a["key"], "node-a", 1)])
        # A's journal records "not applied"; A is fenced; a retry is refused journal side
        self.assertEqual(out_a["status_after"], "failed")
        self.assertEqual(out_a["state_after"], FENCED)
        self.assertEqual(out_a["second"], "NodeNotActive")

    def test_stall_before_dispatch_refused_at_journal(self):
        out_a, out_b = self._run("before_claim")
        self._assert_takeover(out_a, out_b)
        self.assertEqual(out_b["restored_status"], "pending")
        # A still thought it was ACTIVE; the epoch check against the authority refused it
        self.assertEqual(out_a["state_before"], ACTIVE)
        self.assertEqual(out_a["first"], "StaleEpoch")
        self.assertEqual(out_a["status_after"], "pending")   # never claimed in A's journal
        self.assertEqual(out_a["state_after"], FENCED)
        self.assertEqual(self.ext.refusals(), [])            # never reached the boundary
        self.assertEqual(out_a["second"], "NodeNotActive")


class JournalSideFence(_Tmp):
    def test_fence_inside_claim_transaction_rolls_back_claim(self):
        """Epoch moves between the pre-check and the claim (a stall there): the
        in-transaction fence refuses, nothing is claimed or performed."""
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())
        token = a.token()
        calls, performed = [], []

        def fence():
            calls.append(1)
            if len(calls) == 1:
                Node("node-b", self.auth, self.p.b, boundaries=fenced(self.ext)).promote()   # takeover after the pre-check
                return
            a.require_active(token)

        with self.assertRaises(StaleEpoch):
            a.journal.execute(eff.idempotency_key, lambda e: performed.append(e),
                              self.ext.reconcile, owner=token.owner, connector=self.ext.identity, guard=fence)
        self.assertEqual(len(calls), 2)
        self.assertEqual(performed, [])
        after = a.journal.get(eff.idempotency_key)
        self.assertEqual((after.status, after.dispatches, after.in_flight_owner),
                         ("pending", 0, None))

    def test_dispatch_records_epoch_and_connector_rejects_forged_or_old_tokens(self):
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())
        done = FencedEffects(a).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile, connector=self.ext.identity)
        self.assertEqual(done.status, "succeeded")
        self.assertEqual(done.result["epoch"], 1)
        self.assertEqual(owner_epoch("node-a@epoch:7"), 7)
        self.assertEqual(owner_epoch("w1"), 0)
        Node("node-b", self.auth, self.p.b, boundaries=fenced(self.ext)).promote()
        for bad in (FencingToken("node-a", 1), FencingToken("node-x", 2),
                    FencingToken("node-b", 3), None):
            with self.assertRaises(StaleEpochRefused):
                self.ext.apply("k", bad)
        self.assertTrue(issubclass(StaleEpochRefused, EffectFailed))
        self.assertEqual(self.ext.count(), 1)

    def test_expire_in_flight_requires_matching_owner_and_keeps_owner(self):
        j = EffectJournal(self.p.a)
        eff = j.record_intent(TASK, "send", action())

        def ambiguous(e):
            raise RuntimeError("timeout")
        with self.assertRaises(Exception):
            j.execute(eff.idempotency_key, ambiguous, self.ext.reconcile, owner="x@epoch:1", connector="stub.epoch-gateway")
        # an ambiguous outcome keeps the dispatcher's identity (window expired)
        after = j.get(eff.idempotency_key)
        self.assertEqual((after.status, after.in_flight_owner), ("unknown", "x@epoch:1"))
        with j.db.tx() as c:   # simulate the window still being live
            c.execute("UPDATE effects SET in_flight_until=9e18")
        self.assertFalse(j.expire_in_flight(eff.idempotency_key, "y@epoch:1"))
        self.assertTrue(j.expire_in_flight(eff.idempotency_key, "x@epoch:1"))
        self.assertFalse(j.expire_in_flight(eff.idempotency_key, "x@epoch:1"))  # already ended
        after = j.get(eff.idempotency_key)
        self.assertEqual((after.status, after.in_flight_owner), ("unknown", "x@epoch:1"))
        self.assertLess(after.in_flight_until, 9e18)


# -- 1c. boundary check linearizes with promotion (review fix) ----------------
class BoundaryLinearization(_Tmp):
    """Deterministic interleavings, no sleeps: the connector's check + apply and
    a promotion share one BEGIN IMMEDIATE lock on the authority DB."""

    def test_order_token_read_then_promotion_commits_then_apply_is_refused(self):
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())
        token = a.token()
        self.assertTrue(a.is_current(token))              # 1. A reads epoch 1 as current
        Node("node-b", self.auth, self.p.b, boundaries=fenced(self.ext)).promote()     # 2. promotion to epoch 2 commits
        with self.assertRaises(StaleEpochRefused):        # 3. A attempts the apply
            self.ext.apply(eff.idempotency_key, token)
        self.assertEqual(self.ext.applied(), [])
        self.assertEqual([(k, n, e) for k, n, e, _ in self.ext.refusals()],
                         [(eff.idempotency_key, "node-a", 1)])

    def test_order_promotion_between_journal_check_and_connector_call(self):
        """Same order through FencedEffects: the promotion is injected after the
        journal's last epoch check (inside perform), before the connector."""
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())

        with Parked(self.ext, lambda: FencedEffects(a).execute(
                eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                connector=self.ext.identity)) as job:
            Node("node-b", self.auth, self.p.b, boundaries=fenced(self.ext)).promote()
        self.assertIsInstance(job.error, StaleEpochRefused)
        self.assertEqual(self.ext.count(), 0)
        self.assertEqual(a.journal.get(eff.idempotency_key).status, "failed")
        self.assertEqual(a.state, FENCED)

    def test_order_apply_in_progress_then_promotion_cannot_interleave(self):
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())
        token = a.token()
        contender = EpochAuthority(self.p.auth, busy_timeout_s=0)   # never waits for the lock
        seen = {}
        with Parked(self.ext, lambda: self.ext.apply(eff.idempotency_key, token),
                    gate="gate_in_check") as job:    # parked INSIDE the apply transaction
            seen["epoch_visible"] = contender.current().epoch
            try:
                contender.promote("node-b", 1)
                seen["promoted"] = True
            except AuthorityUnavailable:
                seen["promoted"] = False   # the write lock is held by the apply
        self.assertIsNone(job.error)
        self.assertEqual(job.result["epoch"], 1)
        self.assertEqual(seen, {"epoch_visible": 1, "promoted": False})
        self.assertEqual(self.auth.current().epoch, 1)   # no promotion slipped in mid-apply

        # The apply committed first; now the promotion commits, and epoch 1 is refused.
        self.assertEqual(contender.promote("node-b", 1), 2)
        with self.assertRaises(StaleEpochRefused):
            self.ext.apply("second-key", token)
        self.assertEqual(self.ext.applied(), [(eff.idempotency_key, "node-a", 1)])
        self.assertEqual(self.ext.count("second-key"), 0)
        # and the new primary's reconcile sees the epoch-1 apply: no double application
        self.assertEqual(self.ext.reconcile(a.journal.get(eff.idempotency_key)), "succeeded")

    def test_connector_shares_the_authority_db(self):
        with self.assertRaises(TypeError):
            StubConnector(self.p.auth)                    # a path is not an authority
        c = sqlite3.connect(self.p.auth)
        try:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            c.close()
        self.assertTrue({"authority", "ext_fence", "ext_applied", "ext_refused"} <= tables)


# -- 1b. C2 fence seam filled by the epoch authority ---------------------------
def _stall_past_timeout(p, stalled, resume, q):
    """Dispatcher A at epoch 1 on the SHARED journal; stalls inside perform."""
    try:
        auth = EpochAuthority(p.auth)
        ext = StubConnector(auth)
        node = Node("node-a", auth, p.a, dispatch_timeout_s=0.2, boundaries=fenced(ext))
        node.promote()
        node.unpause(ext.reconcile)
        eff = node.journal.record_intent(TASK, "send", action())

        ext.gate_before_apply = (stalled, resume)      # stalls far past the 0.2 s window
        try:
            FencedEffects(node).execute(eff.idempotency_key, ext.perform, ext.reconcile,
                                        connector=ext.identity)
            q.put({"late": "applied"})
        except StaleEpoch as e:
            q.put({"late": type(e).__name__, "state": node.state})
    except BaseException as e:
        q.put({"error": repr(e)})
        raise


class C2FenceSeam(_Tmp):
    def test_stalled_past_timeout_fence_lets_new_epoch_redispatch_once(self):
        stalled, resume, q = SPAWN.Event(), SPAWN.Event(), SPAWN.Queue()
        a = SPAWN.Process(target=_stall_past_timeout, args=(self.p, stalled, resume, q))
        a.start()
        try:
            self.assertTrue(stalled.wait(WAIT), "dispatcher A never stalled")
            time.sleep(0.5)                            # A's in-flight window has expired
            key = EffectJournal(self.p.a).for_step(TASK, "send").idempotency_key
            row = EffectJournal(self.p.a).get(key)
            self.assertEqual((row.status, row.in_flight_owner), ("unknown", "node-a@epoch:1"))
            self.assertLess(row.in_flight_until, time.time())
            self.assertEqual(row.connector, self.ext.identity)  # recorded by C2 on dispatch
            self.assertIsNone(row.connector_digest)   # untrusted: no contract digest at all

            # C2 alone: an expired claim is not permission; "not applied yet" is refused
            with self.assertRaises(EffectOutcomeUnknown):
                EffectJournal(self.p.a).execute(key, self.ext.perform_unfenced,
                                                self.ext.reconcile, owner="plain-c2", connector=self.ext.identity)
            self.assertEqual(EffectJournal(self.p.a).get(key).status, "unknown")

            # B, same journal: promoted to epoch 2, reconciles "failed", fence says True
            b = Node("node-b", self.auth, self.p.a, boundaries=fenced(self.ext))
            # the stub is NOT in the trusted idempotent registry: only the fence clears it
            self.assertNotIn(self.ext.identity, b.journal._trusted)
            self.assertEqual(len(b.journal._trusted), 0)
            self.assertEqual(b.promote(), 2)
            self.assertFalse(b.c2_fence(row))          # PAUSED and not reconciling
            report = b.unpause(self.ext.reconcile)
            self.assertEqual(report["resolved"], [key])
            self.assertIn((key, "node-a@epoch:1", True), b.fence_log)
            self.assertEqual(b.journal.get(key).status, "failed")
            done = FencedEffects(b).execute(key, self.ext.perform, self.ext.reconcile, connector=self.ext.identity)
            self.assertEqual((done.status, done.dispatches, done.connector),
                             ("succeeded", 2, self.ext.identity))
            self.assertIsNone(done.connector_digest)  # cleared by the fence, not by trust

            resume.set()                               # A's late call reaches the boundary
            out = self.get(q)
            a.join(WAIT)
        finally:
            resume.set()
            if a.is_alive():
                a.kill()
        self.assertEqual(out["late"], "StaleEpochRefused")
        self.assertEqual(out["state"], FENCED)
        self.assertEqual(self.ext.count(), 1)          # exactly one application
        self.assertEqual(self.ext.applied(), [(key, "node-b", 2)])
        self.assertEqual([(k, n, e) for k, n, e, _ in self.ext.refusals()],
                         [(key, "node-a", 1)])
        self.assertEqual(EffectJournal(self.p.a).get(key).status, "succeeded")  # A's settle no-op

    def test_ambiguous_dispatch_of_older_epoch_resolved_at_unpause_then_dispatched(self):
        a = self.active_node()                        # epoch 1, registered stub boundary
        eff = a.journal.record_intent(TASK, "send", action())

        with failing(self.ext), self.assertRaises(EffectOutcomeUnknown):
            FencedEffects(a).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                     connector=self.ext.identity)
        b = self.active_node("node-b", self.p.a)     # unpause reconciles it: fence says yes
        self.assertIn((eff.idempotency_key, "node-a@epoch:1", True), b.fence_log)
        self.assertEqual(b.journal.get(eff.idempotency_key).status, "failed")
        self.assertEqual(FencedEffects(b).execute(eff.idempotency_key, self.ext.perform,
                                                  self.ext.reconcile,
                                                  connector=self.ext.identity).status,
                         "succeeded")
        self.assertEqual(self.ext.count(), 1)

    def test_plain_c2_dispatch_has_no_boundary_binding_and_stays_unresolved(self):
        j = EffectJournal(self.p.a)
        eff = j.record_intent(TASK, "send", action())
        with self.assertRaises(Exception):           # dispatched around FencedEffects
            j.execute(eff.idempotency_key, lambda e: 1 / 0, self.ext.reconcile,
                      owner="node-a@epoch:0", connector=self.ext.identity)
        b = Node("node-b", self.auth, self.p.a, boundaries=fenced(self.ext))
        b.promote()
        with self.assertRaises(UnresolvedEffects):
            b.unpause(self.ext.reconcile)
        self.assertIn((eff.idempotency_key, "node-a@epoch:0", False), b.fence_log)
        self.assertEqual((b.state, j.get(eff.idempotency_key).status), (PAUSED, "unknown"))

    def test_fenced_dispatch_bound_to_its_connector_identity(self):
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())

        with failing(self.ext), self.assertRaises(EffectOutcomeUnknown):
            FencedEffects(a).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                     connector=self.ext.identity)
        with self.assertRaises(EffectConflict):   # C2: later dispatches must match
            FencedEffects(a).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                     connector="other.gateway")
        with self.assertRaises(ValueError):
            FencedEffects(a).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                     connector="")
        self.assertEqual(self.ext.count(), 0)

    def test_fence_refuses_unless_epoch_authority_rules_out_late_landing(self):
        b = self.active_node("node-b", self.p.b)      # epoch 1
        key = b.journal.record_intent(TASK, "send", action()).idempotency_key
        b.bind(key, self.ext.identity, self.ext.perform)   # proven, registered boundary

        def mk(owner, connector=self.ext.identity, k=key):
            return SimpleNamespace(idempotency_key=k, in_flight_owner=owner, connector=connector)
        self.assertTrue(b.c2_fence(mk("node-a@epoch:0")))
        self.assertTrue(b.c2_fence(mk("legacy-worker")))        # pre-C4 owner = epoch 0
        self.assertFalse(b.c2_fence(mk(None)))                  # unknown dispatcher
        self.assertFalse(b.c2_fence(mk("node-b@epoch:1")))      # our own epoch
        self.assertFalse(b.c2_fence(mk("node-c@epoch:5")))      # newer than us
        self.assertFalse(b.c2_fence(mk("node-a@epoch:0", connector=None)))        # no record
        self.assertFalse(b.c2_fence(mk("node-a@epoch:0", connector="naive.http")))  # mismatch
        self.assertFalse(b.c2_fence(mk("node-a@epoch:0", k="unbound-key")))       # unbound
        b.pause()
        self.assertFalse(b.c2_fence(mk("node-a@epoch:0")))      # paused, not reconciling
        b.unpause(self.ext.reconcile)
        Node("node-c", self.auth, os.path.join(self.tmp.name, "c.sqlite"),
             boundaries=fenced(self.ext)).promote()
        self.assertFalse(b.c2_fence(mk("node-a@epoch:0")))      # superseded
        self.assertEqual(b.state, FENCED)
        self.assertFalse(b.c2_fence(mk("node-a@epoch:0")))      # fenced

    def test_unpause_stays_paused_when_dispatcher_epoch_unknown(self):
        j = EffectJournal(self.p.a)
        eff = j.record_intent(TASK, "send", action())
        with j.db.tx() as c:   # an unknown effect with no dispatcher evidence
            c.execute("UPDATE effects SET status='unknown', dispatches=1")
        b = Node("node-b", self.auth, self.p.a, boundaries=fenced(self.ext))
        b.promote()
        with self.assertRaises(UnresolvedEffects):
            b.unpause(self.ext.reconcile)              # reconcile says "failed", fence says no
        self.assertEqual(b.state, PAUSED)
        self.assertEqual(j.get(eff.idempotency_key).status, "unknown")


# -- 1d. the fence vouches only for a proven, registered boundary (review fix) --
class NaiveAdapter:
    """Accepts a fencing token and IGNORES it: a plain write, no epoch check."""
    identity = "naive.http"

    def __init__(self):
        self.writes: list[tuple[str, int]] = []
        self.before_write = None

    def perform(self, effect, token):
        effect.action()
        if self.before_write is not None:
            self.before_write(effect, token)           # stalled after require_current
        self.writes.append((effect.idempotency_key, token.epoch))
        return {"applied": effect.idempotency_key}

    def reconcile(self, effect):
        return "succeeded" if any(k == effect.idempotency_key for k, _ in self.writes) \
            else "failed"


class UnprovenBoundary(_Tmp):
    def test_token_ignoring_adapter_late_write_is_never_redispatched(self):
        """Reviewer's counterexample, deterministic: epoch-1 perform stalls after
        require_current and before its write; epoch 2 promotes and reconciles
        "not applied"; the fence must say False, so no re-dispatch; then the
        stalled write lands. Exactly one application."""
        naive = NaiveAdapter()
        a = self.active_node()                     # registry knows only the stub
        eff = a.journal.record_intent(TASK, "send", action())
        key, seen = eff.idempotency_key, {}

        def takeover(effect, token):              # runs inside A's stalled perform
            b = Node("node-b", self.auth, self.p.a, boundaries=fenced(self.ext))
            self.assertEqual(b.promote(), 2)
            self.assertEqual(naive.reconcile(b.journal.get(key)), "failed")  # no write yet
            with self.assertRaises(UnresolvedEffects):
                b.unpause(naive.reconcile)        # A's window is live and is NOT ended
            row = b.journal.get(key)
            self.assertEqual((row.status, row.in_flight_owner), ("unknown", "node-a@epoch:1"))
            self.assertFalse(b._boundary_proven(row))
            self.assertEqual(b.fence_log, [])      # live window: fence not even asked
            # past A's window, every epoch condition holds; the unproven boundary says no
            with self.assertRaises(UnresolvedEffects):
                b.unpause(naive.reconcile, now=row.in_flight_until + 1)
            self.assertEqual(b.fence_log, [(key, "node-a@epoch:1", False)])
            # even once A's window has passed, nothing re-dispatches it
            with self.assertRaises(EffectOutcomeUnknown):
                b.journal.execute(key, lambda e: naive.perform(e, b.token()), naive.reconcile,
                                  owner="probe", connector=naive.identity,
                                  now=row.in_flight_until + 1)
            self.assertEqual(naive.writes, [])
            seen["b"] = b
        naive.before_write = takeover
        done = FencedEffects(a).execute(key, naive.perform, naive.reconcile,
                                        connector=naive.identity)
        b = seen["b"]
        self.assertEqual(b.state, PAUSED)
        self.assertEqual(naive.writes, [(key, 1)])   # the late epoch-1 write landed
        self.assertEqual(done.status, "succeeded")   # A settled it; B never re-dispatched
        self.assertEqual(b.binding(key), (naive.identity, None))   # unproven boundary
        # B can now unpause: reconcile says succeeded (always accepted), still one write
        b.unpause(naive.reconcile)
        self.assertEqual(b.state, ACTIVE)
        self.assertEqual(FencedEffects(b).execute(key, naive.perform, naive.reconcile,
                                                  connector=naive.identity).status,
                         "succeeded")
        self.assertEqual(naive.writes, [(key, 1)])

    def test_rotated_boundary_contract_does_not_vouch_for_old_effects(self):
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())

        with failing(self.ext), self.assertRaises(EffectOutcomeUnknown):
            FencedEffects(a).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                     connector=self.ext.identity)
        rotated = FencedBoundaryRegistry([FencedBoundaryContract(
            self.ext.identity, self.ext.perform, "a different review", STUB_ARTIFACT,
            self.ext.fencing_scope())])
        self.assertNotEqual(rotated[self.ext.identity].contract_digest,
                            self.ext.contract(STUB_ARTIFACT).contract_digest)
        b = Node("node-b", self.auth, self.p.a, boundaries=rotated)
        b.promote()
        with self.assertRaises(UnresolvedEffects):
            b.unpause(self.ext.reconcile)
        self.assertIn((eff.idempotency_key, "node-a@epoch:1", False), b.fence_log)
        self.assertEqual(self.ext.count(), 0)

    def test_empty_registry_by_default_vouches_for_nothing(self):
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())

        with failing(self.ext), self.assertRaises(EffectOutcomeUnknown):
            FencedEffects(a).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                     connector=self.ext.identity)
        b = Node("node-b", self.auth, self.p.a)    # no boundaries configured
        self.assertEqual(len(b.boundaries), 0)
        b.promote()
        with self.assertRaises(UnresolvedEffects):
            b.unpause(self.ext.reconcile)
        self.assertEqual(b.state, PAUSED)

    def test_registered_identity_requires_its_reviewed_adapter(self):
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())
        wrapper = lambda e, t: self.ext.perform(e, t)   # not the reviewed adapter
        with self.assertRaises(EffectConflict):
            FencedEffects(a).execute(eff.idempotency_key, wrapper, self.ext.reconcile,
                                     connector=self.ext.identity)
        after = a.journal.get(eff.idempotency_key)
        self.assertEqual((after.status, after.dispatches), ("pending", 0))
        self.assertIsNone(a.binding(eff.idempotency_key))
        self.assertEqual(self.ext.count(), 0)

    def test_registry_is_frozen_and_validated(self):
        reg = fenced(self.ext)
        with self.assertRaises(AttributeError):
            reg._entries = {}
        with self.assertRaises(TypeError):
            FencedBoundaryRegistry([object()])
        with self.assertRaises(ValueError):
            FencedBoundaryRegistry([self.ext.contract(STUB_ARTIFACT),
                                    self.ext.contract(STUB_ARTIFACT)])
        with self.assertRaises(TypeError):
            Node("node-x", self.auth, self.p.b, boundaries={"x": 1})


# -- 1e. contract binds the verified implementation and its scope (review fix) --
def _token_ignoring_perform(self, effect, token):
    """Synthetic replacement body: same signature, writes WITHOUT any check."""
    if self.gate_before_apply is not None:
        _pass_gate(self.gate_before_apply)
    with self.authority.boundary() as (c, _):
        c.execute("INSERT INTO ext_applied (key, node_id, epoch) VALUES (?,?,?)",
                  (effect.idempotency_key, token.node_id, token.epoch))
    return {"applied": effect.idempotency_key, "epoch": token.epoch}


def _token_ignoring_apply(self, key, token):
    """Synthetic replacement for apply: records the write WITHOUT the check."""
    with self.authority.boundary() as (c, _):
        c.execute("INSERT INTO ext_applied (key, node_id, epoch) VALUES (?,?,?)",
                  (key, token.node_id, token.epoch))
    return {"applied": key, "epoch": token.epoch}


@contextlib.contextmanager
def mutated_code(func, replacement):
    """Swap ``func.__code__`` for ``replacement``'s body, keeping the name,
    qualified name, file and first line (Dolores's in-memory probes)."""
    original = func.__code__
    func.__code__ = replacement.__code__.replace(
        co_name=original.co_name, co_qualname=original.co_qualname,
        co_filename=original.co_filename, co_firstlineno=original.co_firstlineno)
    try:
        yield
    finally:
        func.__code__ = original


@contextlib.contextmanager
def mutated_stub_perform():
    """Dolores's probe: keep StubConnector.perform's module/qualified identity,
    swap its code for a token-ignoring body, AFTER the registry was built."""
    func = StubConnector.perform
    original = func.__code__
    func.__code__ = _token_ignoring_perform.__code__.replace(
        co_name=original.co_name, co_qualname=original.co_qualname,
        co_filename=original.co_filename, co_firstlineno=original.co_firstlineno)
    try:
        yield
    finally:
        func.__code__ = original


IMPOSTOR_SRC = '''
from runtime.fencing import StubConnector as _Real


class StubConnector(_Real):
    def perform(self, effect, token):          # same name, token-ignoring body
        with self.authority.boundary() as (c, _):
            c.execute("INSERT INTO ext_applied (key, node_id, epoch) VALUES (?,?,?)",
                      (effect.idempotency_key, token.node_id, token.epoch))
        return {"applied": effect.idempotency_key}


StubConnector.__module__ = "runtime.fencing"
StubConnector.__qualname__ = "StubConnector"
StubConnector.perform.__module__ = "runtime.fencing"
StubConnector.perform.__qualname__ = "StubConnector.perform"
'''


class ContractBindsImplementationAndScope(_Tmp):
    def _dispatch_ambiguously(self, a):
        eff = a.journal.record_intent(TASK, "send", action())

        with failing(self.ext), self.assertRaises(EffectOutcomeUnknown):
            FencedEffects(a).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                     connector=self.ext.identity)
        return eff.idempotency_key

    def test_positive_control_verified_stub_still_vouches(self):
        a = self.active_node()
        key = self._dispatch_ambiguously(a)
        b = self.active_node("node-b", self.p.a)
        self.assertIn((key, "node-a@epoch:1", True), b.fence_log)
        self.assertEqual(FencedEffects(b).execute(key, self.ext.perform, self.ext.reconcile,
                                                  connector=self.ext.identity).status,
                         "succeeded")
        self.assertEqual(self.ext.applied(), [(key, "node-b", 2)])

    def _late_stale_write_scenario(self, mutation, stale_write_lands=True):
        """Registry built and the effect bound under the reviewed code; then
        ``mutation`` replaces boundary code in memory under the same names.
        Epoch-1's stalled call runs the mutated code; epoch 2 must NOT be
        vouched for; the stale write lands; exactly one application."""
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())
        key, token = eff.idempotency_key, a.token()
        a.bind(key, self.ext.identity, self.ext.perform)          # bound while genuine
        b = Node("node-b", self.auth, self.p.a, boundaries=fenced(self.ext))  # built pre-mutation
        with mutation():
            with self.assertRaises(BoundaryVerificationError):
                fenced(self.ext)                                  # registry build refuses
            other = a.journal.record_intent(TASK, "other", action(n=2))
            with self.assertRaises(EffectConflict):               # live verification at bind
                FencedEffects(a).execute(other.idempotency_key, self.ext.perform,
                                         self.ext.reconcile, connector=self.ext.identity)
            self.assertIsNone(a.binding(other.idempotency_key))
            with Parked(self.ext, lambda: a.journal.execute(
                    key, lambda e: self.ext.perform(e, token), self.ext.reconcile,
                    owner=token.owner, connector=self.ext.identity)) as job:
                self.assertEqual(b.promote(), 2)
                row = b.journal.get(key)
                with self.assertRaises(UnresolvedEffects):        # past A's window
                    b.unpause(self.ext.reconcile, now=row.in_flight_until + 1)
                self.assertEqual(b.fence_log, [(key, "node-a@epoch:1", False)])
                self.assertEqual(b.state, PAUSED)
        self.assertIsNone(job.error)
        if stale_write_lands:     # the mutated code ignores the token: the stale write lands
            self.assertEqual(job.result.status, "succeeded")
            expected = [(key, "node-a", 1)]
        else:                     # apply still checks: the stale call is refused, not landed
            self.assertEqual(job.result.status, "failed")
            expected = [(key, "node-b", 2)]
        if stale_write_lands:
            self.assertEqual(self.ext.applied(), expected)          # no (b,2) re-dispatch
        b.unpause(self.ext.reconcile)
        self.assertEqual(FencedEffects(b).execute(key, self.ext.perform, self.ext.reconcile,
                                                  connector=self.ext.identity).status,
                         "succeeded")
        self.assertEqual(self.ext.applied(), expected)              # exactly one application

    def test_same_name_memory_mutation_after_registry_build_loses_trust(self):
        from runtime.effects import adapter_name
        self.assertEqual(adapter_name(self.ext.perform),
                         "runtime.fencing.StubConnector#runtime.fencing.StubConnector.perform")
        self._late_stale_write_scenario(mutated_stub_perform)

    def test_apply_helper_mutation_loses_trust(self):
        """Dolores's probe at 7c0a944: perform and the file unchanged, only
        StubConnector.apply's code replaced by a token-ignoring version."""
        self._late_stale_write_scenario(
            lambda: mutated_code(StubConnector.apply, _token_ignoring_apply))

    def test_module_helper_mutation_loses_trust(self):
        """A module-level helper on the boundary path (the authority's scope id)."""
        import runtime.fencing as fencing_mod

        def evil(authority):
            return "sqlite:" + os.path.realpath(authority.path)
        self._late_stale_write_scenario(
            lambda: mutated_code(fencing_mod.authority_scope_id, evil), stale_write_lands=False)

    def test_instance_attribute_shadowing_a_method_loses_trust(self):
        import types as _t
        self.ext.apply = _t.MethodType(_token_ignoring_apply, self.ext)   # shadows apply
        try:
            with self.assertRaises(BoundaryVerificationError):
                fenced(self.ext)
        finally:
            del self.ext.apply
        reg = fenced(self.ext)                                    # ...built while genuine
        a = Node("node-a", self.auth, self.p.a, boundaries=reg)
        a.promote()
        a.unpause(self.ext.reconcile)
        eff = a.journal.record_intent(TASK, "send", action())
        self.ext.apply = _t.MethodType(_token_ignoring_apply, self.ext)
        try:
            with self.assertRaises(EffectConflict):              # refused at bind, live
                FencedEffects(a).execute(eff.idempotency_key, self.ext.perform,
                                         self.ext.reconcile, connector=self.ext.identity)
            self.assertFalse(a._boundary_proven(SimpleNamespace(
                idempotency_key=eff.idempotency_key, connector=self.ext.identity)))
        finally:
            del self.ext.apply
        self.assertEqual(self.ext.count(), 0)

    def test_getattribute_descriptor_takeover_loses_trust(self):
        """Dolores's bypass at 344ca26: a NON-callable descriptor installed as
        StubConnector.__getattribute__ resolves self.apply to a token-ignoring
        callable while perform, apply and the file are untouched."""
        class D:
            def __get__(self, obj, owner=None):
                def lookup(name):
                    if name == "apply":
                        return lambda key, token: _token_ignoring_apply(obj, key, token)
                    return object.__getattribute__(obj, name)
                return lookup

        @contextlib.contextmanager
        def descriptor_installed():
            self.assertFalse(callable(D()))
            StubConnector.__getattribute__ = D()
            try:
                yield
            finally:
                del StubConnector.__getattribute__
        with descriptor_installed():                    # the bypass is real
            probe = StubConnector(self.auth)
            self.assertIsNot(type(probe).__dict__.get("apply"), None)
            self.assertNotEqual(getattr(probe.apply, "__name__", ""), "apply")
        self._late_stale_write_scenario(descriptor_installed)

    def test_late_writes_after_verification_lose_trust(self):
        import runtime.fencing as fencing_mod
        reg = fenced(self.ext)
        a = Node("node-a", self.auth, self.p.a, boundaries=reg)
        a.promote()
        a.unpause(self.ext.reconcile)
        contract = reg[self.ext.identity]
        self.assertIsNone(a._contract_problem(contract))

        class FakeEvent:
            def wait(self, timeout=None):
                return True
        writes = [
            ("class attribute", lambda: setattr(StubConnector, "extra", 1),
             lambda: delattr(StubConnector, "extra")),
            ("module value", lambda: setattr(fencing_mod, "_GATE_TYPES",
                                             fencing_mod._GATE_TYPES + (FakeEvent,)),
             None),
            ("snapshotted value", lambda: setattr(fencing_mod, "_CONTEXTMANAGER_HELPER",
                                                  (lambda: 0).__code__), None),
            ("module attribute", lambda: setattr(fencing_mod, "_helper", 1),
             lambda: delattr(fencing_mod, "_helper")),
        ]
        for label, do, undo in writes:
            name = {"module value": "_GATE_TYPES",
                    "snapshotted value": "_CONTEXTMANAGER_HELPER"}.get(label)
            saved = getattr(fencing_mod, name) if name else None
            do()
            try:
                self.assertIsNotNone(a._contract_problem(contract), label)
                eff = a.journal.record_intent(TASK, f"late-{label}", action())
                with self.assertRaises(EffectConflict, msg=label):
                    FencedEffects(a).execute(eff.idempotency_key, self.ext.perform,
                                             self.ext.reconcile, connector=self.ext.identity)
            finally:
                if undo:
                    undo()
                else:
                    setattr(fencing_mod, name, saved)
        self.assertIsNone(a._contract_problem(contract))       # genuine again
        self.assertEqual(self.ext.count(), 0)

    def test_base_swap_loses_trust(self):
        from runtime.fencing import StaleEpoch, StaleEpochRefused

        class EvilStale(StaleEpoch):
            pass
        original = StaleEpochRefused.__bases__
        StaleEpochRefused.__bases__ = (EvilStale,)
        try:
            with self.assertRaisesRegex(BoundaryVerificationError, "bases were swapped"):
                fenced(self.ext)
        finally:
            StaleEpochRefused.__bases__ = original
        fenced(self.ext)

    def test_metaclass_swap_loses_trust(self):
        import runtime.fencing as fencing_mod

        class Meta(type):
            pass
        real = fencing_mod.StubConnector
        ns = {k: v for k, v in vars(real).items() if k not in ("__dict__", "__weakref__")}
        swapped = Meta("StubConnector", real.__bases__, ns)
        swapped.__module__, swapped.__qualname__ = real.__module__, real.__qualname__
        fencing_mod.StubConnector = swapped
        try:
            ext2 = swapped(self.auth)
            with self.assertRaisesRegex(BoundaryVerificationError, "metaclass was swapped"):
                FencedBoundaryRegistry([ext2.contract(STUB_ARTIFACT)])
        finally:
            fencing_mod.StubConnector = real
        fenced(self.ext)

    def test_lookup_affecting_class_attributes_and_class_reassignment_lose_trust(self):
        def getattr_hook(self, name):
            return None
        hooks = {"__getattr__": getattr_hook, "__slots__": ("x",),
                 "__init_subclass__": classmethod(lambda cls: None),
                 "__set_name__": lambda self, owner, name: None}
        # (__new__ is rejected by name too, but setting then deleting it on a live
        # class leaves CPython's tp_new slot changed, so it is not probed in place.)
        for name, value in hooks.items():
            setattr(StubConnector, name, value)
            try:
                with self.assertRaises(BoundaryVerificationError, msg=name):
                    fenced(self.ext)
            finally:
                delattr(StubConnector, name)

        class Sub(StubConnector):
            pass
        self.ext.__class__ = Sub                           # instance __class__ reassignment
        try:
            with self.assertRaises(BoundaryVerificationError):
                fenced(self.ext)
        finally:
            self.ext.__class__ = StubConnector
        fenced(self.ext)

    def test_instance_callables_and_collaborator_tampering_lose_trust(self):
        cases = []

        def extra_callable():
            self.ext.helper = lambda: None                      # a callable, no shadowing
            return lambda: delattr(self.ext, "helper")

        def authority_method_shadow():
            self.ext.authority.boundary = lambda: None          # collaborator shadowed
            return lambda: delattr(self.ext.authority, "boundary")

        def authority_subclass():
            real = self.ext.authority

            class Lenient(EpochAuthority):
                pass
            fake = Lenient.__new__(Lenient)
            fake.__dict__.update(real.__dict__)
            self.ext.authority = fake                           # same path, other class
            return lambda: setattr(self.ext, "authority", real)
        for tamper in (extra_callable, authority_method_shadow, authority_subclass):
            undo = tamper()
            try:
                with self.assertRaises(BoundaryVerificationError, msg=tamper.__name__):
                    fenced(self.ext)
            finally:
                undo()
            cases.append(tamper.__name__)
        fenced(self.ext)                                         # genuine again
        self.assertEqual(len(cases), 3)

    def test_same_name_implementation_from_another_file_loses_trust(self):
        from runtime.effects import adapter_name
        a = self.active_node()
        key = self._dispatch_ambiguously(a)                      # bound under reviewed code
        path = os.path.join(self.tmp.name, "impostor_mod.py")
        with open(path, "w") as f:
            f.write(IMPOSTOR_SRC)
        spec = importlib.util.spec_from_file_location("impostor_mod", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fake = mod.StubConnector(self.auth)
        self.assertEqual(adapter_name(fake.perform), adapter_name(self.ext.perform))
        self.assertEqual(fake.fencing_scope(), self.ext.fencing_scope())
        self.assertNotEqual(boundary_artifact_digest(fake.perform), STUB_ARTIFACT)
        with self.assertRaises(BoundaryVerificationError):      # pinned digest: refused
            FencedBoundaryRegistry([fake.contract(STUB_ARTIFACT)])
        # Even with the impostor's own (honest) digest, the impostor class is not
        # the reviewed module's class at that name: refused.
        with self.assertRaises(BoundaryVerificationError):
            FencedBoundaryRegistry([fake.contract(boundary_artifact_digest(fake.perform))])
        # and the effect bound under the reviewed contract is never vouched by it
        b = Node("node-b", self.auth, self.p.a)
        b.promote()
        with self.assertRaises(UnresolvedEffects):
            b.unpause(fake.reconcile)
        self.assertIn((key, "node-a@epoch:1", False), b.fence_log)
        self.assertEqual(self.ext.count(), 0)

    def test_bound_scope_rotation_loses_trust(self):
        a = self.active_node()
        key = self._dispatch_ambiguously(a)
        b = Node("node-b", self.auth, self.p.a, boundaries=fenced(self.ext))
        other = EpochAuthority(os.path.join(self.tmp.name, "other-authority.sqlite"))
        real = self.ext.authority
        self.ext.authority = other                     # boundary re-pointed after build
        try:
            self.assertNotEqual(self.ext.fencing_scope(), b.boundaries[self.ext.identity].fencing_scope)
            b.promote()
            with self.assertRaises(UnresolvedEffects):
                b.unpause(self.ext.reconcile)
            self.assertIn((key, "node-a@epoch:1", False), b.fence_log)
        finally:
            self.ext.authority = real
        self.assertEqual(self.ext.count(), 0)

    def test_contract_scoped_to_another_authority_never_vouches_or_dispatches(self):
        other = EpochAuthority(os.path.join(self.tmp.name, "other-authority.sqlite"))
        foreign = StubConnector(other)                 # enforces tokens of ANOTHER authority
        a = Node("node-a", self.auth, self.p.a, boundaries=fenced(foreign))
        a.promote()
        a.unpause(self.ext.reconcile)
        eff = a.journal.record_intent(TASK, "send", action())
        with self.assertRaises(EffectConflict):
            FencedEffects(a).execute(eff.idempotency_key, foreign.perform, foreign.reconcile,
                                     connector=foreign.identity)
        self.assertIsNone(a.binding(eff.idempotency_key))

    def test_registry_build_refuses_unverifiable_or_mismatched_contracts(self):
        scope = self.ext.fencing_scope()
        with self.assertRaises(BoundaryVerificationError):     # wrong declared digest
            FencedBoundaryRegistry([self.ext.contract("0" * 64)])
        drifted = FencingScope(scope.boundary, scope.authority, scope.store, "c4.boundary.v0",
                               scope.config_hash)
        with self.assertRaises(BoundaryVerificationError):     # declared scope != live
            FencedBoundaryRegistry([FencedBoundaryContract(
                self.ext.identity, self.ext.perform, "ref", STUB_ARTIFACT, drifted)])
        ns = {}
        exec("def perform(effect, token):\n    return None\n", ns)
        with self.assertRaises(BoundaryVerificationError):     # no source at all
            FencedBoundaryRegistry([FencedBoundaryContract(
                "exec.adapter", ns["perform"], "ref", STUB_ARTIFACT, scope)])
        with self.assertRaises(ValueError):
            FencedBoundaryContract(self.ext.identity, self.ext.perform, "ref", "abc", scope)
        with self.assertRaises(ValueError):
            FencingScope("b", "a", "s", "v", "not-a-hash")


# -- 2. node lifecycle ---------------------------------------------------------
class NodeLifecycle(_Tmp):
    def test_new_node_starts_fenced_and_cannot_dispatch(self):
        n = Node("node-a", self.auth, self.p.a, boundaries=fenced(self.ext))
        self.assertEqual((n.state, n.fence_reason, n.epoch), (FENCED, "new node", None))
        eff = n.journal.record_intent(TASK, "send", action())
        with self.assertRaises(NodeNotActive):
            FencedEffects(n).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile, connector=self.ext.identity)
        self.assertEqual(self.ext.count(), 0)

    def test_returning_node_starts_fenced_then_rejoins_with_fresh_epoch(self):
        old = self.active_node()
        self.assertEqual((old.state, old.epoch), (ACTIVE, 1))
        back = Node("node-a", self.auth, self.p.a, boundaries=fenced(self.ext))          # restart, same journal
        self.assertEqual((back.state, back.fence_reason, back.epoch),
                         (FENCED, "returning node", 1))
        self.assertTrue(back.observe().current)            # observing is not unfencing
        self.assertEqual(back.state, FENCED)
        self.assertTrue(back.rejoin())
        self.assertEqual((back.state, back.epoch), (PAUSED, 2))
        back.unpause(self.ext.reconcile)
        self.assertEqual(back.state, ACTIVE)
        # the stalled earlier incarnation (epoch 1) is now fenced too
        eff = old.journal.record_intent(TASK, "send", action())
        with self.assertRaises(StaleEpoch):
            FencedEffects(old).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile, connector=self.ext.identity)
        self.assertEqual(old.state, FENCED)
        self.assertEqual(self.ext.count(), 0)

    def test_superseded_returning_node_stays_fenced(self):
        self.active_node("node-a", self.p.a)
        self.active_node("node-b", self.p.b)                # failover to epoch 2
        back = Node("node-a", self.auth, self.p.a, boundaries=fenced(self.ext))
        self.assertEqual(back.state, FENCED)
        self.assertFalse(back.rejoin())
        self.assertEqual((back.state, back.fence_reason), (FENCED, "superseded"))
        self.assertEqual(self.auth.current().holder, "node-b")

    def test_promoted_node_paused_until_unpause_after_reconciliation(self):
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())

        with failing(self.ext), self.assertRaises(Exception):
            FencedEffects(a).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile,
                                     connector=self.ext.identity)
        self.assertEqual(a.journal.get(eff.idempotency_key).status, "unknown")

        rep = FileCopyReplicator(self.p.replica)
        rep.replicate(self.p.a)
        rep.restore(self.p.b)
        b = Node("node-b", self.auth, self.p.b, boundaries=fenced(self.ext))
        self.assertEqual(b.promote(), 2)
        self.assertEqual(b.state, PAUSED)
        fx = FencedEffects(b)
        with self.assertRaises(NodeNotActive) as cm:
            fx.execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile, connector=self.ext.identity)
        self.assertEqual(cm.exception.state, PAUSED)

        # reconciliation that cannot decide keeps it paused
        with self.assertRaises(UnresolvedEffects) as cm:
            b.unpause(lambda e: "still_unknown")
        self.assertEqual(cm.exception.keys, [eff.idempotency_key])
        self.assertEqual(b.state, PAUSED)
        with self.assertRaises(NodeNotActive):
            fx.execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile, connector=self.ext.identity)

        # real reconcile: the external system never applied it -> failed -> unpause
        report = b.unpause(self.ext.reconcile)
        self.assertEqual(report["resolved"], [eff.idempotency_key])
        self.assertEqual(b.state, ACTIVE)
        self.assertEqual(b.journal.unresolved(), [])
        self.assertEqual(fx.execute(eff.idempotency_key, self.ext.perform,
                                    self.ext.reconcile, connector=self.ext.identity).status, "succeeded")
        self.assertEqual(self.ext.applied(), [(eff.idempotency_key, "node-b", 2)])

    def test_unpause_refused_when_superseded_while_paused(self):
        b = Node("node-b", self.auth, self.p.b, boundaries=fenced(self.ext))
        b.promote()
        Node("node-c", self.auth, os.path.join(self.tmp.name, "c.sqlite"), boundaries=fenced(self.ext)).promote()
        with self.assertRaises(NodeNotActive):
            b.unpause(self.ext.reconcile)
        self.assertEqual(b.state, FENCED)

    def test_active_node_demoted_on_observing_newer_epoch(self):
        a = self.active_node()
        self.active_node("node-b", self.p.b)
        self.assertFalse(a.observe().current)
        self.assertEqual(a.state, FENCED)


# -- 3. concurrent promotions --------------------------------------------------
class ConcurrentPromotion(_Tmp):
    def test_exactly_one_of_eight_processes_wins(self):
        n = 8
        barrier, q = SPAWN.Barrier(n), SPAWN.Queue()
        procs = [SPAWN.Process(target=_promote_race, args=(self.p.auth, barrier, q, i))
                 for i in range(n)]
        for proc in procs:
            proc.start()
        results = [q.get(timeout=WAIT) for _ in procs]
        for proc in procs:
            proc.join(WAIT)
        winners = [(i, e) for i, e in results if e is not None]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(winners[0][1], 1)
        cur = self.auth.current()
        self.assertEqual((cur.epoch, cur.holder), (1, f"node-{winners[0][0]}"))
        self.assertEqual([h.epoch for h in self.auth.history()], [1])

    def test_cas_is_monotonic_and_refuses_stale_expectation(self):
        self.assertEqual(self.auth.promote("a", 0), 1)
        self.assertEqual(self.auth.promote("b", 1), 2)
        for stale in (0, 1, 3):
            with self.assertRaises(PromotionConflict):
                self.auth.promote("c", stale)
        with self.assertRaises(ValueError):
            self.auth.promote("c", -1)
        self.assertEqual([(h.epoch, h.holder) for h in self.auth.history()],
                         [(1, "a"), (2, "b")])

    def test_losing_node_is_fenced(self):
        a = Node("node-a", self.auth, self.p.a, boundaries=fenced(self.ext))
        b = Node("node-b", self.auth, self.p.b, boundaries=fenced(self.ext))
        a.promote()
        with self.assertRaises(PromotionConflict):
            b._take_epoch(0)                               # B acts on a stale observation
        self.assertEqual((b.state, b.fence_reason), (FENCED, "promotion lost"))


# -- 4. replicator seam --------------------------------------------------------
class ReplicatorSeam(_Tmp):
    def _populate(self):
        a = self.active_node()
        fx = FencedEffects(a)
        ok = a.journal.record_intent(TASK, "s1", action(n=1))
        fx.execute(ok.idempotency_key, self.ext.perform, self.ext.reconcile, connector=self.ext.identity)
        a.journal.record_intent(TASK, "s2", action(n=2))   # stays pending
        unk = a.journal.record_intent(TASK, "s3", action(n=3))

        with failing(self.ext), self.assertRaises(Exception):
            fx.execute(unk.idempotency_key, self.ext.perform, self.ext.reconcile,
                       connector=self.ext.identity)
        return a

    def test_restored_replica_reproduces_journal_and_starts_fenced(self):
        a = self._populate()
        rep = FileCopyReplicator(self.p.replica)
        self.assertEqual(rep.latest(), None)
        gen = rep.replicate(self.p.a)
        self.assertEqual(gen, "000001")
        restored = Path(rep.restore(self.p.b))
        self.assertEqual(effect_rows(restored), effect_rows(self.p.a))
        self.assertEqual(sorted(e.status for e in EffectJournal(restored).unresolved()),
                         ["pending", "unknown"])
        with self.assertRaises(FileExistsError):
            rep.restore(self.p.b)

        # restored under the SAME node id while the authority still names it:
        # still fenced, and it cannot rejoin; it must promote.
        self.assertTrue(a.is_current())
        n = Node("node-a", self.auth, restored, boundaries=fenced(self.ext))
        self.assertEqual((n.state, n.fence_reason), (FENCED, "restored from replica"))
        self.assertFalse(n.rejoin())
        self.assertEqual(n.state, FENCED)
        self.assertEqual(n.promote(), 2)
        self.assertEqual(n.state, PAUSED)
        n.unpause(self.ext.reconcile)
        self.assertEqual(n.state, ACTIVE)
        self.assertFalse(a.is_current())
        # after promotion the restore marker is cleared: a later restart can rejoin
        again = Node("node-a", self.auth, restored, boundaries=fenced(self.ext))
        self.assertEqual(again.fence_reason, "returning node")
        self.assertTrue(again.rejoin())

    def test_generations_are_numbered_and_restore_picks_one(self):
        self.active_node()
        rep = FileCopyReplicator(self.p.replica)
        g1 = rep.replicate(self.p.a)
        EffectJournal(self.p.a).record_intent(TASK, "later", action(n=9))
        g2 = rep.replicate(self.p.a)
        self.assertEqual((g1, g2, rep.latest()), ("000001", "000002", "000002"))
        old = rep.restore(os.path.join(self.tmp.name, "old.sqlite"), g1)
        new = rep.restore(os.path.join(self.tmp.name, "new.sqlite"))
        self.assertEqual(len(effect_rows(old)), 0)
        self.assertEqual(len(effect_rows(new)), 1)
        with self.assertRaises(FileNotFoundError):
            FileCopyReplicator(os.path.join(self.tmp.name, "empty")).restore(
                os.path.join(self.tmp.name, "x.sqlite"))


# -- 5. C1 PolicyEngine integration --------------------------------------------
class PolicyNodeGate(_Tmp):
    def engine(self, node_state, ledger=None):
        self.ledger = ledger or tpe.c0.MemoryApprovalLedger()
        return tpe.engine(self.ledger, node_state=node_state)

    def test_consequential_denied_from_fenced_and_paused_node_without_consuming(self):
        n = Node("node-a", self.auth, self.p.a, boundaries=fenced(self.ext))
        eng = self.engine(n.policy_state)
        approved = tpe.req(approval=tpe.record())
        d = eng.decide(approved)
        self.assertEqual((d.outcome, d.reason_code), ("deny", "node_fenced"))
        self.assertIsNone(self.ledger.pending("a" * 32))   # nonce not burned
        n.promote()
        d = eng.decide(approved)
        self.assertEqual((d.outcome, d.reason_code), ("deny", "node_paused"))
        self.assertIsNone(self.ledger.pending("a" * 32))
        n.unpause(self.ext.reconcile)
        d = eng.decide(approved)
        self.assertEqual((d.outcome, d.reason_code), ("allow", "ok_approved"))
        # another node takes over: the same engine now denies, live
        Node("node-b", self.auth, self.p.b, boundaries=fenced(self.ext)).promote()
        d = eng.decide(tpe.req(approval=tpe.record(nonce="b" * 32)))
        self.assertEqual((d.outcome, d.reason_code), ("deny", "node_fenced"))
        self.assertEqual(n.state, FENCED)

    def test_writes_gated_reads_not(self):
        eng = self.engine(lambda: FENCED)
        self.assertEqual(eng.decide(tpe.req(tpe.action_for("calendar.read"))).outcome, "allow")
        d = eng.decide(tpe.req(tpe.action_for("notes.write")))
        self.assertEqual((d.outcome, d.reason_code), ("deny", "node_fenced"))

    def test_seam_failure_or_unknown_state_fails_closed(self):
        def boom():
            raise RuntimeError("authority down")
        for seam in (boom, lambda: "starting", lambda: None):
            d = self.engine(seam).decide(tpe.req(approval=tpe.record()))
            self.assertEqual((d.outcome, d.reason_code), ("deny", "node_not_active"))

    def test_authority_unreachable_reports_fenced(self):
        n = self.active_node()
        os.remove(self.p.auth)
        os.makedirs(self.p.auth)                          # path is now a directory: unreadable
        self.assertEqual(n.policy_state(), FENCED)
        self.assertEqual((n.state, n.fence_reason), (FENCED, "epoch authority unreadable"))

    def test_without_seam_c1_is_unchanged(self):
        d = tpe.engine().decide(tpe.req(approval=tpe.record()))
        self.assertEqual((d.outcome, d.reason_code), ("allow", "ok_approved"))


if __name__ == "__main__":
    unittest.main()
