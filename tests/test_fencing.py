"""C4 merge evidence (decision T35 item 10): a stale-epoch consequential call is
refused; a returning node starts fenced; a promoted node starts with effects
paused.

Offline only. Two "containers" are two spawned processes, each with its own
journal SQLite file in a temp dir. Shared files stand in for the epoch
authority and for the external system (StubConnector). The replica is a
directory of snapshot files written by FileCopyReplicator (no Litestream).
"""
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_policy_engine as tpe  # noqa: E402  (C1 fixtures: registry, approvals)
from runtime import (ACTIVE, FENCED, PAUSED, Database, EffectFailed, EffectJournal,  # noqa: E402
                     EpochAuthority, FencedEffects, FileCopyReplicator, FencingToken, Node,
                     NodeNotActive, PromotionConflict, StaleEpoch, StaleEpochRefused,
                     StubConnector, UnresolvedEffects, owner_epoch)
from runtime._contract import BoundAction  # noqa: E402

SPAWN = multiprocessing.get_context("spawn")
TASK = "task-c4"
WAIT = 60


def action(task_id=TASK, n=1):
    return BoundAction(task_id, "gmail.send", "send", "fake://external", {"n": n})


def paths(tmp):
    return SimpleNamespace(auth=os.path.join(tmp, "authority.sqlite"),
                           ext=os.path.join(tmp, "external.sqlite"),
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
        ext = StubConnector(p.ext, auth)
        node = Node("node-a", auth, p.a)
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
                fx.execute(eff.idempotency_key, ext.perform, ext.reconcile)
                out["first"] = "dispatched"
            except StaleEpoch as e:
                out["first"] = type(e).__name__
        else:
            def perform(effect, token):
                rep.replicate(p.a)                     # ships the "unknown" claim
                stalled.set()
                resume.wait(WAIT)                      # GC pause / VM freeze
                return ext.perform(effect, token)
            try:
                fx.execute(eff.idempotency_key, perform, ext.reconcile)
                out["first"] = "dispatched"
            except StaleEpoch as e:
                out["first"] = type(e).__name__

        out["status_after"] = node.journal.get(eff.idempotency_key).status
        out["state_after"] = node.state
        try:                                           # it tries again: journal side
            fx.execute(eff.idempotency_key, ext.perform, ext.reconcile)
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
        ext = StubConnector(p.ext, auth)
        FileCopyReplicator(p.replica).restore(p.b)
        node = Node("node-b", auth, p.b)
        out = {"start": node.state, "rejoin": node.rejoin()}
        out["epoch"] = node.promote()
        out["after_promote"] = node.state
        fx = FencedEffects(node)
        key = node.journal.for_step(TASK, "send").idempotency_key
        out["restored_status"] = node.journal.get(key).status
        try:
            fx.execute(key, ext.perform, ext.reconcile)
            out["paused_dispatch"] = "dispatched"
        except NodeNotActive as e:
            out["paused_dispatch"] = e.state
        out["unpause"] = node.unpause(ext.reconcile)
        out["after_unpause"] = node.state
        out["final"] = fx.execute(key, ext.perform, ext.reconcile).status
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
        self.ext = StubConnector(self.p.ext, self.auth)

    def active_node(self, node_id="node-a", db=None):
        node = Node(node_id, self.auth, db or self.p.a)
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
                Node("node-b", self.auth, self.p.b).promote()   # takeover after the pre-check
                return
            a.require_active(token)

        with self.assertRaises(StaleEpoch):
            a.journal.execute(eff.idempotency_key, lambda e: performed.append(e),
                              self.ext.reconcile, owner=token.owner, fence=fence)
        self.assertEqual(len(calls), 2)
        self.assertEqual(performed, [])
        after = a.journal.get(eff.idempotency_key)
        self.assertEqual((after.status, after.dispatches, after.in_flight_owner),
                         ("pending", 0, None))

    def test_dispatch_records_epoch_and_connector_rejects_forged_or_old_tokens(self):
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())
        done = FencedEffects(a).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile)
        self.assertEqual(done.status, "succeeded")
        self.assertEqual(done.result["epoch"], 1)
        self.assertEqual(owner_epoch("node-a@epoch:7"), 7)
        self.assertEqual(owner_epoch("w1"), 0)
        Node("node-b", self.auth, self.p.b).promote()
        for bad in (FencingToken("node-a", 1), FencingToken("node-x", 2),
                    FencingToken("node-b", 3), None):
            with self.assertRaises(StaleEpochRefused):
                self.ext.apply("k", bad)
        self.assertTrue(issubclass(StaleEpochRefused, EffectFailed))
        self.assertEqual(self.ext.count(), 1)

    def test_release_in_flight_requires_matching_owner_and_keeps_unknown(self):
        j = EffectJournal(self.p.a)
        eff = j.record_intent(TASK, "send", action())

        def ambiguous(e):
            raise RuntimeError("timeout")
        with self.assertRaises(Exception):
            j.execute(eff.idempotency_key, ambiguous, self.ext.reconcile, owner="x@epoch:1")
        with j.db.tx() as c:   # simulate the window still being live
            c.execute("UPDATE effects SET in_flight_owner='x@epoch:1', in_flight_until=9e18")
        self.assertFalse(j.release_in_flight(eff.idempotency_key, "y@epoch:1"))
        self.assertTrue(j.release_in_flight(eff.idempotency_key, "x@epoch:1"))
        after = j.get(eff.idempotency_key)
        self.assertEqual((after.status, after.in_flight_owner), ("unknown", None))


