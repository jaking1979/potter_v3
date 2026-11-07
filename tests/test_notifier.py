"""E1 merge evidence: T19 PASS 2 release tests + approval issuance end to end.

Release tests (T19 item 8), one class each:
  DirectSendForbidden      a job sending directly fails (structural lint + handle shape)
  NonAllowlistedIgnored    non-allowlisted / unauthenticated sender ignored and logged
  TelegramDownFallback     Telegram down -> urgent arrives via fallback, routine waits
  ApprovalAfterStateChange approval after a state change is refused
  AckOnFallback            ack on fallback suppresses the primary repeat
  StopMidBatch             /stop mid-batch leaves no further side effects
  CrashAfterSend           crash after send causes no routine duplicate
Plus: issued approval verifies under C1 (allow -> consumed; replay -> deny), changed
precondition refused, 24 h expiry, forged/unsigned decision refused, single-use
response, cross-channel approval rules, long work, watchdog, C6 traces.

Offline only: every channel is a FakeChannel; keys are test constants.
"""
import contextlib
import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from identity.seed import CHRISTIE, JOSH  # noqa: E402
from notifier import (ApprovalIssuer, ChannelAuth, CommandRouter, FakeChannel,  # noqa: E402
                      HmacIssuerKey, InboundMessage, JobSpec, Notifier, Stopped)
from notifier import lint  # noqa: E402
from policy import (ALLOW, DENY, Caller, Capability, CapabilityRegistry,  # noqa: E402
                    PolicyEngine, PolicyRequest, RiskClass)
from policy._contracts import approval as c0  # noqa: E402
from runtime import (ABANDONED_UNKNOWN, ACKED, SENT, SUPPRESSED, Database,  # noqa: E402
                     JournalApprovalLedger, TaskStore)

T = 1_800_000_000
DAY = 24 * 3600
TEST_KEY = b"test-only-e1-issuer-key"          # test constant, not a secret
TG_JOSH, TG_CHRISTIE, TG_STRANGER = "tg-1001", "tg-2002", "tg-6666"
MAIL_JOSH = "josh@example.test"
R = RiskClass
CAPS = (Capability("gmail.send", R.CONSEQUENTIAL, True, False),
        Capability("calendar.update", R.REVERSIBLE_WRITE, True, True, ("etag",)))


class Crash(BaseException):
    """Simulated process death: escapes every `except Exception`."""


class World:
    def __init__(self, tmp: str):
        self.now = float(T)
        self.db = Database(Path(tmp) / "potter.db")
        self.tg = FakeChannel("telegram")
        self.email = FakeChannel("email")
        self.home = FakeChannel("potter_home")
        self.dkim_pass: set[str] = set()
        self.sessions: set[str] = set()
        self.auth = ChannelAuth(
            inbound_bindings={"telegram": {TG_JOSH: JOSH, TG_CHRISTIE: CHRISTIE},
                              "email": {MAIL_JOSH: JOSH},
                              "potter_home": {"home:josh": JOSH}},
            outbound_addresses={JOSH: {"telegram": "chat-1001", "email": MAIL_JOSH,
                                       "potter_home": "home:josh"}},
            dkim_verifier=lambda m: m.raw_ref in self.dkim_pass,
            session_verifier=lambda m: m.raw_ref in self.sessions)
        self.traces: list[str] = []
        self.notifier = self.make_notifier()
        self.issuer = ApprovalIssuer(self.db, key=HmacIssuerKey("e1-test", TEST_KEY),
                                     notifier=self.notifier, auth=self.auth,
                                     clock=lambda: self.now, trace_sink=self.traces.append)
        self.live: dict[str, c0.BoundAction] = {}     # task_id -> action recomputed now
        self.router = CommandRouter(auth=self.auth, notifier=self.notifier, issuer=self.issuer,
                                    live_action=lambda aid, tid: self.live.get(tid),
                                    clock=lambda: self.now)
        self.tasks = TaskStore(self.db, clock=lambda: self.now)
        self.ledger = JournalApprovalLedger(self.db, clock=lambda: self.now)
        self.engine = PolicyEngine(registry=CapabilityRegistry(CAPS),
                                   grants={"agent:potter": frozenset(c.name for c in CAPS)},
                                   ledger=self.ledger,
                                   authenticator=c0.HmacAuthenticator({"e1-test": TEST_KEY}),
                                   clock=lambda: int(self.now))

    def make_notifier(self, **kw):
        return Notifier(self.db, channels={"telegram": self.tg, "email": self.email,
                                           "potter_home": self.home},
                        auth=self.auth, clock=lambda: self.now, dispatch_timeout_s=20,
                        trace_sink=self.traces.append, **kw)

    # -- helpers --------------------------------------------------------------
    def tg_msg(self, text, sender=TG_JOSH, reply_to=None):
        return InboundMessage("telegram", sender, text, reply_to=reply_to)

    def mail_msg(self, text, *, dkim: bool, reply_to=None, ref="mail-1"):
        if dkim:
            self.dkim_pass.add(ref)
        return InboundMessage("email", MAIL_JOSH, text, reply_to=reply_to, raw_ref=ref)

    def job_notify(self, job, key, text, urgency="routine", kind="alert"):
        handle = self.notifier.handle_for(JobSpec(job, "josh"))
        with self.db.tx() as c:
            return handle.notify(c, key, kind=kind, text=text, urgency=urgency)

    def waiting_task(self, key="t1"):
        task, _ = self.tasks.submit(f"trigger:{key}", "send-mail")
        self.tasks.claim("w1", 600)
        self.tasks.wait_for_approval(task.id, "w1")
        return task.id

    def request(self, action, summary="Send the dinner email?"):
        self.live[action.task_id] = action
        with self.db.tx() as c:
            return self.issuer.request(c, action, summary=summary)

    def c1(self, action, record):
        return self.engine.decide(PolicyRequest(Caller("agent:potter"), action.capability,
                                                action, approval=record,
                                                live_state=dict(action.preconditions) or None,
                                                provenance=("principal_direct",)))


