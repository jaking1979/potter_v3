"""E2 merge evidence: responsibility runner (T16) on C1 + C2 + E1.

  DuplicateTrigger       duplicate trigger -> one task, one run, one effect per step
  ReorderedLateEvents    reordered / late events and late schedule ticks -> no double effect
  RestartMidRun          SIGKILL a spawned worker mid-dispatch -> resumes, exactly one effect
  ExpiredInFlightBlocks  kill before the provider applied: reconcile says "not applied", no
                         fence -> the effect stays unresolved and blocks the step (C2 race);
                         with a C4 fence the same restart re-dispatches exactly once
  InterruptMidBatch      /stop (kill flag) mid-batch -> in-flight call completes, nothing after,
                         resumable checkpoint; clearing the flag resumes the remaining steps once
  ApprovalFlow           waiting_approval -> /approve via E1 -> continues once; replay refused
  Watchdog               a silent job that keeps failing is flagged to Josh by the watchdog
  Escalation             3 failures escalate via E1 (ops job, urgent), durably re-enqueued
  LongWork               ack after 30 s, updates on state change only, final message + receipt
  Structure              the runner passes E1's direct-send lint; delivery is declared

Offline only: fake channels, a fake provider in a local SQLite file, test keys.
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import resp_world as rw  # noqa: E402
from notifier import lint  # noqa: E402
from responsibilities import (DefinitionError, Event, Responsibility, Schedule,  # noqa: E402
                              receipt_link)
from runtime import ACKED, SUPPRESSED, UNKNOWN  # noqa: E402
from runtime.tasks import DONE, ESCALATED, QUEUED, WAITING_APPROVAL  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def world(self, **kw):
        return rw.World(self.tmp, **kw)


class DuplicateTrigger(Base):
    def test_duplicate_schedule_trigger_runs_once(self):
        w = self.world()
        first = w.runner.tick()
        again = w.runner.tick()
        w.now += 10
        again += w.runner.tick()                  # same slot, later in it
        self.assertEqual(len(first), 1)
        self.assertEqual(again, [])
        results = w.runner.run_pending()
        self.assertEqual([r.outcome for r in results], ["done"])
        self.assertEqual(w.ext.applied(), [("s1", 1), ("s2", 1), ("s3", 1)])
        self.assertIsNone(w.runner.run_once())
        task = w.runner.tasks.get(first[0].id)
        self.assertEqual(task.state, DONE)
        nid = f"job:notes.batch:{task.id}"
        self.assertIn(nid, w.outbox_ids())
        self.assertIn(receipt_link(task.id), w.body(nid)["text"])

    def test_duplicate_event_from_two_processes_shares_one_task(self):
        w = self.world()
        a = w.runner.on_event("bill.due", {"id": "bill-7", "amount": 80})
        w2 = rw.World(self.tmp, worker="w2")       # another process on the same file
        b = w2.runner.on_event("bill.due", {"id": "bill-7", "amount": 80})
        self.assertTrue(a[0][1])
        self.assertFalse(b[0][1])
        self.assertEqual(a[0][0].id, b[0][0].id)


class ReorderedLateEvents(Base):
    def test_reordered_and_late_events_do_not_double_the_effect(self):
        w = self.world()
        w.runner.on_event("bill.due", {"id": "bill-9", "amount": 80, "seq": 2})
        w.runner.on_event("bill.due", {"id": "bill-9", "amount": 80, "seq": 1})   # reordered
        w.runner.run_pending()                       # log step runs, pay waits for approval
        w.runner.on_event("bill.due", {"id": "bill-9", "amount": 80, "seq": 3})   # late
        w.runner.run_pending()
        self.assertEqual(w.ext.applied("log"), [("log", 1)])
        self.assertEqual(w.ext.calls("log"), 1)
        task = w.runner.on_event("bill.due", {"id": "bill-9", "amount": 80})[0][0]
        self.assertEqual(task.payload["event"]["seq"], 2)    # first arrival is kept

    def test_late_schedule_tick_for_a_superseded_slot_is_dropped(self):
        w = self.world()
        w.now = rw.T0 + 7200
        self.assertEqual(len(w.runner.tick()), 1)
        late = w.runner.tick(now=rw.T0 + 10)         # a tick for an older slot arrives late
        self.assertEqual(late, [])
        w.runner.run_pending()
        self.assertEqual(w.ext.calls("s1"), 1)

    def test_event_triggered_write_needs_approval_even_if_reversible(self):
        # C1 treats event data as untrusted: a reversible write it triggers
        # still waits for Josh (no silent write from external content).
        notes = Responsibility("mail.note", Event("mail.in"), "josh", (rw.note_step("n1"),),
                               cadence_s=86400.0)
        w = self.world(definitions=[notes])
        w.runner.on_event("mail.in", {"id": "m-1"})
        self.assertEqual(w.runner.run_once().outcome, "waiting_approval")
        self.assertEqual(w.ext.calls(), 0)

    def test_composite_keys_with_separators_and_types_do_not_collide(self):
        # Dolores P2: 'a|'+'b' and 'a'+'|b' used to encode alike; so did 1 and "1".
        ev = Event("x", ("a", "b"))
        self.assertNotEqual(ev.dedupe_key({"a": "a|", "b": "b"}),
                            ev.dedupe_key({"a": "a", "b": "|b"}))
        self.assertNotEqual(Event("x").dedupe_key({"id": 1}), Event("x").dedupe_key({"id": "1"}))
        self.assertEqual(ev.dedupe_key({"a": "a|", "b": "b", "noise": 1}),
                         ev.dedupe_key({"b": "b", "a": "a|"}))
        two = Responsibility("pair.watch", ev, "silent", (rw.lookup_step("log"),),
                             cadence_s=86400.0)
        w = self.world(definitions=[two])
        first = w.runner.on_event("x", {"a": "a|", "b": "b", "id": "e1"})
        second = w.runner.on_event("x", {"a": "a", "b": "|b", "id": "e2"})
        self.assertTrue(first[0][1])
        self.assertTrue(second[0][1])
        self.assertNotEqual(first[0][0].id, second[0][0].id)
        for bad in ({"a": True, "b": "b"}, {"a": 1.5, "b": "b"}, {"a": "", "b": "b"},
                    {"a": None, "b": "b"}):
            with self.assertRaises(DefinitionError):
                ev.dedupe_key(bad)

    def payload_note(self, provenance):
        step = rw.Step("n1", "notes.append", "append", "notes/daily", rw.CONNECTOR,
                       parameters=lambda ctx: {"step": "n1",
                                               "line": ctx.payload["event"]["text"]})
        return Responsibility("mail.in", Event("mail.in", ("id",)), "josh", (step,),
                              cadence_s=86400.0, provenance=provenance)

    def test_event_definition_cannot_declare_its_payload_trusted(self):
        # Review @4ebf134 finding 1: provenance=("system_policy",) used to drop
        # external_event, so an attacker-controlled line applied unapproved.
        for prov in (None, ("system_policy",), ("principal_direct", "system_policy"), ()):
            with self.subTest(provenance=prov):
                d = self.payload_note(prov)
                self.assertIn("external_event", d.provenance)
                with tempfile.TemporaryDirectory() as t:
                    w = rw.World(t, definitions=[d])
                    w.runner.on_event("mail.in", {"id": "m-1", "text": "ATTACKER LINE"})
                    self.assertEqual(w.runner.run_once().outcome, "waiting_approval")
                    self.assertEqual(w.ext.calls(), 0)

    def test_event_definition_may_add_sources(self):
        d = self.payload_note(("external_sender",))
        self.assertEqual(set(d.provenance), {"external_event", "external_sender"})

    def test_event_without_its_dedupe_field_is_refused(self):
        w = self.world()
        with self.assertRaises(DefinitionError):
            w.runner.on_event("bill.due", {"amount": 80})


class RestartMidRun(Base):
    def spawn_and_kill(self, phase: str):
        sig = Path(self.tmp) / "signal"
        rw.World(self.tmp, clock=time.time, definitions=[rw.batch()])   # create schema first
        proc = subprocess.Popen([sys.executable, str(HERE / "resp_world.py"), self.tmp,
                                 str(sig), phase])
        try:
            deadline = time.time() + 30
            while not sig.exists():
                if proc.poll() is not None or time.time() > deadline:
                    self.fail("child did not reach the in-flight dispatch")
                time.sleep(0.02)
        finally:
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        self.assertEqual(proc.returncode, -signal.SIGKILL)

    def restarted(self, **kw):
        # Far past the dead worker's lease (60 s) and in-flight window (300 s).
        later = time.time() + 10_000
        return rw.World(self.tmp, clock=lambda: later, definitions=[rw.batch()],
                        worker="w-restarted", **kw)

    def test_killed_after_apply_resumes_with_exactly_one_effect(self):
        self.spawn_and_kill("after")
        w = self.restarted()
        unresolved = w.journal.unresolved()
        self.assertEqual([e.status for e in unresolved], [UNKNOWN])
        results = w.runner.run_pending()
        self.assertEqual([r.outcome for r in results], ["done"])
        self.assertEqual(w.ext.applied(), [("s1", 1), ("s2", 1), ("s3", 1)])
        self.assertEqual(w.ext.calls("s1"), 1)
        self.assertEqual(w.ext.calls("s2"), 1)      # reconciled, never re-dispatched
        self.assertEqual(w.journal.unresolved(), [])


class ExpiredInFlightBlocks(Base):
    def test_expired_in_flight_effect_stays_unresolved_and_blocks_the_step(self):
        RestartMidRun.spawn_and_kill(self, "before")
        w = RestartMidRun.restarted(self)
        self.assertEqual(w.runner.recover()[0].effect_id, "s2")
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "blocked")
        eff = w.journal.for_step(r.task_id, "s2")
        self.assertEqual(eff.status, UNKNOWN)           # "not applied" is not "never lands"
        self.assertEqual(w.ext.calls("s2"), 1)          # never re-dispatched
        self.assertEqual(w.ext.calls("s3"), 0)          # nothing after it ran
        self.assertEqual(w.ext.applied("s2"), [])
        # Repeated restarts keep it blocked and escalate it to Josh (3rd failure).
        results = w.runner.run_pending()
        self.assertEqual(results[-1].outcome, "escalated")
        self.assertEqual(w.runner.tasks.get(r.task_id).state, ESCALATED)
        self.assertIn(f"job:potter.ops:escalation:task:{r.task_id}", w.outbox_ids())
        self.assertEqual(w.ext.calls("s2"), 1)
        self.assertEqual(w.ext.calls("s3"), 0)

    def test_with_a_fence_the_same_restart_redispatches_exactly_once(self):
        RestartMidRun.spawn_and_kill(self, "before")
        w = RestartMidRun.restarted(self, fence=lambda eff: True)    # C4 says: fenced off
        results = w.runner.run_pending()
        self.assertEqual(results[-1].outcome, "done")
        self.assertEqual(w.ext.applied(), [("s1", 1), ("s2", 1), ("s3", 1)])
        self.assertEqual(w.ext.calls("s2"), 2)        # killed attempt + one re-dispatch


class InterruptMidBatch(Base):
    def test_stop_mid_batch_leaves_no_further_effects_and_is_resumable(self):
        w = self.world(definitions=[rw.batch(n=4)])
        w.ext.hooks["s2"] = ("after", lambda eff: w.say("/stop"))   # /stop while s2 in flight
        w.runner.tick()
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "interrupted")
        self.assertEqual(w.ext.applied(), [("s1", 1), ("s2", 1)])  # in-flight call completed
        self.assertEqual(w.journal.for_step(r.task_id, "s2").status, "succeeded")
        task = w.runner.tasks.get(r.task_id)
        self.assertEqual(task.state, QUEUED)
        self.assertEqual(task.checkpoint, {"next": 2, "approvals": {}, "interrupted": True})
        self.assertIsNone(w.runner.run_once())                     # nothing runs while stopped
        self.assertEqual(w.runner.tick(now=w.now + 3600), [])
        self.assertEqual(w.ext.calls("s3"), 0)
        self.assertNotIn(f"job:notes.batch:{r.task_id}", w.outbox_ids())
        # Operator clears the flag: the run resumes at s3, nothing repeats.
        w.ext.hooks.clear()
        w.notifier.kill.clear()
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual(w.ext.applied(), [("s1", 1), ("s2", 1), ("s3", 1), ("s4", 1)])
        self.assertEqual(w.ext.calls(), 4)

    def test_result_message_blocked_by_kill_is_delivered_after_clear(self):
        # Review @4ebf134 minor: a result produced under kill used to be lost
        # (task completed, message never queued).
        w = self.world()
        w.ext.hooks["s3"] = ("after", lambda eff: _set_kill(w))   # last step in flight
        w.runner.tick()
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "interrupted")
        task = w.runner.tasks.get(r.task_id)
        self.assertEqual(task.state, QUEUED)
        self.assertEqual(task.checkpoint["next"], 3)
        nid = f"job:notes.batch:{r.task_id}"
        self.assertNotIn(nid, w.outbox_ids())
        w.ext.hooks.clear()
        w.notifier.kill.clear()
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertIn(nid, w.outbox_ids())
        self.assertEqual(w.ext.calls(), 3)                         # no step re-ran

    def test_dispatch_goes_through_the_marked_kill_seam(self):
        # Review @4ebf134 finding 2: one seam for E1's atomic check-and-send.
        w = self.world(definitions=[rw.batch(n=1)])
        w.runner._dispatch_permitted = lambda: False      # as if /stop won the race
        w.runner.tick()
        self.assertEqual(w.runner.run_once().outcome, "interrupted")
        self.assertEqual(w.ext.calls(), 0)

    def test_kill_flag_set_before_dispatch_stops_before_the_call(self):
        w = self.world()
        w.runner.tick()
        w.ext.hooks["s1"] = ("after", lambda eff: _set_kill(w))   # flag set, no /stop reply
        self.assertEqual(w.runner.run_once().outcome, "interrupted")
        self.assertEqual(w.ext.calls(), 1)


def _set_kill_later(w):
    """Set the kill flag just before the runner asks E1 for an approval."""
    real = w.runner._issue

    def issue(*a, **k):                      # one shot, then the real method
        w.runner._issue = real
        _set_kill(w)
        return real(*a, **k)
    w.runner._issue = issue


def _set_kill(w):
    with w.db.tx() as c:
        w.notifier.kill.set(c, by="test", at=w.now)


class StopIsAPause(Base):
    """Dolores @9c186f8: an interrupt used to be a C2 failure, so the third
    one escalated the task (claim never picks it up again) while the runner
    still reported 'resume'. It is now a lease-checked C2 release."""

    def result_ids(self, w):
        return [i for i in w.outbox_ids() if i.startswith("job:notes.batch:")]

    def test_two_failures_then_success_then_stop_during_finish_delivers_once(self):
        w = self.world(definitions=[rw.batch(n=1)])
        calls = {"n": 0}

        def flaky_then_stop(eff):
            calls["n"] += 1
            if calls["n"] <= 2:
                rw.fail_definitely(eff)            # two transient failures
            _set_kill(w)                           # 3rd attempt applies; /stop lands

        w.ext.hooks["s1"] = ("before", flaky_then_stop)
        w.runner.tick()
        self.assertEqual([r.outcome for r in w.runner.run_pending()],
                         ["retry", "retry", "interrupted"])
        task = w.runner.tasks.by_trigger(
            f"resp:notes.batch:slot:{int(rw.T0 // 3600)}")
        self.assertEqual(task.state, QUEUED)                 # not escalated
        self.assertEqual(task.consecutive_failures, 2)       # the pause added none
        self.assertEqual(self.result_ids(w), [])
        self.assertFalse(any("escalation" in i for i in w.outbox_ids()))
        w.ext.hooks.clear()
        w.notifier.kill.clear()
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual(self.result_ids(w), [f"job:notes.batch:{task.id}"])
        self.assertEqual(w.ext.applied(), [("s1", 1)])
        self.assertEqual(w.runner.tasks.get(task.id).consecutive_failures, 0)
        self.assertIsNone(w.runner.run_once())
        self.assertEqual(len(self.result_ids(w)), 1)         # delivered exactly once

    def test_repeated_stop_resume_cycles_with_a_restart_never_escalate(self):
        n = 5
        w = self.world(definitions=[rw.batch(n=n)])
        w.runner.tick()
        for cycle in range(n):
            if cycle == 2:                                   # restart mid-way
                later = w.now + 10
                w = rw.World(self.tmp, worker=f"w-restart-{cycle}",
                             definitions=[rw.batch(n=n)])
                w.now = later
            w.ext.hooks.clear()
            w.ext.hooks[f"s{cycle + 1}"] = ("after", lambda eff, w=w: _set_kill(w))
            r = w.runner.run_once()
            self.assertEqual(r.outcome, "interrupted", f"cycle {cycle}")
            task = w.runner.tasks.get(r.task_id)
            self.assertEqual(task.state, QUEUED)
            self.assertEqual(task.consecutive_failures, 0)
            w.notifier.kill.clear()
        w.ext.hooks.clear()
        self.assertEqual(w.runner.run_once().outcome, "done")   # held result delivered
        self.assertEqual(w.ext.applied(), [(f"s{i}", 1) for i in range(1, n + 1)])
        self.assertEqual(w.ext.calls(), n)
        self.assertEqual(len(self.result_ids(w)), 1)
        self.assertFalse(any("escalation" in i for i in w.outbox_ids()))

    def test_release_is_lease_checked_and_counts_no_failure(self):
        from runtime import LeaseLost
        w = self.world(definitions=[rw.batch(n=1)])
        store = w.runner.tasks
        task, _ = store.submit("t:release", "notes.batch", job="notes.batch")
        claimed = store.claim("w-a", 60)
        self.assertEqual(claimed.id, task.id)
        with self.assertRaises(LeaseLost):
            store.release(task.id, "w-intruder")             # not the lease holder
        t = store.release(task.id, "w-a")
        self.assertEqual((t.state, t.consecutive_failures, t.lease_owner),
                         (QUEUED, 0, None))
        with self.assertRaises(LeaseLost):
            store.release(task.id, "w-a")                    # no longer running
        self.assertEqual(store.job("notes.batch").consecutive_failures, 0)


class ApprovalIssuanceIsAtomic(Base):
    """Dolores @467b2b9: the find-or-create of an approval request was a read
    followed by a separate write, so overlapping workers could each prompt
    Josh for the same action. It is now one BEGIN IMMEDIATE transaction with
    a primary-key claim on (task, action hash)."""

    def parked_without_request(self, key="race-1"):
        """A bill.watch task in waiting_approval whose request was never issued
        (the worker died between parking and issuing)."""
        w = self.world()
        w.runner.on_event("bill.due", {"id": key, "amount": 10})

        class Crash(BaseException):
            pass

        def boom(*a, **k):
            raise Crash()

        real = w.runner._issue
        w.runner._issue = boom
        with self.assertRaises(Crash):
            w.runner.run_once()
        w.runner._issue = real
        task = w.runner.on_event("bill.due", {"id": key, "amount": 10})[0][0]
        self.assertEqual(task.state, WAITING_APPROVAL)
        self.assertEqual(self.rows(w), (0, 0))
        return w, task

    def rows(self, w):
        with w.db.read() as c:
            return (c.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0],
                    c.execute("SELECT COUNT(*) FROM responsibility_approvals").fetchone()[0])

    def test_two_overlapping_workers_issue_one_request(self):
        import threading
        w, task = self.parked_without_request()
        w2 = rw.World(self.tmp, worker="w2")
        inside, release = threading.Event(), threading.Event()
        real_request = w.issuer.request

        def paused_request(*a, **k):                 # A holds the write lock here
            aid = real_request(*a, **k)
            inside.set()
            release.wait(10)
            return aid

        w.issuer.request = paused_request
        out = {}
        a = threading.Thread(target=lambda: out.setdefault("a", w.runner.poll_approvals()))
        a.start()
        self.assertTrue(inside.wait(10))
        b = threading.Thread(target=lambda: out.setdefault("b", w2.runner.poll_approvals()))
        b.start()
        b.join(0.5)
        self.assertTrue(b.is_alive())                 # B is serialized behind A's lookup+insert
        release.set()
        a.join(10)
        b.join(10)
        self.assertEqual((out["a"], out["b"]), ([], []))  # both keep waiting
        self.assertEqual(self.rows(w), (1, 1))            # ONE prompt, one claim
        self.assertEqual(len([i for i in w.outbox_ids() if i.startswith("approval:")]), 1)

    def test_concurrent_processes_polling_issue_one_request(self):
        w, task = self.parked_without_request("race-2")
        go = Path(self.tmp) / "go"
        procs = [subprocess.Popen([sys.executable, str(HERE / "resp_world.py"), "poll",
                                   self.tmp, str(go), f"p{i}"]) for i in range(4)]
        time.sleep(0.5)
        go.write_text("go")
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        self.assertEqual(self.rows(w), (1, 1))

    def test_restart_after_issue_does_not_prompt_again(self):
        w, task = self.parked_without_request("race-3")
        w.runner.poll_approvals()
        w2 = rw.World(self.tmp, worker="w-restarted")
        w2.runner.poll_approvals()
        w2.runner.run_pending()
        self.assertEqual(self.rows(w2), (1, 1))

    def test_expired_request_ends_the_run_and_is_not_reissued(self):
        w, task = self.parked_without_request("race-4")
        w.runner.poll_approvals()
        w.now += 24 * 3600 + 1
        self.assertEqual([r.outcome for r in w.runner.poll_approvals()], ["failed"])
        self.assertEqual(w.runner.poll_approvals(), [])
        self.assertEqual(self.rows(w), (1, 1))
        self.assertEqual(w.ext.calls("pay"), 0)

    def test_request_cancelled_by_stop_is_not_reissued(self):
        w, task = self.parked_without_request("race-5")
        w.runner.poll_approvals()
        w.say("/stop")
        w.notifier.kill.clear()
        self.assertEqual([r.outcome for r in w.runner.poll_approvals()], ["failed"])
        self.assertEqual(self.rows(w), (1, 1))
        self.assertEqual(w.ext.calls("pay"), 0)

    def test_only_the_claimed_prompt_drives_the_run(self):
        # A stray second E1 request for the same action (issued outside the
        # runner) can neither resume the run nor shadow the claimed one.
        w, task = self.parked_without_request("race-6")
        w.runner.poll_approvals()
        claimed = w.runner._approval_for(task.id, task.checkpoint["approvals"]["pay"])[0]
        action = w.runner._waiting_step(w.runner.tasks.get(task.id))[2]
        w.now += 5
        with w.db.tx() as c:
            stray = w.issuer.request(c, action, summary="stray")
        self.assertNotEqual(stray, claimed)
        self.assertEqual(w.say(f"/approve {stray}").outcome, "approved")
        self.assertEqual(w.runner.poll_approvals(), [])         # still waits on the claim
        self.assertEqual(w.say(f"/approve {claimed}").outcome, "approved")
        self.assertEqual([r.outcome for r in w.runner.poll_approvals()], [QUEUED])
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual(w.ext.calls("pay"), 1)


class ApprovalFlow(Base):
    def test_waiting_approval_then_approve_continues_once(self):
        w = self.world()
        w.runner.on_event("bill.due", {"id": "bill-1", "amount": 120})
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "waiting_approval")
        aid = r.detail
        task = w.runner.tasks.get(r.task_id)
        self.assertEqual(task.state, WAITING_APPROVAL)
        self.assertIn(f"approval:{aid}", w.outbox_ids())
        self.assertEqual(w.ext.calls("pay"), 0)
        self.assertEqual(w.runner.poll_approvals(), [])          # still pending
        self.assertIsNone(w.runner.run_once())
        res = w.say(f"/approve {aid}")
        self.assertEqual(res.outcome, "approved")
        self.assertEqual(w.say(f"/approve {aid}").reason, "already_decided")
        self.assertEqual([p.outcome for p in w.runner.poll_approvals()], [QUEUED])
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual(w.runner.run_pending(), [])
        self.assertEqual(w.runner.poll_approvals(), [])
        self.assertEqual(w.ext.applied(), [("log", 1), ("pay", 1)])
        self.assertEqual(w.ext.calls(), 2)
        self.assertEqual(w.runner.tasks.get(r.task_id).state, DONE)
        record = w.runner._approval_for(r.task_id, w.issuer.get(aid).action_hash)[2]
        self.assertEqual(w.ledger.effect(record["nonce"]).status, "succeeded")

    def approval_count(self, w):
        with w.db.read() as c:
            return c.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]

    def test_request_is_issued_only_once_the_task_is_waiting(self):
        # E1 c8c8422 refuses requests for a task not in waiting_approval.
        w = self.world()
        w.runner.on_event("bill.due", {"id": "bill-6", "amount": 10})
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "waiting_approval")
        self.assertEqual(w.issuer.get(r.detail).task_id, r.task_id)
        self.assertEqual(w.runner.tasks.get(r.task_id).state, WAITING_APPROVAL)

    def test_crash_between_parking_and_request_is_recovered_by_poll(self):
        w = self.world()
        w.runner.on_event("bill.due", {"id": "bill-7", "amount": 10})

        class Crash(BaseException):
            pass

        def boom(*a, **k):
            raise Crash()

        real = w.runner._issue
        w.runner._issue = boom                       # dies after wait_for_approval
        with self.assertRaises(Crash):
            w.runner.run_once()
        w.runner._issue = real
        task = w.runner.on_event("bill.due", {"id": "bill-7", "amount": 10})[0][0]
        self.assertEqual(task.state, WAITING_APPROVAL)
        self.assertEqual(self.approval_count(w), 0)
        self.assertEqual(w.runner.poll_approvals(), [])     # issues the request, keeps waiting
        self.assertEqual(w.runner.poll_approvals(), [])     # never a second prompt
        self.assertEqual(self.approval_count(w), 1)
        with w.db.read() as c:
            aid = c.execute("SELECT approval_id FROM approval_requests").fetchone()[0]
        self.assertEqual(w.say(f"/approve {aid}").outcome, "approved")
        w.runner.poll_approvals()
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual(w.ext.calls("pay"), 1)

    def test_kill_at_request_time_leaves_the_task_waiting_for_a_later_request(self):
        w = self.world()
        w.runner.on_event("bill.due", {"id": "bill-8", "amount": 10})
        w.ext.hooks["log"] = ("after", lambda eff: _set_kill_later(w))
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "waiting_approval")
        self.assertEqual(self.approval_count(w), 0)          # E1 refused while stopped
        self.assertEqual(w.runner.tasks.get(r.task_id).state, WAITING_APPROVAL)
        self.assertEqual(w.runner.poll_approvals(), [])      # still stopped: nothing issued
        self.assertEqual(self.approval_count(w), 0)
        w.notifier.kill.clear()
        w.runner.poll_approvals()
        self.assertEqual(self.approval_count(w), 1)
        self.assertEqual(w.ext.calls("pay"), 0)

    def test_crash_after_consumption_resumes_through_c1_reconcile(self):
        w = self.world()
        w.runner.on_event("bill.due", {"id": "bill-2", "amount": 50})
        aid = w.runner.run_once().detail
        w.say(f"/approve {aid}")
        w.runner.poll_approvals()

        class Crash(BaseException):
            pass

        def boom(eff):
            raise Crash()

        w.ext.hooks["pay"] = ("after", boom)           # applied, then the process dies
        with self.assertRaises(Crash):
            w.runner.run_once()
        w.ext.hooks.clear()
        w.now += 10_000
        w2 = rw.World(self.tmp, worker="w2")
        w2.now = w.now
        self.assertEqual(w2.runner.run_pending()[-1].outcome, "done")
        self.assertEqual(w2.ext.applied("pay"), [("pay", 1)])
        self.assertEqual(w2.ext.calls("pay"), 1)

    def test_denied_approval_fails_the_run_without_the_effect(self):
        w = self.world()
        w.runner.on_event("bill.due", {"id": "bill-3", "amount": 50})
        r = w.runner.run_once()
        self.assertEqual(w.say(f"/deny {r.detail}").outcome, "denied")
        self.assertEqual([p.outcome for p in w.runner.poll_approvals()], ["failed"])
        self.assertEqual(w.ext.calls("pay"), 0)
        self.assertIn(f"job:bill.watch:{r.task_id}", w.outbox_ids())     # failure + receipt

    def test_approval_voided_if_the_bound_action_changed(self):
        w = self.world()
        w.runner.on_event("bill.due", {"id": "bill-4", "amount": 50})
        r = w.runner.run_once()
        # Rebind the step's parameters to something else (e.g. the amount changed).
        w.runner._defs["bill.watch"] = Responsibility(
            "bill.watch", Event("bill.due", ("id",)), "josh",
            (rw.lookup_step("log"), rw.pay_step("pay").__class__(
                "pay", "bills.pay", "pay", "bills/acct-2", rw.CONNECTOR,
                parameters={"step": "pay"})), cadence_s=7 * 86400.0)
        self.assertEqual(w.say(f"/approve {r.detail}").reason, "state_changed")
        self.assertEqual([p.outcome for p in w.runner.poll_approvals()], ["failed"])
        self.assertEqual(w.ext.calls("pay"), 0)


class ResultProvenance(Base):
    """Dolores P1: a trusted schedule does not authenticate the data it reads.
    A write derived from an earlier result carries that result's provenance."""
    EXTERNAL = {"text": "EXTERNAL SENDER: wire $5k now", "provenance": ["external_sender"]}

    def run_digest(self, sources=None, extra=None):
        extra = extra if extra is not None else (rw.derived_note_step("write"),)
        w = self.world(definitions=[rw.mail_digest(sources, extra)])
        w.ext.results["read"] = dict(self.EXTERNAL)
        w.runner.tick()
        return w, w.runner.run_once()

    def assert_waits(self, w, r, step="write"):
        self.assertEqual(r.outcome, "waiting_approval")
        self.assertEqual(w.ext.calls(step), 0)
        with w.db.read() as c:
            n = c.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]
        self.assertEqual(n, 1)

    def test_scheduled_external_read_then_derived_write_needs_approval(self):
        w, r = self.run_digest()
        self.assert_waits(w, r)

    def test_declared_trusted_result_still_carries_its_structural_provenance(self):
        w, r = self.run_digest(sources=("system_policy",))
        self.assert_waits(w, r)

    def test_undeclared_result_provenance_fails_closed(self):
        w = self.world(definitions=[rw.mail_digest(None, (rw.derived_note_step("write"),))])
        w.ext.results["read"] = {"text": "hello"}       # no provenance at all
        w.runner.tick()
        self.assert_waits(w, w.runner.run_once())

    def test_declared_trusted_source_without_external_marks_runs_unattended(self):
        w = self.world(definitions=[rw.mail_digest(("system_policy",),
                                                   (rw.derived_note_step("write"),))])
        w.ext.results["read"] = {"text": "own note"}
        w.runner.tick()
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual(w.ext.calls("write"), 1)

    def test_static_write_after_external_read_is_not_over_blocked(self):
        w, r = self.run_digest(extra=(rw.note_step("static"),))
        self.assertEqual(r.outcome, "done")
        self.assertEqual(w.ext.calls("static"), 1)

    def test_taint_survives_an_intermediate_step(self):
        # read -> static write -> derived write: the third still waits.
        w, r = self.run_digest(extra=(rw.note_step("static"), rw.derived_note_step("write")))
        self.assertEqual(w.ext.calls("static"), 1)
        self.assert_waits(w, r)

    def test_taint_survives_a_restart_after_the_read(self):
        class Crash(BaseException):
            pass

        def die(eff):
            raise Crash()

        w = self.world(definitions=[rw.mail_digest(None, (rw.derived_note_step("write"),))])
        w.ext.results["read"] = dict(self.EXTERNAL)
        w.ext.hooks["read"] = ("after", die)               # read applied, process dies
        w.runner.tick()
        with self.assertRaises(Crash):
            w.runner.run_once()
        w2 = rw.World(self.tmp, worker="w2",
                      definitions=[rw.mail_digest(None, (rw.derived_note_step("write"),))])
        w2.now = w.now + 10_000
        results = w2.runner.run_pending()
        self.assertEqual(results[-1].outcome, "waiting_approval")
        self.assertEqual(w2.ext.calls("write"), 0)
        self.assertEqual(w2.ext.calls("read"), 1)          # reconciled, not re-read

    def test_approved_derived_write_then_runs_once(self):
        w, r = self.run_digest()
        self.assertEqual(w.say(f"/approve {r.detail}").outcome, "approved")
        w.runner.poll_approvals()
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual(w.ext.calls("write"), 1)