# -- 2. node lifecycle ---------------------------------------------------------
class NodeLifecycle(_Tmp):
    def test_new_node_starts_fenced_and_cannot_dispatch(self):
        n = Node("node-a", self.auth, self.p.a)
        self.assertEqual((n.state, n.fence_reason, n.epoch), (FENCED, "new node", None))
        eff = n.journal.record_intent(TASK, "send", action())
        with self.assertRaises(NodeNotActive):
            FencedEffects(n).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile)
        self.assertEqual(self.ext.count(), 0)

    def test_returning_node_starts_fenced_then_rejoins_with_fresh_epoch(self):
        old = self.active_node()
        self.assertEqual((old.state, old.epoch), (ACTIVE, 1))
        back = Node("node-a", self.auth, self.p.a)          # restart, same journal
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
            FencedEffects(old).execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile)
        self.assertEqual(old.state, FENCED)
        self.assertEqual(self.ext.count(), 0)

    def test_superseded_returning_node_stays_fenced(self):
        self.active_node("node-a", self.p.a)
        self.active_node("node-b", self.p.b)                # failover to epoch 2
        back = Node("node-a", self.auth, self.p.a)
        self.assertEqual(back.state, FENCED)
        self.assertFalse(back.rejoin())
        self.assertEqual((back.state, back.fence_reason), (FENCED, "superseded"))
        self.assertEqual(self.auth.current().holder, "node-b")

    def test_promoted_node_paused_until_unpause_after_reconciliation(self):
        a = self.active_node()
        eff = a.journal.record_intent(TASK, "send", action())

        def ambiguous(e, token):
            raise RuntimeError("connection reset")        # outcome unknown
        with self.assertRaises(Exception):
            FencedEffects(a).execute(eff.idempotency_key, ambiguous, self.ext.reconcile)
        self.assertEqual(a.journal.get(eff.idempotency_key).status, "unknown")

        rep = FileCopyReplicator(self.p.replica)
        rep.replicate(self.p.a)
        rep.restore(self.p.b)
        b = Node("node-b", self.auth, self.p.b)
        self.assertEqual(b.promote(), 2)
        self.assertEqual(b.state, PAUSED)
        fx = FencedEffects(b)
        with self.assertRaises(NodeNotActive) as cm:
            fx.execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile)
        self.assertEqual(cm.exception.state, PAUSED)

        # reconciliation that cannot decide keeps it paused
        with self.assertRaises(UnresolvedEffects) as cm:
            b.unpause(lambda e: "still_unknown")
        self.assertEqual(cm.exception.keys, [eff.idempotency_key])
        self.assertEqual(b.state, PAUSED)
        with self.assertRaises(NodeNotActive):
            fx.execute(eff.idempotency_key, self.ext.perform, self.ext.reconcile)

        # real reconcile: the external system never applied it -> failed -> unpause
        report = b.unpause(self.ext.reconcile)
        self.assertEqual(report["resolved"], [eff.idempotency_key])
        self.assertEqual(b.state, ACTIVE)
        self.assertEqual(b.journal.unresolved(), [])
        self.assertEqual(fx.execute(eff.idempotency_key, self.ext.perform,
                                    self.ext.reconcile).status, "succeeded")
        self.assertEqual(self.ext.applied(), [(eff.idempotency_key, "node-b", 2)])

    def test_unpause_refused_when_superseded_while_paused(self):
        b = Node("node-b", self.auth, self.p.b)
        b.promote()
        Node("node-c", self.auth, os.path.join(self.tmp.name, "c.sqlite")).promote()
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
        a = Node("node-a", self.auth, self.p.a)
        b = Node("node-b", self.auth, self.p.b)
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
        fx.execute(ok.idempotency_key, self.ext.perform, self.ext.reconcile)
        a.journal.record_intent(TASK, "s2", action(n=2))   # stays pending
        unk = a.journal.record_intent(TASK, "s3", action(n=3))

        def ambiguous(e, token):
            raise RuntimeError("timeout")
        with self.assertRaises(Exception):
            fx.execute(unk.idempotency_key, ambiguous, self.ext.reconcile)
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
        n = Node("node-a", self.auth, restored)
        self.assertEqual((n.state, n.fence_reason), (FENCED, "restored from replica"))
        self.assertFalse(n.rejoin())
        self.assertEqual(n.state, FENCED)
        self.assertEqual(n.promote(), 2)
        self.assertEqual(n.state, PAUSED)
        n.unpause(self.ext.reconcile)
        self.assertEqual(n.state, ACTIVE)
        self.assertFalse(a.is_current())
        # after promotion the restore marker is cleared: a later restart can rejoin
        again = Node("node-a", self.auth, restored)
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
        n = Node("node-a", self.auth, self.p.a)
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
        Node("node-b", self.auth, self.p.b).promote()
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