def mail_action(task_id, body="7pm works."):
    return c0.BoundAction(task_id, "gmail.send", "send", "alex@example.test",
                          {"subject": "Dinner", "body": body})


def cal_action(task_id, etag="v7"):
    return c0.BoundAction(task_id, "calendar.update", "move", "cal/event-1",
                          {"start": "19:00"}, {"etag": etag})


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


# ============================================================ release tests (T19 item 8)

class DirectSendForbidden(Base):
    BAD_JOB = textwrap.dedent("""
        from notifier.channels import FakeChannel
        from runtime import Outbox, OutboxMessage
        import smtplib

        def nightly(db):
            Outbox(db).relay(lambda m: None, lambda m: "succeeded", owner="job")
    """)

    def test_repository_has_no_direct_channel_use(self):
        self.assertEqual(lint.scan(ROOT), [])

    def test_a_job_sending_directly_fails_the_lint(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "jobs").mkdir()
            (Path(d) / "jobs" / "nightly.py").write_text(self.BAD_JOB)
            rules = [f.rule for f in lint.scan(d)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lint.main(["lint", d]), 1)
        self.assertTrue(any("notifier.channels" in r for r in rules), rules)
        self.assertTrue(any("from runtime" in r for r in rules), rules)
        self.assertTrue(any("smtplib" in r for r in rules), rules)
        self.assertTrue(any(".relay()" in r for r in rules), rules)
        self.assertTrue(any("Outbox()" in r for r in rules), rules)

    def test_job_handle_cannot_send_and_jobs_must_declare_a_target(self):
        handle = self.w.notifier.handle_for(JobSpec("digest", "josh"))
        for attr in ("send", "relay", "channel", "outbox", "_channels", "__dict__"):
            self.assertFalse(hasattr(handle, attr), attr)
        with self.assertRaises(AttributeError):
            handle.x = 1
        with self.assertRaises(ValueError):
            JobSpec("digest", "")              # no declared target
        with self.assertRaises(TypeError):
            self.w.notifier.handle_for("digest")
        with self.db_tx() as c, self.assertRaises(ValueError):
            handle.notify(c, "k", kind="approval", text="x")   # jobs cannot mint prompts
        # Enqueue is not a send: nothing is delivered until the notifier relays.
        self.w.job_notify("digest", "d1", "Morning digest")
        self.assertEqual(self.w.tg.delivered, [])
        self.w.notifier.run()
        self.assertEqual(self.w.tg.delivered_notifications(), ["job:digest:d1"])

    def test_silent_job_sends_nothing(self):
        handle = self.w.notifier.handle_for(JobSpec("housekeeping", "silent"))
        with self.db_tx() as c:
            self.assertIsNone(handle.notify(c, "k", kind="job_result", text="done"))
        self.w.notifier.run()
        self.assertEqual(self.w.tg.calls + self.w.email.calls, 0)

    def test_notification_rolls_back_with_the_job_transaction(self):
        handle = self.w.notifier.handle_for(JobSpec("digest", "josh"))
        with self.assertRaises(RuntimeError):
            with self.w.db.tx() as c:
                handle.notify(c, "k", kind="digest", text="x")
                raise RuntimeError("business change failed")
        self.assertIsNone(self.w.notifier.outbox.get("job:digest:k:telegram"))

    def db_tx(self):
        return self.w.db.tx()