class Watchdog(Base):
    def test_silent_job_failure_is_flagged_by_the_watchdog(self):
        quiet = rw.batch(rid="notes.quiet", delivery="silent", n=1)
        w = self.world(definitions=[quiet])
        w.ext.hooks["s1"] = ("before", rw.fail_definitely)
        w.runner.tick()
        w.runner.run_pending()
        # Silent: nothing from the job itself (its escalation goes to Josh via ops).
        self.assertFalse([i for i in w.outbox_ids() if i.startswith("job:notes.quiet:")])
        self.assertEqual(w.runner.watchdog(), [])     # not stale yet
        w.now += 3600 * 1.5 + 1
        self.assertEqual(w.runner.watchdog(), ["notes.quiet"])
        ids = [i for i in w.outbox_ids() if i.startswith("job:potter.ops:watchdog:notes.quiet")]
        self.assertEqual(len(ids), 1)
        w.runner.watchdog()                           # same bucket: no second alert
        self.assertEqual(len([i for i in w.outbox_ids() if "watchdog" in i]), 1)
        self.assertEqual(w.body(ids[0])["kind"], "alert")


class Escalation(Base):
    def test_three_failures_escalate_via_e1(self):
        w = self.world(definitions=[rw.batch(n=1)])
        w.ext.hooks["s1"] = ("before", rw.fail_definitely)
        w.runner.tick()
        outcomes = [r.outcome for r in w.runner.run_pending()]
        self.assertEqual(outcomes, ["retry", "retry", "escalated"])
        task = w.runner.tasks.by_trigger(f"resp:notes.batch:slot:{int(rw.T0 // 3600)}")
        self.assertEqual(task.state, ESCALATED)
        nid = f"job:potter.ops:escalation:task:{task.id}"
        self.assertIn(nid, w.outbox_ids())
        with w.db.read() as c:
            urgency = c.execute("SELECT urgency FROM outbox WHERE notification_id=?",
                                (nid,)).fetchone()[0]
        self.assertEqual(urgency, "urgent")
        self.assertEqual(w.ext.applied(), [])
        w.notifier.run()
        self.assertIn(nid, w.tg.delivered_notifications())

    def test_lost_escalation_callback_is_re_enqueued_by_the_watchdog(self):
        w = self.world(definitions=[rw.batch(n=1)])
        w.ext.hooks["s1"] = ("before", rw.fail_definitely)
        w.runner.tick()
        w.runner.tasks._escalate = lambda e: None      # simulate a crash before the callback
        w.runner.run_pending()
        self.assertFalse(any("escalation" in i for i in w.outbox_ids()))
        w.runner.watchdog()
        self.assertEqual(len([i for i in w.outbox_ids() if "escalation:task" in i]), 1)
        w.runner.watchdog()
        self.assertEqual(len([i for i in w.outbox_ids() if "escalation:task" in i]), 1)

    def test_three_failed_runs_escalate_the_job(self):
        w = self.world(definitions=[rw.batch(n=1)])
        w.ext.hooks["s1"] = ("before", rw.fail_definitely)
        for slot in range(3):
            w.now = rw.T0 + slot * 3600
            w.runner.tick()
            w.runner.run_pending()
        self.assertIsNotNone(w.runner.tasks.job("notes.batch").escalated_at)
        self.assertTrue(any(i.startswith("job:potter.ops:escalation:job:notes.batch")
                            for i in w.outbox_ids()))


