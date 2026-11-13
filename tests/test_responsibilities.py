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
import json
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
from responsibilities import (Connector, DefinitionError, Event, KillSwitch,  # noqa: E402
                              Responsibility, ResponsibilityRunner, Schedule, kill_switch_for,
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


class LongWorkTruthIsTheOutbox(Base):
    """Dolores @467b2b9: _report committed local history before E1's tick, so
    a crash in between left history saying 'acked' while the outbox had no
    ack (and a state update could be skipped); and a task waiting on a
    pending approval was never ticked again, so the 30 s ack never came.
    Now E1's durable outbox is the only record of what was told."""

    def work_rows(self, w, task_id, job):
        prefix = f"work:{job}:{task_id}:"
        with w.db.read() as c:
            return [(r[0][len(prefix):], json.loads(r[1]))
                    for r in c.execute("SELECT notification_id, body FROM outbox WHERE"
                                       " channel='telegram' AND notification_id LIKE ?"
                                       " ORDER BY created_at, rowid", (prefix + "%",))]

    def crash_first_tick_at_or_after(self, w, seconds, started):
        from unittest import mock
        from notifier.service import LongWork

        class Crash(BaseException):
            pass

        real = LongWork.tick
        state = {"armed": True}

        def tick(lw, st, now):
            if state["armed"] and now - started >= seconds:
                state["armed"] = False
                raise Crash()                 # after E2 pinned the start, before E1 enqueued
            return real(lw, st, now)

        return Crash, mock.patch.object(LongWork, "tick", tick)

    def test_crash_between_pin_and_e1_tick_still_acks_once_and_in_order(self):
        w = self.world()
        w.ext.hooks["log"] = ("after", lambda eff: setattr(w, "now", w.now + 31))
        w.runner.on_event("bill.due", {"id": "lw-1", "amount": 5})
        Crash, patch = self.crash_first_tick_at_or_after(w, 30, rw.T0)
        with patch, self.assertRaises(Crash):
            w.runner.run_once()               # dies at the first >=30 s tick (no ack yet)
        task = w.runner.on_event("bill.due", {"id": "lw-1", "amount": 5})[0][0]
        self.assertEqual(self.work_rows(w, task.id, "bill.watch"), [])
        w2 = rw.World(self.tmp, worker="w-restarted")
        w2.now = w.now + 10_000               # dead worker's lease has expired
        r = w2.runner.run_pending()[-1]
        self.assertEqual(r.outcome, "waiting_approval")
        rows = self.work_rows(w2, task.id, "bill.watch")
        self.assertEqual([k for k, _ in rows], ["ack", "state:1"])          # ack recovered
        self.assertEqual(rows[1][1]["state"], "waiting_approval")
        w2.say(f"/approve {r.detail}")
        w2.runner.poll_approvals()
        w2.runner.run_pending()
        kinds = [k for k, _ in self.work_rows(w2, task.id, "bill.watch")]
        self.assertEqual(kinds.count("ack"), 1)
        self.assertEqual(kinds[0], "ack")
        self.assertEqual(kinds[-1], "final")
        self.assertEqual([b["state"] for k, b in self.work_rows(w2, task.id, "bill.watch")
                          if k.startswith("state:")],
                         ["waiting_approval", "queued", "running", "done"])

    def test_crash_before_a_state_update_is_recovered_by_the_next_report(self):
        w = self.world()
        w.ext.hooks["log"] = ("after", lambda eff: setattr(w, "now", w.now + 31))
        w.runner.on_event("bill.due", {"id": "lw-2", "amount": 5})
        from unittest import mock
        from notifier.service import LongWork

        class Crash(BaseException):
            pass

        real = LongWork.tick

        def tick(lw, st, now):
            if st == WAITING_APPROVAL:
                raise Crash()                 # the waiting update is lost in the crash
            return real(lw, st, now)

        with mock.patch.object(LongWork, "tick", tick), self.assertRaises(Crash):
            w.runner.run_once()
        task = w.runner.on_event("bill.due", {"id": "lw-2", "amount": 5})[0][0]
        self.assertEqual([k for k, _ in self.work_rows(w, task.id, "bill.watch")], ["ack"])
        w2 = rw.World(self.tmp, worker="w2")
        w2.now = w.now + 5
        w2.runner.poll_approvals()            # issues the lost approval request
        w2.runner.poll_approvals()            # pending: ticks waiting -> update sent
        rows = self.work_rows(w2, task.id, "bill.watch")
        self.assertEqual([k for k, _ in rows], ["ack", "state:1"])
        self.assertEqual(rows[1][1]["state"], "waiting_approval")

    def test_pending_approval_past_30s_gets_exactly_one_ack_from_polls(self):
        w = self.world()
        w.runner.on_event("bill.due", {"id": "lw-3", "amount": 5})
        r = w.runner.run_once()               # parks immediately, before 30 s
        self.assertEqual(r.outcome, "waiting_approval")
        self.assertEqual(self.work_rows(w, r.task_id, "bill.watch"), [])
        w.now += 31
        for _ in range(3):
            self.assertEqual(w.runner.poll_approvals(), [])
        self.assertEqual([k for k, _ in self.work_rows(w, r.task_id, "bill.watch")], ["ack"])

    def test_report_progress_acks_during_one_long_step(self):
        w = self.world(definitions=[rw.batch(n=1)])

        def long_step(eff):
            w.now += 45
            self.assertEqual(w.runner.report_progress(), 1)   # periodic reporter mid-step

        w.ext.hooks["s1"] = ("before", long_step)
        w.runner.tick()
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "done")
        kinds = [k for k, _ in self.work_rows(w, r.task_id, "notes.batch")]
        self.assertEqual(kinds[0], "ack")
        self.assertEqual(kinds.count("ack"), 1)
        self.assertEqual(kinds[-1], "final")


def approval_prompts(w):
    with w.db.read() as c:
        return c.execute("SELECT COUNT(*) FROM outbox WHERE channel='telegram' AND"
                         " notification_id LIKE 'approval:%'").fetchone()[0]


def approval_rows(w):
    with w.db.read() as c:
        return c.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]


def schema_version(tmp):
    from runtime import Database
    with Database(Path(tmp) / "potter.db").read() as c:
        return c.execute("SELECT value FROM meta WHERE key='responsibilities_schema'"
                         ).fetchone()[0]