class NonAllowlistedIgnored(Base):
    def test_non_allowlisted_and_unauthenticated_senders_are_ignored_and_logged(self):
        self.w.job_notify("digest", "d1", "hello")
        cases = [
            (self.w.tg_msg("/stop", sender=TG_STRANGER), "unknown_sender"),
            (self.w.tg_msg("/stop", sender=TG_CHRISTIE), "principal_disabled"),
            (self.w.mail_msg("/stop", dkim=False), "dkim_unverified"),
            (InboundMessage("potter_home", "home:josh", "/stop", raw_ref="no-session"),
             "session_unverified"),
            (InboundMessage("sms", "+15550000", "/stop"), "unknown_sender"),
        ]
        for msg, reason in cases:
            with self.subTest(reason=reason):
                res = self.w.router.handle(msg)
                self.assertEqual((res.outcome, res.reason), ("ignored", reason))
        self.assertFalse(self.w.notifier.kill.is_set())
        logged = self.w.router.ignored()
        self.assertEqual([r[2] for r in logged], [r for _, r in cases])
        with self.w.db.read() as c:   # text is never logged
            self.assertNotIn("/stop", json.dumps(c.execute("SELECT * FROM inbound_log").fetchall()))
        self.w.notifier.run()
        self.assertEqual(self.w.tg.delivered_notifications(), ["job:digest:d1"])
        # Josh himself is obeyed.
        self.assertEqual(self.w.router.handle(self.w.tg_msg("/stop")).outcome, "stopped")
        self.assertTrue(self.w.notifier.kill.is_set())

    def test_ignored_sender_cannot_approve(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        res = self.w.router.handle(self.w.tg_msg(f"/approve {aid}", sender=TG_STRANGER))
        self.assertEqual(res.outcome, "ignored")
        self.assertEqual(self.w.issuer.get(aid).status, "pending")

    def test_forged_sender_token_is_not_vouched(self):
        from notifier import AuthenticatedSender
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        forged = AuthenticatedSender(JOSH, "telegram", 1)
        from notifier import ApprovalRefusal
        with self.assertRaises(ApprovalRefusal) as cm:
            self.w.issuer.decide(aid, "approve", forged, mail_action(tid))
        self.assertEqual(cm.exception.code, "unauthenticated")
        with self.assertRaises(PermissionError):
            self.w.notifier.stop(forged)


class TelegramDownFallback(Base):
    def take_telegram_down(self):
        self.w.tg.up = False
        for i in range(3):
            self.w.job_notify("digest", f"r{i}", "routine", urgency="routine")
        self.w.notifier.run()
        self.assertTrue(self.w.notifier.primary_down())

    def test_urgent_arrives_via_fallback_and_routine_waits(self):
        self.take_telegram_down()
        self.w.job_notify("monitor", "fire", "Boiler alarm", urgency="urgent")
        self.w.job_notify("digest", "later", "Routine news", urgency="routine")
        self.w.notifier.run()
        self.assertEqual(self.w.email.delivered_notifications(), ["job:monitor:fire"])
        self.assertEqual(self.w.tg.delivered, [])
        # Routine messages wait: no further attempts during the outage, never failed.
        calls = self.w.tg.calls
        for _ in range(3):
            self.w.now += 5
            self.w.notifier.run()
        self.assertEqual(self.w.tg.calls, calls)
        self.assertEqual(self.w.notifier.outbox.needs_reconciliation(), [])
        self.assertNotIn("job:digest:later", self.w.email.delivered_notifications())
        # Recovery: the probe passes and the routine backlog drains on Telegram.
        self.w.tg.up = True
        self.w.now += 61
        self.w.notifier.run()
        self.assertFalse(self.w.notifier.primary_down())
        delivered = set(self.w.tg.delivered_notifications())
        self.assertTrue({"job:digest:r0", "job:digest:r1", "job:digest:r2",
                         "job:digest:later"} <= delivered, delivered)

    def test_urgent_enqueued_during_outage_goes_to_fallback_immediately(self):
        self.take_telegram_down()
        self.w.job_notify("monitor", "leak", "Water leak", urgency="urgent")
        self.assertIsNotNone(self.w.notifier.outbox.get("job:monitor:leak:email"))

    def test_watchdog_reports_backlog_older_than_30_min(self):
        self.take_telegram_down()
        self.assertEqual(self.w.notifier.watchdog(), [])
        self.w.now += 31 * 60
        stale = self.w.notifier.watchdog()
        self.assertEqual({m.notification_id for m in stale},
                         {"job:digest:r0", "job:digest:r1", "job:digest:r2"})
        self.w.notifier.watchdog()             # same bucket: one alert, not two
        self.w.notifier.run()
        alerts = [n for n in self.w.email.delivered_notifications() if n.startswith("watchdog:")]
        self.assertEqual(len(alerts), 1)


class ApprovalAfterStateChange(Base):
    def test_precondition_change_voids_the_approval(self):
        tid = self.w.waiting_task()
        aid = self.w.request(cal_action(tid, etag="v7"))
        self.w.live[tid] = cal_action(tid, etag="v8")          # the event changed
        res = self.w.router.handle(self.w.tg_msg(f"/approve {aid}"))
        self.assertEqual((res.outcome, res.reason), ("refused", "state_changed"))
        self.assertEqual(self.w.issuer.get(aid).status, "voided")
        # Even if live state goes back, the voided request stays dead.
        self.w.live[tid] = cal_action(tid, etag="v7")
        self.assertEqual(self.w.router.handle(self.w.tg_msg(f"/approve {aid}")).reason,
                         "state_changed")

    def test_parameter_change_voids_the_approval(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid, body="7pm works."))
        self.w.live[tid] = mail_action(tid, body="8pm works.")
        self.assertEqual(self.w.router.handle(self.w.tg_msg(f"/approve {aid}")).reason,
                         "state_changed")

    def test_task_state_change_voids_the_approval(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        self.w.tasks.approval_resolved(tid, False, error="superseded")   # task moved on
        self.assertEqual(self.w.router.handle(self.w.tg_msg(f"/approve {aid}")).reason,
                         "state_changed")

    def test_state_changed_notice_voids_pending_requests(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        self.assertEqual(self.w.issuer.state_changed(tid), [aid])
        self.assertEqual(self.w.router.handle(self.w.tg_msg(f"/approve {aid}")).reason,
                         "state_changed")

    def test_live_state_unavailable_fails_closed(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        del self.w.live[tid]
        self.assertEqual(self.w.router.handle(self.w.tg_msg(f"/approve {aid}")).reason,
                         "state_changed")

    def test_state_change_after_issue_is_refused_by_c1(self):
        tid = self.w.waiting_task()
        aid = self.w.request(cal_action(tid, etag="v7"))
        record = self.w.router.handle(self.w.tg_msg(f"/approve {aid}")).detail
        d = self.w.c1(cal_action(tid, etag="v8"), record)      # state moved after approval
        self.assertEqual((d.outcome, d.reason_code), (DENY, "approval_action_mismatch"))


class AckOnFallback(Base):
    def test_ack_on_fallback_suppresses_the_primary_repeat(self):
        self.w.tg.up = False
        for i in range(3):
            self.w.job_notify("digest", f"r{i}", "routine")
        self.w.notifier.run()
        self.w.job_notify("monitor", "fire", "Boiler alarm", urgency="urgent")
        self.w.notifier.run()
        self.assertEqual(self.w.email.delivered_notifications(), ["job:monitor:fire"])
        res = self.w.router.handle(self.w.mail_msg("/ack", dkim=True, reply_to="job:monitor:fire"))
        self.assertEqual(res.outcome, "acked")
        self.assertEqual(self.w.notifier.outbox.get("job:monitor:fire:email").status, ACKED)
        self.assertEqual(self.w.notifier.outbox.get("job:monitor:fire:telegram").status,
                         SUPPRESSED)
        self.w.tg.up = True
        self.w.now += 61
        self.w.notifier.run()
        self.assertNotIn("job:monitor:fire", self.w.tg.delivered_notifications())
        self.assertIn("job:digest:r0", self.w.tg.delivered_notifications())

    def test_unverified_email_ack_is_ignored(self):
        self.w.job_notify("monitor", "fire", "Boiler alarm", urgency="urgent")
        res = self.w.router.handle(self.w.mail_msg("/ack job:monitor:fire", dkim=False))
        self.assertEqual(res.outcome, "ignored")
        self.w.notifier.run()
        self.assertEqual(self.w.tg.delivered_notifications(), ["job:monitor:fire"])


class StopMidBatch(Base):
    def test_stop_mid_batch_leaves_no_further_side_effects(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        for i in range(5):
            self.w.job_notify("digest", f"b{i}", f"item {i}")
        stop_results = []

        def stop_during_second_send(msg):
            if len(self.w.tg.delivered) == 2 and not stop_results:   # 3rd send is in flight
                stop_results.append(self.w.router.handle(self.w.tg_msg("/stop")))
        self.w.tg.on_send = stop_during_second_send
        self.w.notifier.run()

        (res,) = stop_results
        self.assertEqual(res.outcome, "stopped")
        delivered = self.w.tg.delivered_notifications()
        # Prompt + b0 before /stop, b1 in flight completes, then only the /stop reply.
        self.assertEqual(len(delivered), 4, delivered)
        self.assertTrue(delivered[-1].startswith("stop:"))
        reply = self.w.tg.delivered[-1].body
        self.assertEqual(reply["kind"], "stop_ack")
        self.assertEqual(reply["cancelled_approvals"], [aid])
        cancelled = set(reply["cancelled_notifications"])
        # The in-flight one (completed, but nothing follows it) + the three never sent.
        self.assertEqual(cancelled, {delivered[2], "job:digest:b2", "job:digest:b3",
                                     "job:digest:b4"})
        self.assertEqual(cancelled & set(delivered[:3]), {delivered[2]})
        in_flight = self.w.notifier.outbox.get(f"{delivered[2]}:telegram")
        self.assertEqual(in_flight.status, SUPPRESSED)
        self.assertIsNotNone(in_flight.sent_at)      # completed, recorded, no retry
        self.assertEqual(self.w.issuer.get(aid).status, "cancelled")

        # Nothing follows: more runs, later times, new work all produce no effect.
        calls = (self.w.tg.calls, self.w.email.calls)
        self.w.now += 3600
        self.w.notifier.run()
        with self.assertRaises(Stopped):
            self.w.job_notify("digest", "after", "late")
        with self.assertRaises(Stopped):
            self.w.request(mail_action(self.w.waiting_task("t2")))
        self.assertEqual(self.w.router.handle(self.w.tg_msg(f"/approve {aid}")).reason,
                         "stopped")
        self.w.notifier.run()
        self.assertEqual((self.w.tg.calls, self.w.email.calls), calls)
        self.assertEqual(self.w.notifier.watchdog(), [])

    def test_stop_cancels_unknown_urgent_so_it_is_not_resent(self):
        self.w.tg.crash_after_delivery = Crash()
        self.w.job_notify("monitor", "fire", "alarm", urgency="urgent")
        with self.assertRaises(Crash):
            self.w.notifier.run()
        self.w.router.handle(self.w.tg_msg("/stop"))
        self.w.now += 3600
        self.w.notifier.run()
        self.assertEqual(self.w.tg.delivered_notifications().count("job:monitor:fire"), 1)


class CrashAfterSend(Base):
    def test_crash_after_send_causes_no_routine_duplicate(self):
        self.w.tg.crash_after_delivery = Crash()
        self.w.job_notify("digest", "d1", "Morning digest")
        with self.assertRaises(Crash):
            self.w.notifier.run()
        # Restart: fresh objects on the same file, after the in-flight window.
        self.w.now += 60
        restarted = self.w.make_notifier()
        report = restarted.run()
        self.assertEqual(self.w.tg.delivered_notifications(), ["job:digest:d1"])
        self.assertEqual(report.abandoned_unknown, ["job:digest:d1:telegram"])
        self.assertEqual(restarted.outbox.get("job:digest:d1:telegram").status,
                         ABANDONED_UNKNOWN)

    def test_ambiguous_routine_send_is_not_resent(self):
        self.w.tg.ambiguous_after_delivery = True
        self.w.job_notify("digest", "d1", "Morning digest")
        self.w.notifier.run()
        self.w.now += 3600
        self.w.notifier.run()
        self.assertEqual(self.w.tg.delivered_notifications(), ["job:digest:d1"])

    def test_urgent_unknown_is_resent_exactly_once(self):
        self.w.tg.ambiguous_after_delivery = True
        self.w.job_notify("monitor", "fire", "alarm", urgency="urgent")
        self.w.notifier.run()
        self.assertEqual(self.w.tg.delivered_notifications(), ["job:monitor:fire"] * 2)

    def test_notification_id_is_written_before_send(self):
        seen = []
        self.w.tg.on_send = lambda m: seen.append(self.w.notifier.outbox.get(m.message_id).status)
        self.w.job_notify("digest", "d1", "x")
        self.w.notifier.run()
        self.assertEqual(seen, ["unknown"])     # durable row, dispatch committed pre-send
        self.assertEqual(self.w.notifier.outbox.get("job:digest:d1:telegram").status, SENT)


# ============================================================ approvals end to end

class ApprovalEndToEnd(Base):
    def test_issued_approval_verifies_under_c1_then_replay_denied(self):
        tid = self.w.waiting_task()
        action = mail_action(tid)
        self.assertEqual(self.w.c1(action, None).reason_code, "approval_required")
        aid = self.w.request(action)
        self.w.notifier.run()
        (prompt,) = self.w.tg.delivered
        self.assertEqual(prompt.body["approval_id"], aid)
        self.assertNotIn("7pm", json.dumps(prompt.body))       # parameters never in the prompt
        res = self.w.router.handle(self.w.tg_msg("/approve", reply_to=f"approval:{aid}"))
        self.assertEqual(res.outcome, "approved")
        record = res.detail
        d = self.w.c1(action, record)
        self.assertEqual((d.outcome, d.reason_code), (ALLOW, "ok_approved"))
        self.assertEqual(self.w.ledger.pending(record["nonce"]).action(), action)
        replay = self.w.c1(action, record)
        self.assertEqual((replay.outcome, replay.reason_code), (DENY, "approval_replayed"))

    def test_response_is_single_use(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        self.assertEqual(self.w.router.handle(self.w.tg_msg(f"/approve {aid}")).outcome,
                         "approved")
        for text in (f"/approve {aid}", f"/deny {aid}"):
            res = self.w.router.handle(self.w.tg_msg(text))
            self.assertEqual((res.outcome, res.reason), ("refused", "already_decided"))

    def test_deny_issues_a_signed_deny_that_c1_refuses(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        record = self.w.router.handle(self.w.tg_msg(f"/deny {aid}")).detail
        self.assertEqual(record["decision"], "deny")
        self.assertEqual(self.w.c1(mail_action(tid), record).reason_code, "approval_denied")

    def test_changed_precondition_refused(self):
        tid = self.w.waiting_task()
        aid = self.w.request(cal_action(tid, etag="v7"))
        self.w.live[tid] = cal_action(tid, etag="v9")
        res = self.w.router.handle(self.w.tg_msg(f"/approve {aid}"))
        self.assertEqual(res.reason, "state_changed")
        # And an approval for v7 cannot drive the action computed against v9 in C1.
        tid2 = self.w.waiting_task("t2")
        aid2 = self.w.request(cal_action(tid2, etag="v7"))
        rec = self.w.router.handle(self.w.tg_msg(f"/approve {aid2}")).detail
        self.assertEqual(self.w.c1(cal_action(tid2, etag="v9"), rec).outcome, DENY)
        self.assertEqual(self.w.c1(cal_action(tid2, etag="v7"), rec).outcome, ALLOW)

    def test_24h_expiry(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        self.w.now = T + DAY
        res = self.w.router.handle(self.w.tg_msg(f"/approve {aid}"))
        self.assertEqual((res.outcome, res.reason), ("refused", "expired"))
        self.assertEqual(self.w.issuer.get(aid).status, "expired")

    def test_approval_window_never_exceeds_24h_from_request(self):
        tid = self.w.waiting_task()
        action = mail_action(tid)
        aid = self.w.request(action)
        self.w.now = T + DAY - 60                          # approved just before the deadline
        record = self.w.router.handle(self.w.tg_msg(f"/approve {aid}")).detail
        self.assertEqual(record["expires_at"], T + DAY)
        self.w.now = T + DAY                               # C1 at the deadline
        d = self.w.c1(action, record)
        self.assertEqual((d.outcome, d.reason_code), (DENY, "approval_window"))

    def test_forged_or_unsigned_decision_refused(self):
        tid = self.w.waiting_task()
        action = mail_action(tid)
        aid = self.w.request(action)
        genuine = self.w.router.handle(self.w.tg_msg(f"/approve {aid}")).detail
        attacker = c0.HmacAuthenticator({"e1-test": b"attacker-key"})
        forged = dict(genuine, nonce="b" * 32)
        forged["issuer_signature"] = attacker.sign("e1-test", c0.signing_payload(forged))
        unsigned = dict(genuine, nonce="c" * 32, issuer_signature="")
        tampered = dict(genuine, decision="approve", channel="potter_home")   # sig no longer matches
        client_minted = {k: v for k, v in genuine.items() if k != "issuer_signature"}
        for name, rec, code in (("forged", forged, "approval_unauthenticated"),
                                ("unsigned", unsigned, "approval_malformed"),
                                ("tampered", tampered, "approval_unauthenticated"),
                                ("client-minted", client_minted, "approval_malformed")):
            with self.subTest(name):
                d = self.w.c1(action, rec)
                self.assertEqual((d.outcome, d.reason_code), (DENY, code))
        self.assertEqual(self.w.c1(action, genuine).outcome, ALLOW)   # untouched by the above

    def test_reply_on_another_channel_counts_only_if_it_authenticates_josh(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        # Email without DKIM: ignored.
        self.assertEqual(self.w.router.handle(
            self.w.mail_msg(f"/approve {aid}", dkim=False)).outcome, "ignored")
        # Email with DKIM authenticates Josh, but C0 v0 has no email approval channel.
        res = self.w.router.handle(self.w.mail_msg(f"/approve {aid}", dkim=True, ref="m2"))
        self.assertEqual((res.outcome, res.reason), ("refused", "channel_not_approval_capable"))
        # Potter Home with a verified session counts.
        self.w.sessions.add("sess-1")
        res = self.w.router.handle(InboundMessage("potter_home", "home:josh", f"/approve {aid}",
                                                  raw_ref="sess-1"))
        self.assertEqual(res.outcome, "approved")
        self.assertEqual(res.detail["channel"], "potter_home")
        self.assertEqual(self.w.c1(mail_action(tid), res.detail).outcome, ALLOW)

    def test_browser_approval_carries_screenshot(self):
        with self.w.db.tx() as c, self.assertRaises(ValueError):
            self.w.issuer.request(c, c0.BoundAction("t", "browser", "click", "x"), summary="s")

    def test_traces_for_issue_decide_send(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        self.w.notifier.run()
        self.w.router.handle(self.w.tg_msg(f"/approve {aid}"))
        recs = [json.loads(x) for x in self.w.traces]
        kinds = [(r["potter.component"], r["potter.outcome"]) for r in recs]
        self.assertEqual(kinds, [("task", "accepted"), ("effect", "completed"),
                                 ("task", "accepted")])
        self.assertEqual(recs[0]["task.id"], tid)
        blob = "".join(self.w.traces)
        for private in ("7pm", "alex@example.test", aid, "Dinner", TG_JOSH):
            self.assertNotIn(private, blob)

    def test_trace_failure_fails_closed(self):
        tid = self.w.waiting_task()
        self.w.issuer._sink = lambda line: (_ for _ in ()).throw(OSError("sink down"))
        with self.assertRaises(OSError):
            self.w.request(mail_action(tid))
        with self.w.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0], 0)
        self.assertIsNone(self.w.notifier.outbox.get("approval:x:telegram"))


# ============================================================ review regressions (Codex, #28)

class VoucherForgery(Base):
    """P1: a constructed or relabelled voucher must never pass."""

    def test_codex_repro_dkim_email_then_constructed_telegram_voucher_is_refused(self):
        from dataclasses import replace
        from notifier import AuthenticatedSender, ApprovalRefusal
        tid = self.w.waiting_task()
        action = mail_action(tid)
        aid = self.w.request(action)
        # Authenticate one DKIM email so the instance has minted a voucher (serial 1).
        email_sender = self.w.auth.authenticate(self.w.mail_msg("hi", dkim=True)).sender
        self.assertEqual(email_sender.channel, "email")
        lookalikes = [
            AuthenticatedSender(JOSH, "telegram", 1),
            AuthenticatedSender(JOSH, "telegram", 1, email_sender._token),
            replace(email_sender, channel="telegram"),
        ]
        for forged in lookalikes:
            with self.subTest(forged=forged):
                self.assertFalse(self.w.auth.vouches(forged))
                with self.assertRaises(ApprovalRefusal) as cm:
                    self.w.issuer.decide(aid, "approve", forged, action)
                self.assertEqual(cm.exception.code, "unauthenticated")
        self.assertEqual(self.w.issuer.get(aid).status, "pending")
        # The genuine email voucher is still refused for approvals (C0 has no email channel).
        with self.assertRaises(ApprovalRefusal) as cm:
            self.w.issuer.decide(aid, "approve", email_sender, action)
        self.assertEqual(cm.exception.code, "channel_not_approval_capable")
        self.assertEqual(self.w.issuer.get(aid).status, "pending")

    def test_mutated_voucher_is_refused(self):
        sender = self.w.auth.authenticate(self.w.tg_msg("/ack")).sender
        self.assertTrue(self.w.auth.vouches(sender))
        object.__setattr__(sender, "channel", "potter_home")
        self.assertFalse(self.w.auth.vouches(sender))

    def test_voucher_drives_at_most_one_approval(self):
        from notifier import ApprovalRefusal
        t1, t2 = self.w.waiting_task("t1"), self.w.waiting_task("t2")
        a1, a2 = self.w.request(mail_action(t1)), self.w.request(mail_action(t2))
        sender = self.w.auth.authenticate(self.w.tg_msg("/approve")).sender
        self.w.issuer.decide(a1, "approve", sender, mail_action(t1))
        with self.assertRaises(ApprovalRefusal) as cm:
            self.w.issuer.decide(a2, "approve", sender, mail_action(t2))
        self.assertEqual(cm.exception.code, "unauthenticated")
        self.assertFalse(self.w.auth.vouches(sender))
        self.assertEqual(self.w.issuer.get(a2).status, "pending")

    def test_voucher_from_another_auth_instance_is_refused(self):
        from notifier import ChannelAuth as CA
        stranger_auth = CA(inbound_bindings={"telegram": {TG_JOSH: JOSH}},
                           outbound_addresses={JOSH: {"telegram": "chat-1001"}})
        foreign = stranger_auth.authenticate(self.w.tg_msg("/stop")).sender
        self.assertFalse(self.w.auth.vouches(foreign))
        with self.assertRaises(PermissionError):
            self.w.notifier.stop(foreign)


class WatchdogSameBucket(Base):
    """P2: a backlog that changes within one 30-min bucket must not crash."""

    def test_two_messages_going_stale_in_one_bucket(self):
        self.w.tg.up = False
        for i in range(3):
            self.w.job_notify("digest", f"r{i}", "routine")
        self.w.notifier.run()
        self.w.now = (int(T // 1800) + 1) * 1800 + 60      # early in a fresh bucket
        self.w.job_notify("digest", "late1", "routine")
        self.w.now += 31 * 60 - 120                        # r* stale; late1 not yet
        first = self.w.notifier.watchdog()
        self.w.job_notify("digest", "late2", "routine")
        self.w.now += 100                                  # same bucket; late1 now stale too
        second = self.w.notifier.watchdog()               # must not raise OutboxConflict
        self.assertGreater(len(second), len(first))
        alerts = [m for m in (self.w.notifier.outbox.get(f"watchdog:backlog:{b}:email")
                              for b in range(int(T // 1800), int(self.w.now // 1800) + 1))
                  if m is not None]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(len(alerts[0].body["stale_messages"]), len(first))   # original content


class LongWorkRestart(Base):
    """P2: update numbering survives a restart without reusing an id."""

    def test_restart_continues_numbering(self):
        handle = self.w.notifier.handle_for(JobSpec("research", "josh"))
        work = handle.long_work("w1", T)
        work.tick("searching", T + 31)                     # ack
        work.tick("drafting", T + 40)                      # state:1
        # Restart: new notifier + new LongWork on the same file.
        restarted = self.w.make_notifier().handle_for(JobSpec("research", "josh"))
        work2 = restarted.long_work("w1", T)
        work2.tick("drafting", T + 50)                     # replayed tick: nothing
        work2.tick("reviewing", T + 60)                    # state:2, not a reused state:1
        work2.finish("receipt://effects/r", T + 70)
        self.w.now = T + 70
        self.w.notifier.run()
        got = [(m.notification_id.split(":", 3)[-1], m.body.get("state"))
               for m in self.w.tg.delivered]
        self.assertEqual(got, [("ack", "searching"), ("state:1", "drafting"),
                               ("state:2", "reviewing"), ("final", None)])
        with self.assertRaises(Exception):
            restarted.long_work("w1", T).tick("again", T + 80)   # finished stays finished


class StopAckFallback(Base):
    """P2: with Telegram down after /stop, the confirmation falls back to email."""

    def test_stop_ack_reaches_email_when_telegram_is_down(self):
        tid = self.w.waiting_task()
        self.w.request(mail_action(tid))
        self.w.job_notify("digest", "d1", "routine")
        res = self.w.router.handle(self.w.tg_msg("/stop"))
        self.assertEqual(res.outcome, "stopped")
        self.w.tg.up = False
        for _ in range(12):
            self.w.notifier.run()
            if any(m.body.get("kind") == "stop_ack" for m in self.w.email.delivered):
                break
            self.w.now += 60
        kinds = [m.body["kind"] for m in self.w.email.delivered]
        self.assertEqual(kinds, ["stop_ack"])              # only the confirmation, nothing else
        self.assertEqual(self.w.email.delivered[0].notification_id, res.detail.reply_notification_id)
        self.assertEqual(self.w.tg.delivered, [])


# ============================================================ long work (item 5)

class LongWorkUpdates(Base):
    def test_ack_after_30s_updates_on_state_change_final_with_receipt(self):
        work = self.w.notifier.handle_for(JobSpec("research", "josh")).long_work("w1", T)
        work.tick("searching", T + 5)
        work.tick("searching", T + 29)
        self.w.notifier.run()
        self.assertEqual(self.w.tg.delivered, [])                 # under 30 s: silent
        work.tick("searching", T + 31)
        work.tick("searching", T + 40)                            # no change: nothing
        work.tick("drafting", T + 50)
        work.tick("drafting", T + 70)
        work.finish("receipt://effects/abc", T + 90)
        self.w.now = T + 90
        self.w.notifier.run()
        bodies = [m.body for m in self.w.tg.delivered]
        self.assertEqual([b["kind"] for b in bodies], ["work_ack", "work_update", "work_final"])
        self.assertEqual(bodies[1]["state"], "drafting")
        self.assertEqual(bodies[2]["receipt"], "receipt://effects/abc")

    def test_short_work_sends_only_final(self):
        work = self.w.notifier.handle_for(JobSpec("quick", "josh")).long_work("w1", T)
        work.tick("a", T + 1)
        work.tick("b", T + 2)
        work.finish("receipt://effects/q", T + 3)
        self.w.now = T + 3
        self.w.notifier.run()
        self.assertEqual([m.body["kind"] for m in self.w.tg.delivered], ["work_final"])


if __name__ == "__main__":
    unittest.main()