class LongWork(Base):
    def test_ack_after_30s_updates_on_change_only_and_final_receipt(self):
        w = self.world()

        def slow(eff):
            w.now += 20

        for s in ("s1", "s2", "s3"):
            w.ext.hooks[s] = ("before", slow)
        w.runner.tick()
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "done")
        base = f"work:notes.batch:{r.task_id}"
        work = [i for i in w.outbox_ids() if i.startswith("work:")]
        self.assertCountEqual(work, [f"{base}:ack", f"{base}:state:1", f"{base}:final"])
        self.assertEqual(w.body(f"{base}:state:1")["state"], "done")
        self.assertEqual(w.body(f"{base}:final")["receipt"], receipt_link(r.task_id))

    def test_short_run_sends_no_long_work_messages(self):
        w = self.world()
        w.runner.tick()
        w.runner.run_once()
        self.assertEqual([i for i in w.outbox_ids() if i.startswith("work:")], [])

    def test_waiting_state_is_reported_after_ack(self):
        w = self.world()
        w.ext.hooks["log"] = ("before", lambda eff: setattr(w, "now", w.now + 31))
        w.runner.on_event("bill.due", {"id": "bill-5", "amount": 9})
        r = w.runner.run_once()
        base = f"work:bill.watch:{r.task_id}"
        self.assertIn(f"{base}:ack", w.outbox_ids())
        self.assertEqual(w.body(f"{base}:state:1")["state"], "waiting_approval")
        # A restarted runner replays the same ticks: no duplicate, no conflict.
        w2 = rw.World(self.tmp, worker="w2")
        w2.now = w.now + 5
        w2.say(f"/approve {r.detail}")
        w2.runner.poll_approvals()
        w2.runner.run_pending()
        work = [i for i in w2.outbox_ids() if i.startswith(base)]
        self.assertCountEqual(work, [f"{base}:ack", f"{base}:state:1", f"{base}:state:2",
                                     f"{base}:state:3", f"{base}:state:4", f"{base}:final"])
        self.assertEqual([w2.body(f"{base}:state:{n}")["state"] for n in (2, 3, 4)],
                         ["queued", "running", "done"])