class UpgradeFromLegacy(Base):
    """Dolores @e6a7af6 (reproduced): the e2.v0 -> v1 upgrade created an empty
    claim table, so a legacy waiting task with an E1 request got a SECOND
    prompt. Databases here are produced by the actual prior E2 code
    (tests/legacy/), then opened by this code."""

    def legacy(self, scenario, *then, base="e2_467b2b9"):
        out = subprocess.run([sys.executable, str(HERE / "legacy_fixture.py"), base, scenario,
                              self.tmp, *then], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        info = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(schema_version(self.tmp), "e2.v1" if then else "e2.v0")
        return info

    def upgraded(self, **kw):
        w = rw.World(self.tmp, **kw)
        self.assertEqual(schema_version(self.tmp), "e2.v3")
        return w

    def claim(self, w, task_id):
        with w.db.read() as c:
            rows = c.execute("SELECT approval_id FROM responsibility_approvals WHERE task_id=?",
                             (task_id,)).fetchall()
        return [r[0] for r in rows]

    def test_pending_request_is_claimed_and_never_reprompted(self):
        info = self.legacy("pending")
        w = self.upgraded()
        self.assertEqual(self.claim(w, info["task_id"]), [info["approval_id"]])
        for _ in range(3):
            self.assertEqual(w.runner.poll_approvals(), [])
        self.assertEqual((approval_rows(w), approval_prompts(w)), (1, 1))
        self.assertEqual(w.say(f"/approve {info['approval_id']}").outcome, "approved")
        self.assertEqual([r.outcome for r in w.runner.poll_approvals()], [QUEUED])
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual(w.ext.calls("pay"), 1)
        self.assertEqual(approval_prompts(w), 1)

    def test_reproduced_v0_then_v1_path_reconciles_on_v2(self):
        # Dolores's sequence: v0 data, reopened by e6a7af6 (empty migration).
        info = self.legacy("pending", "e2_e6a7af6")
        w = self.upgraded()
        self.assertEqual(self.claim(w, info["task_id"]), [info["approval_id"]])
        w.runner.poll_approvals()
        self.assertEqual((approval_rows(w), approval_prompts(w)), (1, 1))

    def test_approved_waiting_request_resumes_without_a_prompt(self):
        info = self.legacy("approved")
        w = self.upgraded()
        self.assertEqual(self.claim(w, info["task_id"]), [info["approval_id"]])
        self.assertEqual([r.outcome for r in w.runner.poll_approvals()], [QUEUED])
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual((w.ext.calls("pay"), approval_prompts(w)), (1, 1))

    def test_approved_and_queued_task_resumes_without_a_prompt(self):
        info = self.legacy("approved_queued")
        w = self.upgraded()
        self.assertEqual(self.claim(w, info["task_id"]), [info["approval_id"]])
        self.assertEqual(w.runner.run_pending()[-1].outcome, "done")
        self.assertEqual((w.ext.calls("pay"), approval_prompts(w)), (1, 1))

    def test_terminal_request_is_held_for_an_operator(self):
        info = self.legacy("terminal")
        w = self.upgraded()
        self.assertEqual(self.claim(w, info["task_id"]), [])
        self.assertEqual(w.runner.holds(),
                         [(info["task_id"], "pay", "legacy request is denied")])
        self.assertEqual(w.runner.poll_approvals(), [])
        self.assertEqual(w.runner.run_pending(), [])
        self.assertEqual((approval_rows(w), approval_prompts(w)), (1, 1))
        self.assertIn(f"job:potter.ops:hold:{info['task_id']}", w.outbox_ids())
        w.runner.resolve_hold(info["task_id"], None)            # operator ends the run
        self.assertEqual(w.runner.tasks.get(info["task_id"]).state, "failed")
        self.assertEqual((approval_rows(w), w.ext.calls("pay")), (1, 0))

    def test_no_request_means_no_prompt_was_ever_sent_so_one_is_issued(self):
        # E1 writes a request and its prompt in one transaction: no row means
        # Josh was never asked. Issuing the first prompt is not a duplicate.
        info = self.legacy("none")
        w = self.upgraded()
        self.assertEqual((approval_rows(w), approval_prompts(w)), (0, 0))
        self.assertEqual(w.runner.holds(), [])
        w.runner.poll_approvals()
        w.runner.poll_approvals()
        self.assertEqual((approval_rows(w), approval_prompts(w)), (1, 1))

    def test_multiple_legacy_requests_are_held_never_guessed(self):
        info = self.legacy("multiple")
        w = self.upgraded()
        self.assertEqual(self.claim(w, info["task_id"]), [])
        self.assertEqual(w.runner.holds(),
                         [(info["task_id"], "pay", "2 legacy requests")])
        self.assertEqual(w.say(f"/approve {info['approval_id']}").outcome, "approved")
        self.assertEqual(w.runner.poll_approvals(), [])          # no authority guessed
        self.assertEqual((approval_rows(w), approval_prompts(w)), (2, 2))
        w.runner.resolve_hold(info["task_id"], info["approval_id"])   # operator picks
        self.assertEqual([r.outcome for r in w.runner.poll_approvals()], [QUEUED])
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual((w.ext.calls("pay"), approval_rows(w), approval_prompts(w)), (1, 2, 2))

    def test_resolve_hold_refuses_an_approval_for_another_task(self):
        info = self.legacy("multiple")
        w = self.upgraded()
        with self.assertRaises(ValueError):
            w.runner.resolve_hold(info["task_id"], "appr-not-this-task")
        self.assertEqual(len(w.runner.holds()), 1)

    def test_held_queued_task_is_parked_not_run(self):
        info = self.legacy("multiple_queued")
        w = self.upgraded()
        self.assertEqual(w.runner.tasks.get(info["task_id"]).state, QUEUED)
        r = w.runner.run_once()
        self.assertEqual((r.outcome, r.detail), ("held", "2 legacy requests"))
        self.assertEqual(w.runner.tasks.get(info["task_id"]).state, WAITING_APPROVAL)
        self.assertEqual(w.runner.run_pending(), [])
        self.assertEqual(w.runner.poll_approvals(), [])
        self.assertEqual((w.ext.calls("pay"), approval_prompts(w)), (0, 2))

    def test_interrupted_upgrade_leaves_v0_and_no_partial_claims(self):
        from responsibilities import _schema
        info = self.legacy("pending")
        real = _schema._reconcile_legacy

        def dies_midway(c, now):
            real(c, now)                 # claims written ...
            raise RuntimeError("power cut during upgrade")   # ... then the process dies

        _schema._reconcile_legacy = dies_midway
        try:
            with self.assertRaises(RuntimeError):
                rw.World(self.tmp)
        finally:
            _schema._reconcile_legacy = real
        self.assertEqual(schema_version(self.tmp), "e2.v0")      # rolled back whole
        from runtime import Database
        with Database(Path(self.tmp) / "potter.db").read() as c:
            have = c.execute("SELECT name FROM sqlite_master WHERE name="
                             "'responsibility_approvals'").fetchone()
            claims = c.execute("SELECT COUNT(*) FROM responsibility_approvals").fetchone()[0] \
                if have else 0
        self.assertEqual(claims, 0)
        w = self.upgraded()                                        # retried on restart
        self.assertEqual(self.claim(w, info["task_id"]), [info["approval_id"]])
        w.runner.poll_approvals()
        self.assertEqual(approval_prompts(w), 1)

    def test_restart_after_upgrade_changes_nothing(self):
        info = self.legacy("multiple")
        w = self.upgraded()
        before = (w.runner.holds(), self.claim(w, info["task_id"]))
        w2 = rw.World(self.tmp, worker="w2")
        w2.runner.poll_approvals()
        self.assertEqual((w2.runner.holds(), self.claim(w2, info["task_id"])), before)
        self.assertEqual(approval_prompts(w2), 2)

    def test_old_code_refuses_the_upgraded_database(self):
        self.legacy("pending")
        self.upgraded()
        out = subprocess.run([sys.executable, "-c",
                              "import sys; sys.argv=['x']; sys.path.insert(0, sys.argv[0]);"
                              f"import legacy_fixture as lf; lf.reopen('e2_467b2b9', {self.tmp!r})"],
                             cwd=str(HERE), capture_output=True, text=True, timeout=120)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("JournalUnavailable", out.stderr)
        self.assertIn("e2.v3", out.stderr)


class ScheduleOrdering(Base):
    """Dolores @e6a7af6 (reproduced): tick() committed the watermark, then
    submitted the task in a second transaction, so an older tick paused in
    between could create a stale slot after a newer tick advanced it."""

    def slots(self, w):
        with w.db.read() as c:
            return sorted(json.loads(r[0])["slot"] for r in c.execute(
                "SELECT payload FROM tasks WHERE kind='notes.batch'"))

    def test_older_tick_paused_before_insert_serializes_the_newer_one(self):
        import threading
        old, new = self.world(), rw.World(self.tmp, worker="w2")
        inside, release = threading.Event(), threading.Event()
        real = old.runner.tasks.submit

        def paused(*a, **k):                  # old has read + moved the watermark
            inside.set()
            release.wait(10)
            return real(*a, **k)

        old.runner.tasks.submit = paused
        out = {}
        a = threading.Thread(target=lambda: out.setdefault("old", old.runner.tick(now=rw.T0 + 10)))
        a.start()
        self.assertTrue(inside.wait(10))
        b = threading.Thread(target=lambda: out.setdefault("new",
                                                           new.runner.tick(now=rw.T0 + 7200)))
        b.start()
        b.join(0.5)
        self.assertTrue(b.is_alive())         # the newer tick waits for the older decision
        release.set()
        a.join(10)
        b.join(10)
        s0, s2 = int((rw.T0 + 10) // 3600), int((rw.T0 + 7200) // 3600)
        self.assertEqual(self.slots(old), [s0, s2])
        self.assertEqual(new.runner.tick(now=rw.T0 + 10), [])    # now stale: dropped

    def test_newer_tick_first_means_the_older_slot_is_never_created(self):
        old, new = self.world(), rw.World(self.tmp, worker="w2")
        self.assertEqual(len(new.runner.tick(now=rw.T0 + 7200)), 1)
        self.assertEqual(old.runner.tick(now=rw.T0 + 10), [])
        self.assertEqual(self.slots(old), [int((rw.T0 + 7200) // 3600)])

    def test_crash_between_watermark_and_insert_records_neither(self):
        w = self.world()

        class Crash(BaseException):
            pass

        def boom(*a, **k):
            raise Crash()

        real = w.runner.tasks.submit
        w.runner.tasks.submit = boom
        with self.assertRaises(Crash):
            w.runner.tick(now=rw.T0 + 10)
        w.runner.tasks.submit = real
        with w.db.read() as c:
            self.assertIsNone(c.execute("SELECT value FROM responsibility_state WHERE"
                                        " key='watermark:notes.batch'").fetchone())
        self.assertEqual(self.slots(w), [])
        # Recovery: the next tick in the same slot creates it ...
        self.assertEqual(len(rw.World(self.tmp, worker="w2").runner.tick(now=rw.T0 + 20)), 1)
        # ... a restart in the same slot creates nothing more.
        self.assertEqual(rw.World(self.tmp, worker="w3").runner.tick(now=rw.T0 + 30), [])
        self.assertEqual(self.slots(w), [int((rw.T0 + 10) // 3600)])

    def test_crash_then_next_tick_in_a_later_slot_skips_the_missed_one(self):
        w = self.world()

        class Crash(BaseException):
            pass

        def boom(*a, **k):
            raise Crash()

        w.runner.tasks.submit = boom
        with self.assertRaises(Crash):
            w.runner.tick(now=rw.T0 + 10)
        w2 = rw.World(self.tmp, worker="w2")
        w2.runner.tick(now=rw.T0 + 7200)
        self.assertEqual(self.slots(w2), [int((rw.T0 + 7200) // 3600)])   # documented skip


class FinalOnlyAfterTerminalCommit(Base):
    """Dolores @e6a7af6 (reproduced): _finish enqueued the 'done' update and
    the final receipt before C2's complete() committed, so a crash there left a
    success announcement for a task C2 still recorded as running."""

    def successes(self, w, task_id):
        out = []
        with w.db.read() as c:
            for nid, body in c.execute("SELECT notification_id, body FROM outbox WHERE"
                                       " channel='telegram'"):
                b = json.loads(body)
                if nid == f"job:notes.batch:{task_id}" or (
                        nid.startswith(f"work:notes.batch:{task_id}:") and (
                            b.get("kind") == "work_final" or b.get("state") == "done")):
                    out.append(nid[len(f"work:notes.batch:{task_id}:"):]
                               if nid.startswith("work:") else "result")
        return sorted(out)

    def long_batch(self):
        w = self.world(definitions=[rw.batch(n=1)])
        w.ext.hooks["s1"] = ("before", lambda eff: setattr(w, "now", w.now + 31))
        w.runner.tick()
        return w

    def test_crash_before_complete_announces_nothing_and_restart_announces_once(self):
        w = self.long_batch()

        class Crash(BaseException):
            pass

        def boom(*a, **k):
            raise Crash()

        w.runner.tasks.complete = boom
        with self.assertRaises(Crash):
            w.runner.run_once()
        task = w.runner.tasks.by_trigger(f"resp:notes.batch:slot:{int(rw.T0 // 3600)}")
        self.assertEqual(task.state, "running")
        self.assertEqual(self.successes(w, task.id), [])
        w.notifier.run()                                       # dispatch before recovery
        self.assertFalse([n for n in w.tg.delivered_notifications()
                          if n == f"job:notes.batch:{task.id}"
                          or n.endswith(":final")])
        w2 = rw.World(self.tmp, worker="w2", definitions=[rw.batch(n=1)])
        w2.now = w.now + 10_000
        self.assertEqual(w2.runner.run_pending()[-1].outcome, "done")
        kinds = self.successes(w2, task.id)
        self.assertEqual(kinds.count("result"), 1)
        self.assertEqual(kinds.count("final"), 1)
        self.assertEqual(w2.ext.calls("s1"), 1)

    def test_crash_after_commit_before_final_is_recovered_once(self):
        w = self.long_batch()

        class Crash(BaseException):
            pass

        def boom(*a, **k):
            raise Crash()

        w.runner._deliver_final = boom
        with self.assertRaises(Crash):
            w.runner.run_once()
        task = w.runner.tasks.by_trigger(f"resp:notes.batch:slot:{int(rw.T0 // 3600)}")
        self.assertEqual(task.state, DONE)                        # terminal is committed
        self.assertEqual(self.successes(w, task.id), ["result"])  # result was in that commit
        w2 = rw.World(self.tmp, worker="w2", definitions=[rw.batch(n=1)])
        w2.now = w.now + 5
        w2.runner.report_progress()                               # recovery sweep
        w2.runner.watchdog()
        w2.runner.report_progress()
        kinds = self.successes(w2, task.id)
        self.assertEqual(kinds.count("final"), 1)
        self.assertEqual(kinds.count("result"), 1)
        self.assertEqual(len([k for k in kinds if k.startswith("state:")]), 1)

    def test_lease_lost_at_complete_announces_nothing(self):
        w = self.long_batch()
        real = w.runner.tasks.complete

        def late(*a, **k):
            w.now += 1000                                        # lease expired meanwhile
            return real(*a, **k)

        w.runner.tasks.complete = late
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "lease_lost")
        self.assertEqual(self.successes(w, r.task_id), [])
        w.runner.tasks.complete = real
        self.assertEqual(w.runner.run_pending()[-1].outcome, "done")
        self.assertEqual(self.successes(w, r.task_id).count("result"), 1)
        self.assertEqual(w.ext.calls("s1"), 1)

    def test_kill_at_the_announcement_rolls_back_completion(self):
        w = self.long_batch()

        class StoppingHandle:
            silent = False

            def notify(self, *a, **k):
                # E1 refuses because another process committed /stop just now.
                from notifier import Stopped
                raise Stopped("kill flag set")

            def long_work(self, *a, **k):
                return w.notifier.handle_for(rw.batch(n=1).job_spec).long_work(*a, **k)

        real = w.runner._handles["notes.batch"]
        w.runner._handles["notes.batch"] = StoppingHandle()
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "interrupted")
        task = w.runner.tasks.get(r.task_id)
        self.assertEqual((task.state, task.consecutive_failures), (QUEUED, 0))
        self.assertEqual(self.successes(w, r.task_id), [])
        w.runner._handles["notes.batch"] = real
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual(self.successes(w, r.task_id).count("result"), 1)
        self.assertEqual(w.ext.calls("s1"), 1)


class FinalMarkedOnlyOnProof(Base):
    """Dolores @59a2e75 (reproduced via E1's real missing-address path):
    _deliver_final read every NotifierError as 'already finished' and marked
    the final delivered with no final in the outbox, so it was lost."""

    def committed_but_undelivered(self):
        """A long-work notes.batch run whose terminal state is committed and
        whose final is still owed (the worker died before delivering it)."""
        w = self.world(definitions=[rw.batch(n=1)])
        w.ext.hooks["s1"] = ("before", lambda eff: setattr(w, "now", w.now + 31))
        w.runner.tick()

        class Crash(BaseException):
            pass

        def boom(*a, **k):
            raise Crash()

        w.runner._deliver_final = boom
        with self.assertRaises(Crash):
            w.runner.run_once()
        task = w.runner.tasks.by_trigger(f"resp:notes.batch:slot:{int(rw.T0 // 3600)}")
        self.assertEqual(task.state, DONE)
        return w, task

    def finals(self, w, task_id):
        with w.db.read() as c:
            delivered = c.execute("SELECT delivered_at FROM responsibility_finals WHERE"
                                  " task_id=?", (task_id,)).fetchone()[0]
            n = c.execute("SELECT COUNT(*) FROM outbox WHERE channel='telegram' AND"
                          " notification_id=?", (f"work:notes.batch:{task_id}:final",)
                          ).fetchone()[0]
        return delivered is not None, n

    def test_missing_address_keeps_the_final_pending_until_it_is_restored(self):
        from identity.seed import JOSH
        w, task = self.committed_but_undelivered()
        w2 = rw.World(self.tmp, worker="w2", definitions=[rw.batch(n=1)])
        w2.now = w.now + 5
        w2.auth._out[JOSH].pop("telegram")          # address lost (config change)
        self.assertEqual(w2.runner.deliver_pending_finals(), 0)
        w2.runner.report_progress()
        w2.runner.watchdog()
        self.assertEqual(self.finals(w2, task.id), (False, 0))   # pending, nothing faked
        w3 = rw.World(self.tmp, worker="w3", definitions=[rw.batch(n=1)])  # restart, restored
        w3.now = w2.now + 5
        self.assertEqual(w3.runner.deliver_pending_finals(), 1)
        self.assertEqual(self.finals(w3, task.id), (True, 1))
        self.assertEqual(w3.runner.deliver_pending_finals(), 0)
        w3.runner.report_progress()
        self.assertEqual(self.finals(w3, task.id), (True, 1))     # exactly once

    def test_previously_enqueued_final_is_marked_once(self):
        w, task = self.committed_but_undelivered()
        w2 = rw.World(self.tmp, worker="w2", definitions=[rw.batch(n=1)])
        w2.now = w.now + 5
        lw = w2.notifier.handle_for(rw.batch(n=1).job_spec).long_work(task.id, rw.T0)
        lw.finish(receipt_link(task.id), w2.now)     # final landed; marker did not
        self.assertEqual(self.finals(w2, task.id), (False, 1))
        self.assertEqual(w2.runner.deliver_pending_finals(), 1)  # 'already finished' + proof
        self.assertEqual(self.finals(w2, task.id), (True, 1))
        self.assertEqual(w2.runner.deliver_pending_finals(), 0)

    def test_absent_definition_stays_pending(self):
        w, task = self.committed_but_undelivered()
        w2 = rw.World(self.tmp, worker="w2", definitions=[rw.bill_watch()])   # no notes.batch
        w2.now = w.now + 5
        self.assertEqual(w2.runner.deliver_pending_finals(), 0)
        w2.runner.watchdog()
        self.assertEqual(self.finals(w2, task.id), (False, 0))
        w3 = rw.World(self.tmp, worker="w3", definitions=[rw.batch(n=1)])
        w3.now = w2.now + 5
        self.assertEqual(w3.runner.deliver_pending_finals(), 1)
        self.assertEqual(self.finals(w3, task.id), (True, 1))

    def test_short_run_owes_no_final_and_is_marked(self):
        w = self.world(definitions=[rw.batch(n=1)])
        w.runner.tick()
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "done")
        self.assertEqual(self.finals(w, r.task_id), (True, 0))

    def test_kill_keeps_the_final_pending(self):
        w, task = self.committed_but_undelivered()
        _set_kill(w)
        w2 = rw.World(self.tmp, worker="w2", definitions=[rw.batch(n=1)])
        w2.now = w.now + 5
        self.assertEqual(w2.runner.deliver_pending_finals(), 0)
        self.assertEqual(self.finals(w2, task.id), (False, 0))
        w2.notifier.kill.clear()
        self.assertEqual(w2.runner.deliver_pending_finals(), 1)
        self.assertEqual(self.finals(w2, task.id), (True, 1))


class TerminalFinalsAreDurable(Base):
    """Dolores @6f073f4 (reproduced): FAILED / ESCALATED / rejected-approval
    paths committed the terminal state and then sent their final messages
    with no durable record, so a crash in between lost the long-work final
    and the failure alert for good."""

    class Crash(BaseException):
        pass

    def crash_at_record(self, w):
        def boom(*a, **k):
            raise self.Crash()
        w.runner._record_terminal = boom

    def counts(self, w, job, task_id):
        with w.db.read() as c:
            rows = [r[0] for r in c.execute("SELECT notification_id FROM outbox WHERE"
                                            " channel='telegram'")]
            fin = c.execute("SELECT outcome, delivered_at FROM responsibility_finals WHERE"
                            " task_id=?", (task_id,)).fetchone()
        return {"alert": rows.count(f"job:{job}:{task_id}"),
                "final": rows.count(f"work:{job}:{task_id}:final"),
                "escalation": rows.count(f"job:potter.ops:escalation:task:{task_id}"),
                "finals": None if fin is None else (fin[0], fin[1] is not None)}

    def fatal_def(self, rid="notes.fatal", long=True, delivery="josh"):
        holder = {}

        def boom_params(ctx):
            if long:
                holder["w"].now += 31          # the run has been going for 30 s+
            raise ValueError("cannot bind")    # -> _Fatal -> FAILED

        d = Responsibility(rid, Schedule(3600.0), delivery,
                           (rw.Step("s1", "notes.append", "append", "notes/x", rw.CONNECTOR,
                                    parameters=boom_params),))
        return d, holder

    def reopen(self, w, defs, **kw):
        w2 = rw.World(self.tmp, worker="w-reopened", definitions=defs, **kw)
        w2.now = w.now + 5
        return w2

    def test_failed_crash_after_commit_is_recovered_once(self):
        d, holder = self.fatal_def()
        w = self.world(definitions=[d])
        holder["w"] = w
        w.runner.tick()
        self.crash_at_record(w)
        with self.assertRaises(self.Crash):
            w.runner.run_once()
        tid = w.runner.tasks.by_trigger(f"resp:notes.fatal:slot:{int(rw.T0 // 3600)}").id
        self.assertEqual(w.runner.tasks.get(tid).state, "failed")
        self.assertEqual(self.counts(w, "notes.fatal", tid),
                         {"alert": 0, "final": 0, "escalation": 0, "finals": None})
        w2 = self.reopen(w, [d])
        holder["w"] = w2
        for _ in range(3):
            w2.runner.deliver_pending_finals()
            w2.runner.report_progress()
            w2.runner.watchdog()
        self.assertEqual(self.counts(w2, "notes.fatal", tid),
                         {"alert": 1, "final": 1, "escalation": 0, "finals": ("failed", True)})
        final = w2.body(f"work:notes.fatal:{tid}:final")
        self.assertEqual((final["outcome"], final["receipt"]), ("failed", receipt_link(tid)))

    def test_failed_crash_before_commit_announces_nothing_until_it_really_fails(self):
        d, holder = self.fatal_def(long=False)
        w = self.world(definitions=[d])
        holder["w"] = w
        w.runner.tick()

        def boom(*a, **k):
            raise self.Crash()

        real = w.runner.tasks.fail
        w.runner.tasks.fail = boom
        with self.assertRaises(self.Crash):
            w.runner.run_once()
        tid = w.runner.tasks.by_trigger(f"resp:notes.fatal:slot:{int(rw.T0 // 3600)}").id
        self.assertEqual(w.runner.tasks.get(tid).state, "running")
        w.runner.deliver_pending_finals()
        self.assertEqual(self.counts(w, "notes.fatal", tid),
                         {"alert": 0, "final": 0, "escalation": 0, "finals": None})
        w.runner.tasks.fail = real
        w2 = self.reopen(w, [d])
        w2.now = w.now + 10_000                # dead worker's lease expired
        holder["w"] = w2
        w2.runner.run_pending()
        self.assertEqual(w2.runner.tasks.get(tid).state, "failed")
        self.assertEqual(self.counts(w2, "notes.fatal", tid)["alert"], 1)
        self.assertEqual(self.counts(w2, "notes.fatal", tid)["finals"], ("failed", True))

    def test_escalated_crash_after_commit_is_recovered_once(self):
        w = self.world(definitions=[rw.batch(n=1)])

        def slow_fail(eff):
            w.now += 31
            rw.fail_definitely(eff)

        w.ext.hooks["s1"] = ("before", slow_fail)
        w.runner.tick()
        self.assertEqual([w.runner.run_once().outcome for _ in range(2)], ["retry", "retry"])
        self.crash_at_record(w)
        with self.assertRaises(self.Crash):
            w.runner.run_once()                # third failure escalates, then crash
        tid = w.runner.tasks.by_trigger(f"resp:notes.batch:slot:{int(rw.T0 // 3600)}").id
        self.assertEqual(w.runner.tasks.get(tid).state, ESCALATED)
        w2 = self.reopen(w, [rw.batch(n=1)])
        for _ in range(2):
            w2.runner.watchdog()
            w2.runner.report_progress()
        c = self.counts(w2, "notes.batch", tid)
        self.assertEqual((c["final"], c["escalation"], c["alert"], c["finals"]),
                         (1, 1, 0, ("escalated", True)))
        self.assertEqual(w2.body(f"work:notes.batch:{tid}:final")["outcome"], "escalated")

    def test_rejected_approval_crash_after_commit_is_recovered_once(self):
        w = self.world()
        w.runner.on_event("bill.due", {"id": "deny-1", "amount": 5})
        r = w.runner.run_once()
        self.assertEqual(w.say(f"/deny {r.detail}").outcome, "denied")
        self.crash_at_record(w)
        with self.assertRaises(self.Crash):
            w.runner.poll_approvals()
        self.assertEqual(w.runner.tasks.get(r.task_id).state, "failed")
        self.assertEqual(self.counts(w, "bill.watch", r.task_id)["alert"], 0)
        w2 = self.reopen(w, [rw.batch(), rw.bill_watch()])
        w2.runner.deliver_pending_finals()
        w2.runner.deliver_pending_finals()
        c = self.counts(w2, "bill.watch", r.task_id)
        self.assertEqual((c["alert"], c["finals"]), (1, ("failed", True)))
        self.assertEqual(w2.ext.calls("pay"), 0)

    def test_escalation_inside_claim_is_backfilled(self):
        # C2 escalates a task whose lease expired three times inside claim();
        # the runner never sees that transition, the sweep does.
        w = self.world(definitions=[rw.batch(n=1)])
        w.runner.tick()
        for i in range(3):
            self.assertIsNotNone(w.runner.tasks.claim(f"dead-{i}", 1))
            w.now += 5
        w.runner.tasks.claim("dead-x", 1)        # 3rd expiry escalates the task
        tid = w.runner.tasks.by_trigger(f"resp:notes.batch:slot:{int(rw.T0 // 3600)}").id
        self.assertEqual(w.runner.tasks.get(tid).state, ESCALATED)
        w.runner.deliver_pending_finals()
        self.assertEqual(self.counts(w, "notes.batch", tid)["finals"], ("escalated", True))

    def test_silent_failure_owes_nothing_and_is_marked(self):
        d, holder = self.fatal_def(rid="notes.quietfail", delivery="silent")
        w = self.world(definitions=[d])
        holder["w"] = w
        w.runner.tick()
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "failed")
        c = self.counts(w, "notes.quietfail", r.task_id)
        self.assertEqual((c["alert"], c["final"], c["finals"]), (0, 0, ("failed", True)))

    def test_kill_keeps_a_failure_final_pending_until_cleared(self):
        d, holder = self.fatal_def()
        w = self.world(definitions=[d])
        holder["w"] = w
        w.runner.tick()
        self.crash_at_record(w)
        with self.assertRaises(self.Crash):
            w.runner.run_once()
        tid = w.runner.tasks.by_trigger(f"resp:notes.fatal:slot:{int(rw.T0 // 3600)}").id
        _set_kill(w)
        w2 = self.reopen(w, [d])
        w2.runner.deliver_pending_finals()
        self.assertEqual(self.counts(w2, "notes.fatal", tid)["finals"], ("failed", False))
        w2.notifier.kill.clear()
        w2.runner.deliver_pending_finals()
        c = self.counts(w2, "notes.fatal", tid)
        self.assertEqual((c["alert"], c["final"], c["finals"]), (1, 1, ("failed", True)))

    def test_upgrade_does_not_reannounce_failures_handled_by_old_code(self):
        out = subprocess.run([sys.executable, str(HERE / "legacy_fixture.py"), "e2_467b2b9",
                              "denied_polled", self.tmp], capture_output=True, text=True,
                             timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        tid = json.loads(out.stdout.strip().splitlines()[-1])["task_id"]
        w = rw.World(self.tmp)
        before = w.outbox_ids()
        w.runner.deliver_pending_finals()
        w.runner.watchdog()
        self.assertEqual(w.outbox_ids(), before)          # nothing re-announced
        self.assertEqual(self.counts(w, "bill.watch", tid)["finals"], ("failed", True))


class FinalTimingUsesTheTerminalCommit(Base):
    """Dolores @e33eecc: _record_terminal stored the time it ran, the backfill
    stored the task's committed updated_at, so a short run whose record was
    merely delayed past 30 s was treated as long work. Both now use C2's
    committed terminal timestamp."""
    Crash = TerminalFinalsAreDurable.Crash
    crash_at_record = TerminalFinalsAreDurable.crash_at_record
    counts = TerminalFinalsAreDurable.counts
    fatal_def = TerminalFinalsAreDurable.fatal_def
    reopen = TerminalFinalsAreDurable.reopen

    def run_failure(self, *, long, path):
        d, holder = self.fatal_def(long=long)
        w = self.world(definitions=[d])
        holder["w"] = w
        w.runner.tick()
        real = w.runner._record_terminal
        if path == "delayed":
            def delayed(*a, **k):
                w.now += 31                     # scheduler pause after the commit
                return real(*a, **k)
            w.runner._record_terminal = delayed
            w.runner.run_once()
            w2 = w
        else:                                   # crash, then the backfill sweep
            self.crash_at_record(w)
            with self.assertRaises(self.Crash):
                w.runner.run_once()
            w2 = self.reopen(w, [d])
            w2.now = w.now + 31
            holder["w"] = w2
            w2.runner.deliver_pending_finals()
        tid = w2.runner.tasks.by_trigger(f"resp:notes.fatal:slot:{int(rw.T0 // 3600)}").id
        return self.counts(w2, "notes.fatal", tid)

    def test_short_failure_owes_no_final_whether_delayed_or_backfilled(self):
        for path in ("delayed", "backfill"):
            with self.subTest(path=path):
                with tempfile.TemporaryDirectory() as t:
                    self.tmp = t
                    c = self.run_failure(long=False, path=path)
                    self.assertEqual((c["alert"], c["final"], c["finals"]),
                                     (1, 0, ("failed", True)))

    def test_long_failure_owes_a_final_whether_delayed_or_backfilled(self):
        for path in ("delayed", "backfill"):
            with self.subTest(path=path):
                with tempfile.TemporaryDirectory() as t:
                    self.tmp = t
                    c = self.run_failure(long=True, path=path)
                    self.assertEqual((c["alert"], c["final"], c["finals"]),
                                     (1, 1, ("failed", True)))

    def test_short_success_with_a_delayed_delivery_owes_no_final(self):
        w = self.world(definitions=[rw.batch(n=1)])
        real = w.runner._deliver_final

        def delayed(*a, **k):
            w.now += 31                         # pause between commit and delivery
            return real(*a, **k)

        w.runner._deliver_final = delayed
        w.runner.tick()
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "done")
        c = self.counts(w, "notes.batch", r.task_id)
        self.assertEqual((c["final"], c["finals"]), (0, ("done", True)))

    def test_long_success_control_owes_a_final(self):
        w = self.world(definitions=[rw.batch(n=1)])
        w.ext.hooks["s1"] = ("before", lambda eff: setattr(w, "now", w.now + 30))
        w.runner.tick()
        r = w.runner.run_once()
        c = self.counts(w, "notes.batch", r.task_id)
        self.assertEqual((c["final"], c["finals"]), (1, ("done", True)))


class RealC2Seams(Base):
    """E2 on the real C2 (claude/c2-task-effect-journal a4eef4d): no E2 fork
    of runtime/tasks.py. These go through C2's own submit(tx=), complete(tx=),
    release() and the in-claim kill switch."""

    def test_stop_after_the_early_check_is_refused_under_the_c2_claim_lock(self):
        # Closes the long-open window: /stop lands after E2's cheap pre-check;
        # C2's kill switch, read inside the claim transaction, refuses.
        w = self.world(definitions=[rw.batch(n=2)])
        real = w.runner._dispatch_permitted

        fired = []

        def check_then_stop():
            ok = real()
            if w.ext.calls() == 1 and not fired:   # s1 done; /stop races s2's dispatch
                fired.append(True)
                _set_kill(w)
            return ok

        w.runner._dispatch_permitted = check_then_stop
        w.runner.tick()
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "interrupted")
        self.assertEqual(w.ext.calls(), 1)                 # s2 never sent
        eff = w.journal.for_step(r.task_id, "s2")
        self.assertEqual((eff.status, eff.dispatches), ("pending", 0))
        task = w.runner.tasks.get(r.task_id)
        self.assertEqual((task.state, task.consecutive_failures), (QUEUED, 0))
        w.notifier.kill.clear()
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual(w.ext.applied(), [("s1", 1), ("s2", 1)])

    def test_stop_between_claim_and_send_is_undone_by_c2(self):
        # /stop commits in the gap between C2's claim transaction and its
        # post-claim check. The switch is the real KillSwitch reading the real
        # E1 flag; the subclass only places the /stop at that moment (its 2nd
        # read: the 1st is inside the claim). This depends on the switch: with
        # a KillSwitch that ignored the flag, perform runs and calls() == 1;
        # a journal with no switch at all is refused at assembly
        # (test_runner_refuses_journal_without_kill_switch).
        reads = []

        class StopAfterClaim(KillSwitch):
            __slots__ = ()

            def __call__(self, eff):
                reads.append(eff.status)
                if len(reads) == 2:                       # claim committed, not sent
                    _set_kill(w)
                return super().__call__(eff)

        w = self.world(definitions=[rw.batch(n=1)],
                       kill_switch=StopAfterClaim(str(Path(self.tmp) / "potter.db")))
        w.runner.tick()
        r = w.runner.run_once()
        self.assertEqual(r.outcome, "interrupted")
        self.assertEqual(reads, ["pending", "unknown"])    # in-claim, then post-claim
        self.assertEqual(w.ext.calls(), 0)                 # perform never ran
        eff = w.journal.for_step(r.task_id, "s1")
        self.assertEqual(eff.status, "pending")            # claim undone by C2
        self.assertIsNone(eff.in_flight_owner)
        w.notifier.kill.clear()
        self.assertEqual(w.runner.run_once().outcome, "done")
        self.assertEqual(w.ext.applied(), [("s1", 1)])

    def _assemble(self, w, journal):
        return ResponsibilityRunner(
            w.db, definitions=[rw.batch(n=1)], journal=journal, policy=w.engine,
            ledger=w.ledger, issuer=w.issuer, handle_for=w.notifier.handle_for,
            connectors=[Connector(rw.CONNECTOR, w.ext.perform, w.ext.reconcile)],
            worker_id="w9", clock=w.clock)

    def test_runner_refuses_journal_without_kill_switch(self):
        from runtime import EffectJournal
        w = self.world(definitions=[rw.batch(n=1)])
        for ks in (None, lambda eff: w.notifier.kill.is_set(), lambda eff: False):
            with self.assertRaisesRegex(ValueError, "kill_switch"):
                self._assemble(w, EffectJournal(w.db, clock=w.clock, kill_switch=ks))
        self._assemble(w, EffectJournal(w.db, clock=w.clock,
                                        kill_switch=kill_switch_for(w.db)))   # accepted

    def test_runner_refuses_kill_switch_bound_to_another_database(self):
        from runtime import Database, EffectJournal
        w = self.world(definitions=[rw.batch(n=1)])
        other = Database(Path(self.tmp) / "other.db")
        with self.assertRaisesRegex(ValueError, "kill_switch"):
            self._assemble(w, EffectJournal(w.db, clock=w.clock,
                                            kill_switch=kill_switch_for(other)))

    def test_journal_cannot_be_swapped_after_assembly(self):
        from runtime import EffectJournal
        w = self.world(definitions=[rw.batch(n=1)])
        with self.assertRaises(AttributeError):
            w.runner.journal = EffectJournal(w.db, clock=w.clock)

    def test_tick_waits_on_another_writer_and_commits_one_slot(self):
        import threading
        w = self.world()
        w2 = rw.World(self.tmp, worker="w2")
        holding, release = threading.Event(), threading.Event()

        def hold_write_lock():
            with w2.db.tx():                 # another process-like writer
                holding.set()
                release.wait(10)

        h = threading.Thread(target=hold_write_lock)
        h.start()
        self.assertTrue(holding.wait(10))
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("tick", w.runner.tick()))
        t.start()
        t.join(0.5)
        self.assertTrue(t.is_alive())        # blocked on the write lock, nothing half-done
        with w.db.read() as c:
            self.assertIsNone(c.execute("SELECT value FROM responsibility_state WHERE"
                                        " key='watermark:notes.batch'").fetchone())
        release.set()
        h.join(10)
        t.join(10)
        self.assertEqual(len(out["tick"]), 1)
        self.assertEqual(w.runner.tick(), [])

    def test_c2_refuses_a_caller_transaction_from_another_database(self):
        w = self.world()
        other = rw.World(Path(self.tmp) / "other" if (Path(self.tmp) / "other").mkdir() is None
                         else self.tmp)
        with other.db.tx() as c, self.assertRaises(ValueError):
            w.runner.tasks.submit("resp:x:slot:1", "notes.batch", {}, job="notes.batch", tx=c)
        self.assertIsNone(w.runner.tasks.by_trigger("resp:x:slot:1"))


class StopAfterTheLastKillRead(Base):
    """Dolores @82aa9d7: a /stop can commit after C2's last kill read and
    before or during the connector call; ``perform`` runs outside any
    transaction, so that send is NOT stopped. This is a documented residual
    window (README, ``kill_switch_for``). These pin what IS guaranteed there:
    at most one send, settled in the journal; interrupt at the next step
    boundary; nothing later sent; ambiguity reconciled, never re-dispatched."""

    def stop_after_last_read(self, n=2):
        reads = []

        class StopAfterRead(KillSwitch):
            __slots__ = ()

            def __call__(self, eff):
                ok = super().__call__(eff)              # the real flag, read first
                reads.append(eff.status)
                if len(reads) == 2:                     # C2's post-claim read for s1
                    _set_kill(w)                        # /stop lands right after it
                return ok

        w = self.world(definitions=[rw.batch(n=n)],
                       kill_switch=StopAfterRead(str(Path(self.tmp) / "potter.db")))
        return w, reads

    def assert_paused_after_one_send(self, w, r):
        self.assertEqual(r.outcome, "interrupted")
        self.assertEqual(w.ext.calls("s1"), 1)
        self.assertEqual(w.ext.calls("s2"), 0)              # nothing after it sent
        task = w.runner.tasks.get(r.task_id)
        self.assertEqual((task.state, task.consecutive_failures), (QUEUED, 0))
        self.assertIsNone(w.journal.for_step(r.task_id, "s2"))
        w.notifier.kill.clear()
        self.assertEqual(w.runner.run_pending()[-1].outcome, "done")
        self.assertEqual(w.ext.calls("s1"), 1)              # never re-dispatched
        self.assertEqual(w.ext.applied(), [("s1", 1), ("s2", 1)])
        self.assertEqual(w.journal.unresolved(), [])

    def test_stop_between_last_kill_read_and_perform_does_not_stop_that_send(self):
        w, reads = self.stop_after_last_read()
        w.runner.tick()
        r = w.runner.run_once()
        self.assertEqual(reads[:2], ["pending", "unknown"])  # in-claim, post-claim
        self.assertTrue(w.notifier.kill.is_set())
        self.assertEqual(w.journal.for_step(r.task_id, "s1").status, "succeeded")
        self.assert_paused_after_one_send(w, r)

    def test_stop_while_the_connector_runs_does_not_stop_that_send(self):
        w = self.world(definitions=[rw.batch(n=2)])
        w.ext.hooks["s1"] = ("before", lambda eff: _set_kill(w))
        w.runner.tick()
        r = w.runner.run_once()
        self.assertEqual(w.journal.for_step(r.task_id, "s1").status, "succeeded")
        self.assert_paused_after_one_send(w, r)

    def test_stop_then_ambiguous_outcome_is_reconciled_not_resent(self):
        w = self.world(definitions=[rw.batch(n=2)])

        def stop_then_lose_the_reply(eff):
            _set_kill(w)
            raise TimeoutError("reply lost after the provider applied it")
        w.ext.hooks["s1"] = ("after", stop_then_lose_the_reply)
        w.runner.tick()
        r = w.runner.run_once()
        self.assertNotEqual(r.outcome, "done")
        self.assertEqual(w.ext.calls("s1"), 1)
        self.assertEqual(w.ext.calls("s2"), 0)
        w.notifier.kill.clear()
        self.assertEqual(w.runner.run_pending()[-1].outcome, "done")
        self.assertEqual(w.ext.calls("s1"), 1)              # reconciled, not re-sent
        self.assertEqual(w.ext.applied(), [("s1", 1), ("s2", 1)])

    def restart_after_stop_mid_send(self, phase):
        RestartMidRun.spawn_and_kill(self, phase)
        w = RestartMidRun.restarted(self)
        self.assertTrue(w.notifier.kill.is_set())           # the /stop survived the kill
        self.assertIsNone(w.runner.run_once())              # nothing claimed under /stop
        self.assertEqual(w.ext.calls("s2"), 1)              # no re-dispatch under /stop
        self.assertEqual(w.ext.calls("s3"), 0)
        w.notifier.kill.clear()
        return w

    def test_restart_after_stop_mid_send_applied_reconciles_once(self):
        w = self.restart_after_stop_mid_send("stop-after")
        self.assertEqual(w.runner.run_pending()[-1].outcome, "done")
        self.assertEqual(w.ext.calls("s2"), 1)              # reconciled, never re-sent
        self.assertEqual(w.ext.applied(), [("s1", 1), ("s2", 1), ("s3", 1)])
        self.assertEqual(w.journal.unresolved(), [])

    def test_restart_after_stop_mid_send_not_applied_stays_blocked(self):
        w = self.restart_after_stop_mid_send("stop-before")
        self.assertEqual(w.runner.run_once().outcome, "blocked")
        self.assertEqual(w.ext.calls("s2"), 1)              # "not applied" != "never lands"
        self.assertEqual(w.ext.calls("s3"), 0)
        self.assertEqual(w.ext.applied("s2"), [])


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