class Structure(Base):
    def test_runner_passes_the_direct_send_lint(self):
        self.assertEqual(lint.scan(HERE.parent), [])

    def test_runner_holds_no_channel_or_notifier(self):
        w = self.world()
        for v in vars(w.runner).values():
            self.assertNotIn(type(v).__name__, {"Notifier", "FakeChannel", "Outbox"})

    def test_definitions_must_declare_delivery_and_cadence(self):
        with self.assertRaises(DefinitionError):
            Responsibility("x.y", Schedule(60), "everyone", (rw.note_step("a"),))
        with self.assertRaises(DefinitionError):
            Responsibility("x.y", Event("t"), "josh", (rw.note_step("a"),))
        with self.assertRaises(ValueError):
            rw.World(self.tmp, definitions=[Responsibility(
                "x.y", Schedule(60), "josh",
                (rw.note_step("a").__class__("a", "notes.append", "o", "t", "nope"),))])

    def test_stop_suppresses_a_queued_result(self):
        # Sanity: the /stop sweep suppresses the queued result, nothing sent after.
        w = self.world()
        w.runner.tick()
        w.runner.run_once()
        w.say("/stop")
        w.notifier.run()
        with w.db.read() as c:
            statuses = {s for (s,) in c.execute(
                "SELECT status FROM outbox WHERE notification_id LIKE 'job:notes.batch:%'")}
        self.assertEqual(statuses, {SUPPRESSED})
        self.assertNotIn(ACKED, statuses)


if __name__ == "__main__":
    unittest.main()
