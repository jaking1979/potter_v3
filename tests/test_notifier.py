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
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from identity.seed import CHRISTIE, JOSH  # noqa: E402
from notifier import (ApprovalIssuer, ChannelAuth, CommandRouter, FakeChannel,  # noqa: E402
                      HmacIssuerKey, InboundMessage, JobSpec, Notifier, Stopped,
                      TransportSigner)
from notifier import lint  # noqa: E402
from policy import (ALLOW, DENY, Caller, Capability, CapabilityRegistry,  # noqa: E402
                    PolicyEngine, PolicyRequest, RiskClass)
from policy._contracts import approval as c0  # noqa: E402
from runtime import (ABANDONED_UNKNOWN, ACKED, SENT, SUPPRESSED, Database,  # noqa: E402
                     JournalApprovalLedger, TaskStore)

T = 1_800_000_000
DAY = 24 * 3600
TEST_KEY = b"test-only-e1-issuer-key"          # test constant, not a secret
TRANSPORT_KEYS = {"telegram": b"test-only-telegram-adapter-key",   # test constants
                  "email": b"test-only-email-adapter-key-0",
                  "potter_home": b"test-only-home-adapter-key-00"}
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
        self.signers = {ch: TransportSigner(ch, k) for ch, k in TRANSPORT_KEYS.items()}
        self._updates = 0
        self.auth = ChannelAuth(
            db=self.db, transport_keys=TRANSPORT_KEYS, clock=lambda: self.now,
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
    def signed(self, channel, sender, text, reply_to=None, raw_ref=None):
        """What the trusted (fake) adapter hands the router: a signed update."""
        self._updates += 1
        return self.signers[channel].envelope(sender, text, update_id=f"u{self._updates}",
                                              sent_at=self.now, reply_to=reply_to,
                                              raw_ref=raw_ref)

    def tg_msg(self, text, sender=TG_JOSH, reply_to=None):
        return self.signed("telegram", sender, text, reply_to)

    def mail_msg(self, text, *, dkim: bool, reply_to=None, ref="mail-1"):
        if dkim:
            self.dkim_pass.add(ref)
        return self.signed("email", MAIL_JOSH, text, reply_to, ref)

    def home_msg(self, text, *, session: str):
        return self.signed("potter_home", "home:josh", text, raw_ref=session)

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
            (self.w.home_msg("/stop", session="no-session"), "session_unverified"),
            (InboundMessage("sms", "+15550000", "/stop", update_id="x", sent_at=T),
             "unknown_channel"),
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
        res = self.w.router.handle(self.w.home_msg(f"/approve {aid}", session="sess-1"))
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
        sender = self.w.auth.authenticate(self.w.tg_msg(f"/approve {a1}")).sender
        self.w.issuer.decide(a1, "approve", sender, mail_action(t1))
        with self.assertRaises(ApprovalRefusal) as cm:
            self.w.issuer.decide(a2, "approve", sender, mail_action(t2))
        self.assertEqual(cm.exception.code, "unauthenticated")
        self.assertFalse(self.w.auth.vouches(sender))
        self.assertEqual(self.w.issuer.get(a2).status, "pending")

    def test_voucher_from_another_auth_instance_is_refused(self):
        from notifier import ChannelAuth as CA
        stranger_auth = CA(db=self.w.db, transport_keys=TRANSPORT_KEYS,
                           clock=lambda: self.w.now,
                           inbound_bindings={"telegram": {TG_JOSH: JOSH}},
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


# ============================================================ review regressions (Dolores, #28)

class HandleIsolation(Base):
    """A job holding a handle cannot reach a channel (Dolores finding 1)."""
    DOLORES = "handle._n._channels['telegram'].send(msg)"

    def _queued_msg(self):
        self.w.job_notify("digest", "d1", "x")
        return self.w.notifier.outbox.get("job:digest:d1:telegram")

    def test_dolores_snippet_is_impossible_and_flagged(self):
        handle = self.w.notifier.handle_for(JobSpec("digest", "josh"))
        msg = self._queued_msg()
        with self.assertRaises(AttributeError):
            exec(self.DOLORES, {"handle": handle, "msg": msg})
        work = handle.long_work("w1", T)
        with self.assertRaises(AttributeError):
            exec(self.DOLORES.replace("handle", "work"), {"work": work, "msg": msg})
        self.assertEqual(self.w.tg.calls, 0)
        rules = [f.rule for f in lint.scan_source(self.DOLORES, "jobs/x.py", exempt=False)]
        self.assertIn("direct send: .send()", rules)
        self.assertIn("private reach-through: ._n", rules)
        self.assertIn("private reach-through: ._channels", rules)

    def test_no_notifier_or_channel_reachable_from_handle_data(self):
        import gc
        import types
        from notifier import ChannelAuth as CA, Notifier as N
        from runtime import Outbox
        handle = self.w.notifier.handle_for(JobSpec("digest", "josh"))
        forbidden = (N, FakeChannel, CA, Outbox)
        skip = (type, types.FunctionType, types.ModuleType, types.BuiltinFunctionType)
        for root in (handle, handle.long_work("w1", T)):
            seen, frontier = set(), [root]
            for _ in range(5):                 # instance data, 5 hops deep
                nxt = []
                for obj in frontier:
                    for ref in gc.get_referents(obj):
                        if id(ref) in seen or isinstance(ref, skip):
                            continue
                        seen.add(id(ref))
                        self.assertNotIsInstance(ref, forbidden, f"{type(root).__name__} leaks")
                        nxt.append(ref)
                frontier = nxt
        self.assertFalse(hasattr(handle, "_n"))
        with self.assertRaises(TypeError):
            import pickle
            pickle.dumps(handle)

    def test_lint_flags_other_reach_through_paths(self):
        cases = {
            "getattr(handle, '_n')": "dynamic getattr()",
            "getattr(handle, name)": "dynamic getattr()",
            "vars(handle)": "vars()",
            "type(handle).notify.__globals__['_HANDLES']": ".__globals__",
            "import gc\ngc.get_referents(handle)": "introspection import: gc",
            "from inspect import getclosurevars": "introspection import: inspect",
            "handle.notify.__func__.__closure__": ".__closure__",
            "from notifier.service import _resolve": "direct channel path",
        }
        for src, needle in cases.items():
            with self.subTest(src=src):
                rules = [f.rule for f in lint.scan_source(src, "jobs/x.py", exempt=False)]
                self.assertTrue(any(needle in r for r in rules), rules)
        ok = "handle.notify(tx, 'k', kind='alert', text='t')\nself._auth = 1"
        self.assertEqual(lint.scan_source(ok, "jobs/x.py", exempt=False), [])


class StopSendRace(Base):
    """Dolores finding 2: no send may start after stop() has returned."""

    def setUp(self):
        super().setUp()
        import threading
        self.threading = threading
        self.paused, self.resume = threading.Event(), threading.Event()

    def _pause_once(self, msg):
        if msg.notification_id == "job:digest:d1" and not self.paused.is_set():
            self.paused.set()
            self.assertTrue(self.resume.wait(10))

    def _run_in_thread(self):
        errors = []

        def target():
            try:
                self.w.notifier.run()
            except BaseException as e:      # surfaced by the test
                errors.append(e)
        t = self.threading.Thread(target=target)
        t.start()
        self.assertTrue(self.paused.wait(10))
        return t, errors

    def test_paused_after_check_then_stop_returns_then_resume_no_send(self):
        # Pause where the old code had already passed its kill check.
        self.w.notifier._hook_before_gate = self._pause_once
        self.w.job_notify("digest", "d1", "x")
        worker, errors = self._run_in_thread()
        res = self.w.router.handle(self.w.tg_msg("/stop"))      # returns while paused
        self.assertEqual(res.outcome, "stopped")
        self.assertEqual(res.detail.unconfirmed_in_flight, ())
        self.resume.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertNotIn("job:digest:d1", self.w.tg.delivered_notifications())
        self.assertEqual([m.body["kind"] for m in self.w.tg.delivered], ["stop_ack"])

    def test_sender_past_gate_aborts_and_stop_waits_for_it(self):
        # Past the gate but not yet inside channel.send: stop() must wait for the
        # sender to resolve, and the sender's recheck sees the flag and aborts.
        self.w.notifier._hook_after_gate = self._pause_once
        self.w.job_notify("digest", "d1", "x")
        self.w.job_notify("digest", "d2", "y")
        worker, errors = self._run_in_thread()
        order = []
        self.w.tg.on_send = lambda m: order.append(("send", m.notification_id))

        def do_stop():
            self.stop_res = self.w.router.handle(self.w.tg_msg("/stop"))
            order.append(("stop_returned", None))
        stopper = self.threading.Thread(target=do_stop)
        stopper.start()
        stopper.join(0.3)
        self.assertTrue(stopper.is_alive(), "stop() must wait for the sender past the gate")
        self.resume.set()
        stopper.join(10)
        worker.join(10)
        self.assertEqual(errors, [])
        self.assertTrue(self.stop_res.detail.all_clear)
        events = [e for e in order if not (e[0] == "send" and e[1].startswith("stop:"))]
        self.assertEqual(events, [("stop_returned", None)])     # d1 aborted, d2 never sent
        self.assertIn("stop_ack", [m.body["kind"] for m in self.w.tg.delivered])
        with self.w.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM notifier_sending").fetchone()[0], 0)

    def test_send_inside_channel_completes_before_stop_returns(self):
        # Already inside channel.send: it may complete, and does so before stop() returns.
        entered, release = self.threading.Event(), self.threading.Event()

        def inside(msg):
            if msg.notification_id == "job:digest:d1":
                entered.set()
                self.assertTrue(release.wait(10))
        self.w.tg.on_send = inside
        self.w.job_notify("digest", "d1", "x")
        worker = self.threading.Thread(target=self.w.notifier.run)
        worker.start()
        self.assertTrue(entered.wait(10))
        done = []
        stopper = self.threading.Thread(
            target=lambda: done.append(self.w.router.handle(self.w.tg_msg("/stop"))))
        stopper.start()
        stopper.join(0.3)
        self.assertTrue(stopper.is_alive())
        release.set()
        stopper.join(10)
        worker.join(10)
        self.assertIn("job:digest:d1", self.w.tg.delivered_notifications())
        self.assertTrue(done[0].detail.all_clear)

    def test_unreturned_row_is_always_reported_never_aged_out(self):
        with self.w.db.tx() as c:
            c.execute("INSERT INTO notifier_sending (message_id, dispatch, owner, since, status)"
                      " VALUES ('ghost:telegram', 1, 'dead', ?, 'dispatching')",
                      (self.w.now - 10,))
        quick = self.w.make_notifier(stop_wait_s=0.05)
        # Bounded wait expires on a live-looking row: returned as unconfirmed, not all-clear.
        report = quick.stop(self.w.auth.authenticate(self.w.tg_msg("/stop")).sender)
        self.assertEqual(report.unconfirmed_in_flight, ("ghost:telegram",))
        self.assertEqual(report.expired_in_flight, ())
        self.assertFalse(report.all_clear)
        # Aged far past the dispatch timeout: still reported (flagged expired), not dropped.
        with self.w.db.tx() as c:
            c.execute("UPDATE notifier_sending SET since=?", (self.w.now - 3600,))
        report = quick.stop(self.w.auth.authenticate(self.w.tg_msg("/stop")).sender)
        self.assertEqual(report.unconfirmed_in_flight, ("ghost:telegram",))
        self.assertEqual(report.expired_in_flight, ("ghost:telegram",))
        self.assertFalse(report.all_clear)

    def test_dolores_aged_live_sender_cannot_deliver_after_stop(self):
        # Pause after the gate, before the first channel.send; /stop at T+21 with
        # dispatch_timeout 20; resume: the sender must abort, not deliver.
        self.w.notifier._hook_after_gate = self._pause_once
        self.w.job_notify("digest", "d1", "x")
        worker, errors = self._run_in_thread()
        self.w.now = T + 21
        res = self.w.router.handle(self.w.tg_msg("/stop"))
        self.assertEqual(res.outcome, "stopped")
        self.assertEqual(res.detail.unconfirmed_in_flight, ("job:digest:d1:telegram",))
        self.assertEqual(res.detail.expired_in_flight, ("job:digest:d1:telegram",))
        self.assertFalse(res.detail.all_clear)
        self.resume.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertNotIn("job:digest:d1", self.w.tg.delivered_notifications())
        with self.w.db.read() as c:   # the aborting sender cleared its own row
            self.assertEqual(c.execute("SELECT COUNT(*) FROM notifier_sending").fetchone()[0], 0)

    def test_bounded_wait_expiry_returns_unconfirmed_and_late_sender_aborts(self):
        self.w.notifier = quick = self.w.make_notifier(stop_wait_s=0.1)
        self.w.router = CommandRouter(auth=self.w.auth, notifier=quick, issuer=self.w.issuer,
                                      live_action=lambda aid, tid: None, clock=lambda: self.w.now)
        quick._hook_after_gate = self._pause_once
        self.w.job_notify("digest", "d1", "x")
        worker, errors = self._run_in_thread()
        res = self.w.router.handle(self.w.tg_msg("/stop"))      # wait expires while paused
        self.assertEqual(res.detail.unconfirmed_in_flight, ("job:digest:d1:telegram",))
        self.assertEqual(res.detail.expired_in_flight, ())
        self.assertFalse(res.detail.all_clear)
        self.resume.set()
        worker.join(10)
        self.assertEqual(errors, [])
        self.assertNotIn("job:digest:d1", self.w.tg.delivered_notifications())

    def test_expired_gate_cannot_send_even_without_stop(self):
        self.w.notifier._hook_after_gate = self._pause_once
        self.w.job_notify("digest", "d1", "x")
        worker, errors = self._run_in_thread()
        self.w.now = T + 25                                 # past dispatch timeout, no /stop
        self.resume.set()
        worker.join(10)
        self.assertEqual(errors, [])
        self.assertNotIn("job:digest:d1", self.w.tg.delivered_notifications())


# ============================================================ transport auth + task authority (Dolores, #28)

class TransportAuthentication(Base):
    """Commands count only as signed, fresh, unreplayed adapter envelopes."""

    def assertIgnored(self, msg, reason):
        res = self.w.router.handle(msg)
        self.assertEqual((res.outcome, res.reason), ("ignored", reason))

    def test_fabricated_stop_is_ignored_and_logged(self):
        self.assertIgnored(InboundMessage("telegram", TG_JOSH, "/stop"), "unauthenticated_transport")
        self.assertIgnored(InboundMessage("telegram", TG_JOSH, "/stop", update_id="u-x",
                                          sent_at=T, transport_mac="0" * 64),
                           "unauthenticated_transport")
        self.assertFalse(self.w.notifier.kill.is_set())
        self.assertEqual([r[2] for r in self.w.router.ignored()],
                         ["unauthenticated_transport"] * 2)

    def test_fabricated_approve_is_ignored(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        self.assertIgnored(InboundMessage("telegram", TG_JOSH, f"/approve {aid}"),
                           "unauthenticated_transport")
        self.assertIsNone(self.w.auth.authenticate(
            InboundMessage("telegram", TG_JOSH, f"/approve {aid}")).sender)
        self.assertEqual(self.w.issuer.get(aid).status, "pending")

    def test_attacker_key_is_refused(self):
        evil = TransportSigner("telegram", b"attacker-telegram-adapter-key")
        msg = evil.envelope(TG_JOSH, "/stop", update_id="e1", sent_at=T)
        self.assertIgnored(msg, "unauthenticated_transport")
        self.assertFalse(self.w.notifier.kill.is_set())

    def test_message_substitution_is_refused(self):
        from dataclasses import replace
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        genuine = self.w.tg_msg("/ack job:digest:d1")
        for field, value in (("text", "/stop"), ("text", f"/approve {aid}"),
                             ("reply_to", f"approval:{aid}"), ("sender_ref", TG_CHRISTIE),
                             ("channel", "potter_home"), ("update_id", "u-other")):
            with self.subTest(field=field):
                self.assertIgnored(replace(genuine, **{field: value}), "unauthenticated_transport")
        self.assertFalse(self.w.notifier.kill.is_set())
        self.assertEqual(self.w.issuer.get(aid).status, "pending")

    def test_replayed_update_is_refused(self):
        self.w.job_notify("digest", "d1", "x")
        ack = self.w.tg_msg("/ack job:digest:d1")
        self.assertEqual(self.w.router.handle(ack).outcome, "acked")
        self.assertIgnored(ack, "replayed")
        restarted_auth = ChannelAuth(db=self.w.db, transport_keys=TRANSPORT_KEYS,
                                     clock=lambda: self.w.now,
                                     inbound_bindings={"telegram": {TG_JOSH: JOSH}},
                                     outbound_addresses={})
        self.assertEqual(restarted_auth.authenticate(ack).reason, "replayed")   # durable

    def test_stale_update_is_refused(self):
        old = self.w.tg_msg("/stop")
        self.w.now += 301
        self.assertIgnored(old, "stale_transport")
        self.assertFalse(self.w.notifier.kill.is_set())

    def test_voucher_is_bound_to_its_command(self):
        from notifier import ApprovalRefusal
        t1, t2 = self.w.waiting_task("t1"), self.w.waiting_task("t2")
        a1, a2 = self.w.request(mail_action(t1)), self.w.request(mail_action(t2))
        ack_voucher = self.w.auth.authenticate(self.w.tg_msg("/ack job:digest:d1")).sender
        with self.assertRaises(PermissionError):
            self.w.notifier.stop(ack_voucher)
        self.assertFalse(self.w.notifier.kill.is_set())
        with self.assertRaises(ApprovalRefusal) as cm:
            self.w.issuer.decide(a1, "approve", ack_voucher, mail_action(t1))
        self.assertEqual(cm.exception.code, "command_mismatch")
        v1 = self.w.auth.authenticate(self.w.tg_msg(f"/approve {a1}")).sender
        with self.assertRaises(ApprovalRefusal) as cm:
            self.w.issuer.decide(a2, "approve", v1, mail_action(t2))     # wrong approval id
        self.assertEqual(cm.exception.code, "command_mismatch")
        deny_voucher = self.w.auth.authenticate(self.w.tg_msg(f"/deny {a2}")).sender
        with self.assertRaises(ApprovalRefusal) as cm:
            self.w.issuer.decide(a2, "approve", deny_voucher, mail_action(t2))   # deny != approve
        self.assertEqual(cm.exception.code, "command_mismatch")
        self.assertEqual({self.w.issuer.get(a).status for a in (a1, a2)}, {"pending"})

    def test_boundary_wiring_with_fake_telegram_adapter(self):
        """Fake transport -> adapter (verifies webhook secret, signs) -> router."""
        world = self.w

        class FakeTelegramAdapter:
            WEBHOOK_SECRET = "test-only-webhook-secret"

            def __init__(self, signer):
                self._signer, self._n = signer, 0

            def on_webhook(self, headers, update):
                if headers.get("X-Telegram-Bot-Api-Secret-Token") != self.WEBHOOK_SECRET:
                    return None                         # never reaches the router
                self._n += 1
                msg = self._signer.envelope(str(update["from_id"]), update["text"],
                                            update_id=f"tg-{update['update_id']}",
                                            sent_at=world.now)
                return world.router.handle(msg)

        adapter = FakeTelegramAdapter(world.signers["telegram"])
        good = {"X-Telegram-Bot-Api-Secret-Token": adapter.WEBHOOK_SECRET}
        self.assertIsNone(adapter.on_webhook({}, {"update_id": 1, "from_id": TG_JOSH,
                                                  "text": "/stop"}))
        self.assertFalse(world.notifier.kill.is_set())
        res = adapter.on_webhook(good, {"update_id": 2, "from_id": TG_STRANGER, "text": "/stop"})
        self.assertEqual((res.outcome, res.reason), ("ignored", "unknown_sender"))
        res = adapter.on_webhook(good, {"update_id": 3, "from_id": TG_JOSH, "text": "/stop"})
        self.assertEqual(res.outcome, "stopped")
        res = adapter.on_webhook(good, {"update_id": 3, "from_id": TG_JOSH, "text": "/stop"})
        self.assertEqual((res.outcome, res.reason), ("ignored", "replayed"))


class TaskAuthority(Base):
    """Approvals exist only for an authoritative C2 task waiting for them."""

    def test_request_for_missing_task_is_refused(self):
        from notifier import ApprovalRefusal
        with self.w.db.tx() as c, self.assertRaises(ApprovalRefusal) as cm:
            self.w.issuer.request(c, mail_action("no-such-task"), summary="s")
        self.assertEqual(cm.exception.code, "task_not_waiting")
        with self.w.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_request_for_task_not_waiting_is_refused(self):
        from notifier import ApprovalRefusal
        task, _ = self.w.tasks.submit("trigger:q", "send-mail")      # queued, not waiting
        with self.w.db.tx() as c, self.assertRaises(ApprovalRefusal) as cm:
            self.w.issuer.request(c, mail_action(task.id), summary="s")
        self.assertEqual(cm.exception.code, "task_not_waiting")

    def test_approve_after_task_vanishes_is_refused(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        with self.w.db.tx() as c:
            c.execute("DELETE FROM tasks WHERE id=?", (tid,))
        res = self.w.router.handle(self.w.tg_msg(f"/approve {aid}"))
        self.assertEqual((res.outcome, res.reason), ("refused", "task_not_waiting"))
        self.assertEqual(self.w.issuer.get(aid).status, "voided")


# ============================================================ crash safety + honest stop boundary (Dolores, #28 @ c8c8422)

class CommandCrashSafety(Base):
    """A verified command is never lost to a crash, and never runs twice."""

    def reopen(self):
        """Simulated restart: fresh objects (auth, notifier, issuer, router) on the same file."""
        w2 = World(self._tmp.name)
        w2.now = self.w.now
        w2.live = self.w.live
        return w2

    def test_crash_after_auth_before_stop_then_redelivery_stops(self):
        msg = self.w.tg_msg("/stop")
        self.assertTrue(self.w.auth.authenticate(msg).ok)      # recorded pending ... crash
        self.assertFalse(self.w.notifier.kill.is_set())
        w2 = self.reopen()
        res = w2.router.handle(msg)                            # adapter redelivers
        self.assertEqual(res.outcome, "stopped")
        self.assertTrue(w2.notifier.kill.is_set())
        self.assertEqual(w2.router.handle(msg).reason, "replayed")   # processed: once only

    def test_crash_after_stop_commits_before_reply_is_not_rerun(self):
        msg = self.w.tg_msg("/stop")
        real_stop = self.w.notifier.stop

        def stop_then_crash(sender, now=None):
            real_stop(sender, now)
            raise Crash()                                      # dies before replying
        self.w.notifier.stop = stop_then_crash
        with self.assertRaises(Crash):
            self.w.router.handle(msg)
        w2 = self.reopen()
        self.assertTrue(w2.notifier.kill.is_set())
        self.assertEqual(w2.router.handle(msg).reason, "replayed")
        w2.notifier.run()
        self.assertEqual([m.body["kind"] for m in w2.tg.delivered], ["stop_ack"])   # one reply

    def test_crash_after_auth_before_approve_then_redelivery_approves_once(self):
        tid = self.w.waiting_task()
        action = mail_action(tid)
        aid = self.w.request(action)
        msg = self.w.tg_msg(f"/approve {aid}")
        self.assertTrue(self.w.auth.authenticate(msg).ok)      # crash before decide
        w2 = self.reopen()
        res = w2.router.handle(msg)
        self.assertEqual(res.outcome, "approved")
        self.assertEqual(w2.c1(action, res.detail).outcome, ALLOW)
        again = w2.router.handle(msg)
        self.assertEqual((again.outcome, again.reason), ("ignored", "replayed"))

    def test_crash_after_approve_commits_is_not_rerun(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        msg = self.w.tg_msg(f"/approve {aid}")
        real = self.w.issuer.decide

        def decide_then_crash(*a, **kw):
            real(*a, **kw)
            raise Crash()
        self.w.issuer.decide = decide_then_crash
        with self.assertRaises(Crash):
            self.w.router.handle(msg)
        w2 = self.reopen()
        with w2.db.read() as c:
            nonce = c.execute("SELECT nonce FROM approval_requests").fetchone()[0]
        self.assertEqual(w2.router.handle(msg).reason, "replayed")
        with w2.db.read() as c:   # still exactly one decision, same nonce
            self.assertEqual(c.execute("SELECT nonce FROM approval_requests").fetchone()[0], nonce)

    def test_duplicate_pending_deliveries_execute_once(self):
        msg = self.w.tg_msg("/stop")
        first = self.w.auth.authenticate(msg).sender            # pending, voucher 1
        self.assertEqual(self.w.router.handle(msg).outcome, "stopped")   # resumes, voucher 2
        from notifier.auth import AlreadyProcessed
        with self.assertRaises(AlreadyProcessed):
            self.w.notifier.stop(first)                         # loser rolls back
        self.w.notifier.run()
        self.assertEqual([m.body["kind"] for m in self.w.tg.delivered], ["stop_ack"])

    def test_refused_command_is_marked_processed(self):
        msg = self.w.tg_msg("/approve appr-nonexistent")
        self.assertEqual(self.w.router.handle(msg).reason, "unknown_approval")
        self.assertEqual(self.w.router.handle(msg).reason, "replayed")
        with self.w.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM inbound_seen WHERE status='pending'"
                                       ).fetchone()[0], 0)


class StopBoundaryAfterFinalRecheck(Base):
    """The documented boundary: past the final recheck may deliver once, reported unconfirmed."""

    def setUp(self):
        super().setUp()
        import threading
        self.threading = threading
        self.paused, self.resume = threading.Event(), threading.Event()

    def _pause_once(self, msg):
        if msg.notification_id == "job:digest:d1" and not self.paused.is_set():
            self.paused.set()
            self.assertTrue(self.resume.wait(10))

    def _start(self):
        t = self.threading.Thread(target=self.w.notifier.run)
        t.start()
        self.assertTrue(self.paused.wait(10))
        return t

    def test_after_final_recheck_aged_sender_is_reported_and_may_deliver_once(self):
        self.w.notifier._hook_after_recheck = self._pause_once
        self.w.job_notify("digest", "d1", "x")
        worker = self._start()
        self.w.now = T + 21                                    # past the 20 s dispatch timeout
        res = self.w.router.handle(self.w.tg_msg("/stop"))
        self.assertEqual(res.detail.unconfirmed_in_flight, ("job:digest:d1:telegram",))
        self.assertEqual(res.detail.expired_in_flight, ("job:digest:d1:telegram",))
        self.assertFalse(res.detail.all_clear)
        self.resume.set()
        worker.join(10)
        self.assertEqual(self.w.tg.delivered_notifications().count("job:digest:d1"), 1)

    def test_after_final_recheck_bounded_wait_expiry_reports_unconfirmed(self):
        self.w.notifier = quick = self.w.make_notifier(stop_wait_s=0.1)
        self.w.router = CommandRouter(auth=self.w.auth, notifier=quick, issuer=self.w.issuer,
                                      live_action=lambda aid, tid: None, clock=lambda: self.w.now)
        quick._hook_after_recheck = self._pause_once
        self.w.job_notify("digest", "d1", "x")
        worker = self._start()
        res = self.w.router.handle(self.w.tg_msg("/stop"))
        self.assertEqual(res.detail.unconfirmed_in_flight, ("job:digest:d1:telegram",))
        self.assertEqual(res.detail.expired_in_flight, ())
        self.assertFalse(res.detail.all_clear)
        self.resume.set()
        worker.join(10)
        self.assertEqual(self.w.tg.delivered_notifications().count("job:digest:d1"), 1)


# ============================================================ Dolores review @ f14e564

class _Reopen(Base):
    def reopen(self, **auth_kw):
        w2 = World(self._tmp.name)
        w2.now, w2.live = self.w.now, self.w.live
        if auth_kw:
            w2.auth = ChannelAuth(db=w2.db, transport_keys=TRANSPORT_KEYS, clock=lambda: w2.now,
                                  outbound_addresses={JOSH: {"telegram": "chat-1001",
                                                             "email": MAIL_JOSH}}, **auth_kw)
            w2.router = CommandRouter(auth=w2.auth, notifier=w2.notifier, issuer=w2.issuer,
                                      live_action=lambda aid, tid: w2.live.get(tid),
                                      clock=lambda: w2.now)
        return w2

    def pending_count(self):
        with self.w.db.read() as c:
            return c.execute("SELECT COUNT(*) FROM inbound_seen WHERE status='pending'"
                             ).fetchone()[0]


class FailureLeavesCommandPending(_Reopen):
    """Finding 1: only a known, completed outcome marks an update processed."""

    def test_signing_failure_inside_decide_then_redelivery_approves_once(self):
        tid = self.w.waiting_task()
        action = mail_action(tid)
        aid = self.w.request(action)
        real_key = self.w.issuer._key

        class FlakyKey:
            key_id = real_key.key_id
            calls = 0

            def sign(self, payload):
                FlakyKey.calls += 1
                if FlakyKey.calls == 1:
                    raise OSError("HSM unavailable")
                return real_key.sign(payload)
        self.w.issuer._key = FlakyKey()
        msg = self.w.tg_msg(f"/approve {aid}")
        with self.assertRaises(OSError):
            self.w.router.handle(msg)
        self.assertEqual(self.pending_count(), 1)                 # not marked processed
        self.assertEqual(self.w.issuer.get(aid).status, "pending")  # tx rolled back
        res = self.w.router.handle(msg)                           # adapter redelivers
        self.assertEqual(res.outcome, "approved")
        self.assertEqual(self.w.c1(action, res.detail).outcome, ALLOW)
        self.assertEqual(self.w.router.handle(msg).reason, "replayed")

    def test_trace_failure_inside_decide_then_restart_redelivery_approves_once(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        self.w.issuer._sink = lambda line: (_ for _ in ()).throw(OSError("sink down"))
        msg = self.w.tg_msg(f"/approve {aid}")
        with self.assertRaises(OSError):
            self.w.router.handle(msg)
        w2 = self.reopen()
        self.assertEqual(w2.router.handle(msg).outcome, "approved")
        with w2.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM approval_requests WHERE"
                                       " status='approved'").fetchone()[0], 1)

    def test_failure_inside_stop_transaction_then_redelivery_stops_once(self):
        real = self.w.notifier._sweep_tx
        calls = []

        def flaky_sweep(c, now):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("disk hiccup")
            return real(c, now)
        self.w.notifier._sweep_tx = flaky_sweep
        msg = self.w.tg_msg("/stop")
        with self.assertRaises(RuntimeError):
            self.w.router.handle(msg)
        self.assertFalse(self.w.notifier.kill.is_set())           # rolled back
        self.assertEqual(self.pending_count(), 1)
        self.assertEqual(self.w.router.handle(msg).outcome, "stopped")
        self.assertEqual(self.w.router.handle(msg).reason, "replayed")
        self.w.notifier.run()
        self.assertEqual([m.body["kind"] for m in self.w.tg.delivered], ["stop_ack"])

    def test_failure_after_commit_is_not_reapplied(self):
        tid = self.w.waiting_task()
        aid = self.w.request(mail_action(tid))
        real = self.w.issuer.decide

        def decide_then_fail(*a, **kw):
            real(*a, **kw)
            raise RuntimeError("reply path failed after commit")
        self.w.issuer.decide = decide_then_fail
        msg = self.w.tg_msg(f"/approve {aid}")
        with self.assertRaises(RuntimeError):
            self.w.router.handle(msg)
        self.assertEqual(self.pending_count(), 0)                 # marked in the effect tx
        with self.w.db.read() as c:
            nonce = c.execute("SELECT nonce FROM approval_requests").fetchone()[0]
        self.assertEqual(self.reopen().router.handle(msg).reason, "replayed")
        with self.w.db.read() as c:
            self.assertEqual(c.execute("SELECT nonce FROM approval_requests").fetchone()[0], nonce)

    def test_ack_failure_leaves_pending_and_redelivery_acks(self):
        self.w.job_notify("digest", "d1", "x")
        real_ack = self.w.notifier.outbox.ack
        calls = []

        def flaky(mid, now=None):
            calls.append(mid)
            if len(calls) == 1:
                raise RuntimeError("busy")
            return real_ack(mid, now=now)
        self.w.notifier.outbox.ack = flaky
        msg = self.w.tg_msg("/ack job:digest:d1")
        with self.assertRaises(RuntimeError):
            self.w.router.handle(msg)
        self.assertEqual(self.pending_count(), 1)
        self.assertEqual(self.w.router.handle(msg).outcome, "acked")
        self.assertEqual(self.w.notifier.outbox.get("job:digest:d1:telegram").status, "acked")


class PendingRecoveryPath(_Reopen):
    """Finding 2: pending commands survive long outages; nothing else gets in."""

    def test_restart_after_freshness_window_resumes_pending_command(self):
        msg = self.w.tg_msg("/stop")
        self.assertTrue(self.w.auth.authenticate(msg).ok)         # journaled pending ... crash
        self.w.now += 3600                                        # outage far beyond 300 s
        w2 = self.reopen()
        self.assertEqual(w2.router.handle(msg).outcome, "stopped")
        self.assertTrue(w2.notifier.kill.is_set())

    def test_altered_envelope_for_pending_update_is_refused(self):
        msg = self.w.tg_msg("/ack job:digest:d1")
        self.assertTrue(self.w.auth.authenticate(msg).ok)         # pending
        self.w.now += 3600
        w2 = self.reopen()
        # Same update id, validly signed but different content: not the journaled envelope.
        swapped = w2.signers["telegram"].envelope(TG_JOSH, "/stop", update_id=msg.update_id,
                                                  sent_at=msg.sent_at)
        res = w2.router.handle(swapped)
        self.assertEqual((res.outcome, res.reason), ("ignored", "replayed"))
        self.assertFalse(w2.notifier.kill.is_set())
        from dataclasses import replace
        self.assertEqual(w2.router.handle(replace(msg, text="/stop")).reason,
                         "unauthenticated_transport")

    def test_stale_never_seen_update_is_refused_and_not_recorded(self):
        msg = self.w.tg_msg("/stop")
        self.w.now += 301
        self.assertEqual(self.w.router.handle(msg).reason, "stale_transport")
        with self.w.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM inbound_seen").fetchone()[0], 0)
        self.assertFalse(self.w.notifier.kill.is_set())

    def test_pruned_pending_row_is_never_resurrected(self):
        # E3 (or any pruner) removes an E1 pending row after 2x skew; a later
        # redelivery of that exact envelope is stale, never resumed or re-recorded.
        msg = self.w.tg_msg("/stop")
        self.assertTrue(self.w.auth.authenticate(msg).ok)         # pending ... crash
        with self.w.db.tx() as c:
            c.execute("DELETE FROM inbound_seen WHERE update_id=?", (msg.update_id,))
        self.w.now += 2 * 300 + 1
        w2 = self.reopen()
        res = w2.router.handle(msg)
        self.assertEqual((res.outcome, res.reason), ("ignored", "stale_transport"))
        self.assertFalse(w2.notifier.kill.is_set())
        with w2.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM inbound_seen").fetchone()[0], 0)

    def test_pruned_processed_row_is_stale_not_replayable(self):
        msg = self.w.tg_msg("/noop")
        self.w.router.handle(msg)                                  # processed
        self.w.now += 2 * 300 + 61
        self.w.router.handle(self.w.tg_msg("/noop"))               # prune runs
        res = self.w.router.handle(msg)
        self.assertEqual(res.reason, "stale_transport")
        with self.w.db.read() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM inbound_seen WHERE update_id=?",
                                        (msg.update_id,)).fetchone())

    def test_recovery_rechecks_authorization(self):
        msg = self.w.tg_msg("/stop")
        self.assertTrue(self.w.auth.authenticate(msg).ok)         # pending ... crash
        w2 = self.reopen(inbound_bindings={"telegram": {}})        # Josh's binding revoked
        self.assertEqual(w2.router.handle(msg).reason, "unknown_sender")
        self.assertFalse(w2.notifier.kill.is_set())
        self.assertEqual(self.pending_count(), 0)                 # closed, never executed


class RetentionBounds(_Reopen):
    def test_journal_keeps_digest_only_and_vouchers_are_released(self):
        self.w.job_notify("digest", "d1", "x")
        self.w.router.handle(self.w.tg_msg("/ack job:digest:d1"))
        with self.w.db.read() as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(inbound_seen)")]
            blob = json.dumps(c.execute("SELECT * FROM inbound_seen").fetchall())
        self.assertNotIn("command", cols)
        self.assertNotIn("reply_to", cols)
        self.assertNotIn("/ack", blob)
        self.assertNotIn("job:digest:d1", blob)
        self.assertEqual(self.w.auth._minted, {})                  # released after dispatch

    def test_voucher_map_is_capped(self):
        from notifier import auth as auth_mod
        for _ in range(auth_mod.MAX_MINTED + 50):
            self.w.auth.authenticate(self.w.tg_msg("/noop"))
        self.assertLessEqual(len(self.w.auth._minted), auth_mod.MAX_MINTED)

    def test_old_processed_rows_are_pruned_pending_rows_kept(self):
        done = self.w.tg_msg("/noop")
        self.w.router.handle(done)                                 # processed
        pending = self.w.tg_msg("/stop")
        self.assertTrue(self.w.auth.authenticate(pending).ok)     # pending
        self.w.now += 2 * 300 + 61
        self.w.router.handle(self.w.tg_msg("/noop"))               # triggers prune
        with self.w.db.read() as c:
            ids = {r[0]: r[1] for r in c.execute("SELECT update_id, status FROM inbound_seen")}
        self.assertNotIn(done.update_id, ids)
        self.assertEqual(ids[pending.update_id], "pending")
        # The pruned envelope cannot come back: it is stale now.
        self.assertEqual(self.w.router.handle(done).reason, "stale_transport")


class DurableInFlightRecord(_Reopen):
    """T19: the in-flight record is durable and only resolution removes it."""

    def rows(self, db=None):
        with (db or self.w.db).read() as c:
            return c.execute("SELECT message_id, dispatch, status FROM notifier_sending"
                             " ORDER BY message_id, dispatch").fetchall()

    def stop(self, w=None):
        w = w or self.w
        return w.router.handle(w.tg_msg("/stop")).detail

    def test_crash_before_dispatch_leaves_no_record_and_no_send(self):
        def crash(msg):
            raise Crash()
        self.w.notifier._hook_after_gate = crash
        self.w.job_notify("digest", "d1", "x")
        with self.assertRaises(Crash):
            self.w.notifier.run()
        self.assertEqual(self.rows(), [])                           # never dispatched
        self.assertEqual(self.w.tg.calls, 0)
        self.assertTrue(self.stop().all_clear)

    def test_crash_after_dispatch_is_reported_unknown_across_restart_and_age(self):
        self.w.tg.crash_after_delivery = Crash()
        self.w.job_notify("digest", "d1", "x")
        with self.assertRaises(Crash):
            self.w.notifier.run()
        self.assertEqual(self.rows(), [("job:digest:d1:telegram", 1, "unknown")])
        self.w.now += 3600                                         # leases long expired
        w2 = self.reopen()
        w2.notifier.run()                                          # reconcile: still unknown
        self.assertEqual(w2.notifier.outbox.get("job:digest:d1:telegram").status,
                         "abandoned_unknown")
        report = self.stop(w2)
        self.assertEqual(report.unknown_outcome, ("job:digest:d1:telegram",))
        self.assertIn("job:digest:d1:telegram", report.unconfirmed_in_flight)
        self.assertFalse(report.all_clear)
        self.assertEqual(self.rows(w2.db), [("job:digest:d1:telegram", 1, "unknown")])

    def test_dead_process_dispatching_record_never_ages_out(self):
        self.w.job_notify("digest", "d1", "x")
        with self.w.db.tx() as c:                                  # killed -9 mid-dispatch
            c.execute("INSERT INTO notifier_sending (message_id, dispatch, owner, since,"
                      " status) VALUES ('job:digest:d1:telegram', 1, 'dead', ?, 'dispatching')",
                      (self.w.now,))
        for hours in (1, 24, 24 * 30):
            self.w.now = T + hours * 3600
            report = self.reopen().router.handle(self.w.tg_msg("/stop")).detail
            self.assertEqual(report.unconfirmed_in_flight, ("job:digest:d1:telegram",))
            self.assertEqual(report.expired_in_flight, ("job:digest:d1:telegram",))
            self.assertFalse(report.all_clear)

    def test_provider_timeout_is_unknown_until_reconciled_delivered(self):
        self.w.tg.ambiguous_after_delivery = True
        self.w.job_notify("digest", "d1", "x")
        self.w.notifier.run()                                      # timeout -> unknown, routine abandoned
        self.w.tg.ambiguous_after_delivery = False
        self.assertEqual(self.rows(), [("job:digest:d1:telegram", 1, "unknown")])
        self.assertEqual(self.stop().unknown_outcome, ("job:digest:d1:telegram",))
        # Provider later confirms delivery (e.g. Josh acks): the record resolves.
        self.w.notifier.outbox.ack("job:digest:d1:telegram")
        self.w.notifier.run()
        self.assertEqual(self.rows(), [])

    def test_retry_and_duplicate_send_are_recorded_per_dispatch(self):
        self.w.tg.ambiguous_after_delivery = True
        seen = []
        self.w.tg.on_send = lambda m: seen.append(self.rows())
        self.w.job_notify("monitor", "fire", "alarm", urgency="urgent")
        self.w.notifier.run()                                      # dispatch 1 unknown, 2 resent
        self.assertEqual(self.w.tg.delivered_notifications(), ["job:monitor:fire"] * 2)
        self.assertEqual(seen[1], [("job:monitor:fire:telegram", 1, "unknown"),
                                   ("job:monitor:fire:telegram", 2, "dispatching")])
        self.assertEqual(self.rows(), [("job:monitor:fire:telegram", 1, "unknown"),
                                       ("job:monitor:fire:telegram", 2, "unknown")])
        self.assertEqual(self.stop().unknown_outcome, ("job:monitor:fire:telegram",))

    def test_reconciled_success_clears_record(self):
        self.w.tg.crash_after_delivery = Crash()
        self.w.job_notify("digest", "d1", "x")
        with self.assertRaises(Crash):
            self.w.notifier.run()
        self.w.now += 60
        w2 = self.reopen()
        w2.tg.reconcile_verdict = "succeeded"
        w2.notifier.run()
        self.assertEqual(w2.notifier.outbox.get("job:digest:d1:telegram").status, "sent")
        self.assertEqual(self.rows(w2.db), [])
        self.assertTrue(self.stop(w2).all_clear)

    def test_concurrent_stop_and_dispatch_never_reports_all_clear_falsely(self):
        import threading
        for trial in range(15):
            with self.subTest(trial=trial):
                self.tearDown()
                self.setUp()
                for i in range(6):
                    self.w.job_notify("digest", f"b{i}", f"{i}")
                done = threading.Event()
                late = []                                       # sends after stop() returned
                started = threading.Event()

                def on_send(m):
                    started.set()
                    if done.is_set() and not m.notification_id.startswith("stop:"):
                        late.append(m.message_id)
                self.w.tg.on_send = on_send
                worker = threading.Thread(target=self.w.notifier.run)
                worker.start()
                self.assertTrue(started.wait(10))
                report = self.stop()
                done.set()
                worker.join(10)
                # Any send that landed after stop() returned was reported unconfirmed.
                self.assertTrue(set(late) <= set(report.unconfirmed_in_flight),
                                (late, report))
                if late:
                    self.assertFalse(report.all_clear)
                self.assertEqual(self.rows(), [])

    def test_unreadable_in_flight_state_is_never_all_clear(self):
        def broken():
            raise sqlite3.OperationalError("disk I/O error")
        self.w.notifier._await_in_flight = broken
        report = self.stop()
        self.assertTrue(report.state_unknown)
        self.assertFalse(report.all_clear)


# ============================================================ clock rollback (Dolores @ 225e7df)

class ClockRollback(_Reopen):
    """Replay protection must not trust a wall clock that moves backwards."""

    def jump_then_prune(self):
        stop = self.w.tg_msg("/stop")                               # signed at T
        self.assertEqual(self.w.router.handle(stop).outcome, "stopped")
        self.w.notifier.kill.clear()                                # operator clears kill
        self.w.now = T + 1000
        self.w.router.handle(self.w.tg_msg("/noop"))                # prunes the /stop row
        with self.w.db.read() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM inbound_seen WHERE update_id=?",
                                        (stop.update_id,)).fetchone())
        return stop

    def test_dolores_jump_forward_then_roll_back(self):
        stop = self.jump_then_prune()
        self.w.now = T                                              # clock rolls back
        res = self.w.router.handle(stop)
        self.assertEqual((res.outcome, res.reason), ("ignored", "clock_regression"))
        self.assertFalse(self.w.notifier.kill.is_set())
        # Fail closed: even a brand-new, correctly signed command is refused.
        self.assertEqual(self.w.router.handle(self.w.tg_msg("/stop")).reason,
                         "clock_regression")
        self.assertFalse(self.w.notifier.kill.is_set())

    def test_restart_between_jump_and_rollback(self):
        stop = self.jump_then_prune()
        self.w.now = T
        w2 = self.reopen()                                          # nothing process-local survives
        self.assertEqual(w2.router.handle(stop).reason, "clock_regression")
        self.assertFalse(w2.notifier.kill.is_set())

    def test_rollback_on_another_transport_also_fails_closed(self):
        self.jump_then_prune()
        self.w.now = T
        res = self.w.router.handle(self.w.mail_msg("/stop", dkim=True))
        self.assertEqual(res.reason, "clock_regression")

    def test_operator_reconcile_keeps_old_envelopes_rejected(self):
        stop = self.jump_then_prune()
        self.w.now = T
        self.w.auth.reconcile_clock()                               # operator accepts clock T
        # The sent_at watermark did not move back: the replay is still refused.
        self.assertEqual(self.w.router.handle(stop).reason, "stale_transport")
        self.assertFalse(self.w.notifier.kill.is_set())
        # Once the clock passes the watermark floor again, new commands work.
        self.w.now = T + 1000
        self.assertEqual(self.w.router.handle(self.w.tg_msg("/stop")).outcome, "stopped")

    def test_pruned_envelope_rejected_by_watermark_without_rollback(self):
        stop = self.jump_then_prune()
        self.w.now = T + 1000
        self.assertEqual(self.w.router.handle(stop).reason, "stale_transport")

    def test_restore_of_db_copy_taken_after_acceptance(self):
        import shutil
        stop = self.w.tg_msg("/stop")
        self.assertEqual(self.w.router.handle(stop).outcome, "stopped")
        self.w.notifier.kill.clear()
        db_path = Path(self._tmp.name) / "potter.db"
        backup = Path(self._tmp.name) / "backup"
        backup.mkdir()
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db_path) + suffix)
            if src.exists():
                shutil.copy(src, backup / (db_path.name + suffix))
        self.jump_then_prune_existing()
        # Restore the older copy (it still holds the processed /stop row), roll back.
        for suffix in ("", "-wal", "-shm"):
            dst = Path(str(db_path) + suffix)
            if dst.exists():
                dst.unlink()
            src = backup / (db_path.name + suffix)
            if src.exists():
                shutil.copy(src, dst)
        self.w.now = T
        w2 = self.reopen()
        self.assertEqual(w2.router.handle(stop).reason, "replayed")
        self.assertFalse(w2.notifier.kill.is_set())

    def jump_then_prune_existing(self):
        self.w.now = T + 1000
        self.w.router.handle(self.w.tg_msg("/noop"))

    def test_restore_of_db_copy_taken_after_jump_fails_closed(self):
        import shutil
        stop = self.jump_then_prune()
        db_path = Path(self._tmp.name) / "potter.db"
        snap = Path(self._tmp.name) / "snap"
        snap.mkdir()
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db_path) + suffix)
            if src.exists():
                shutil.copy(src, snap / (db_path.name + suffix))
        self.w.now = T + 2000
        self.w.router.handle(self.w.tg_msg("/noop"))
        for suffix in ("", "-wal", "-shm"):
            dst = Path(str(db_path) + suffix)
            if dst.exists():
                dst.unlink()
            src = snap / (db_path.name + suffix)
            if src.exists():
                shutil.copy(src, dst)
        self.w.now = T                                              # restored + rolled back
        w2 = self.reopen()
        self.assertEqual(w2.router.handle(stop).reason, "clock_regression")
        self.assertFalse(w2.notifier.kill.is_set())

    def test_small_backward_step_within_tolerance_is_tolerated(self):
        self.w.now = T + 100
        self.w.router.handle(self.w.tg_msg("/noop"))
        self.w.now = T + 97                                         # 3 s back (< 5 s)
        self.assertEqual(self.w.router.handle(self.w.tg_msg("/stop")).outcome, "stopped")

    def test_high_water_and_skew_are_exposed_read_only(self):
        self.w.router.handle(self.w.tg_msg("/noop"))
        hw = self.w.auth.high_water("telegram")
        self.assertEqual(hw, {"max_sent_at": T, "max_clock": T})
        hw["max_sent_at"] = 0                                       # a copy: no effect
        self.assertEqual(self.w.auth.high_water("telegram")["max_sent_at"], T)
        self.assertEqual(self.w.auth.max_skew_s, 300.0)
        self.assertEqual(self.w.auth.prune_horizon_s, 660.0)
        with self.assertRaises(AttributeError):
            self.w.auth.max_skew_s = 1


# ============================================================ schema migration e1.v0 -> e1.v1 (Dolores @ c23ecd9)

def _v0_ddl():
    """The e1.v0 fixture is generated from the accepted schema text itself
    (``_schema.V0_SCHEMA``, verbatim from 225e7df), so it cannot drift."""
    from notifier import _schema
    return tuple(_schema.V0_SCHEMA)


class SchemaMigration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "potter.db"
        self.signer = TransportSigner("telegram", TRANSPORT_KEYS["telegram"])

    def tearDown(self):
        from notifier import _schema
        _schema._MIGRATION_FAULT = None
        self._tmp.cleanup()

    def make_v0(self, rows=(), ddl=None):
        """A database as 225e7df left it: C2/C3 tables, e1.v0 notifier tables, rows."""
        from runtime import Outbox
        db = Database(self.path)
        Outbox(db)
        with db.tx() as c:
            for stmt in (ddl if ddl is not None else _v0_ddl()):
                c.execute(stmt)
            c.execute("INSERT INTO meta (key, value) VALUES ('notifier_schema', 'e1.v0')")
            for r in rows:
                c.execute("INSERT INTO inbound_seen (channel, update_id, at, status, principal,"
                          " envelope_digest, processed_at) VALUES (?,?,?,?,?,?,?)", r)
        return db

    def envelope(self, text, update_id, sent_at=T):
        return self.signer.envelope(TG_JOSH, text, update_id=update_id, sent_at=sent_at)

    def v0_row(self, msg, status, processed_at=T):
        digest = ChannelAuth._digest(msg)
        return ("telegram", msg.update_id, msg.sent_at, status,
                "human:josh" if status == "pending" else None, digest,
                processed_at if status == "processed" else None)

    def open_world(self, now=T + 10):
        w = World.__new__(World)
        World.__init__(w, self._tmp.name)          # migrates on first construction
        w.now = now
        return w

    def schema_version(self):
        with Database(self.path).read() as c:
            return c.execute("SELECT value FROM meta WHERE key='notifier_schema'").fetchone()[0]

    def test_v0_database_is_migrated_and_old_processed_row_stays_burned(self):
        stop = self.envelope("/stop", "old-1")
        self.make_v0([self.v0_row(stop, "processed")])
        w = self.open_world()
        self.assertEqual(self.schema_version(), "e1.v1")
        with w.db.read() as c:
            row = c.execute("SELECT status, sent_at, migration_flag FROM inbound_seen"
                            " WHERE update_id='old-1'").fetchone()
        self.assertEqual(row[0], "processed")
        self.assertEqual(row[2], "sent_at_unknown")
        # Replay of the pre-migration envelope after migration: rejected.
        w.now = T + 10
        self.assertEqual(w.router.handle(stop).reason, "replayed")
        self.assertFalse(w.notifier.kill.is_set())
        # ... and still rejected once its row is long pruned.
        w.now = T + 10_000
        w.router.handle(w.tg_msg("/noop"))
        self.assertEqual(w.router.handle(stop).reason, "stale_transport")

    def test_old_pending_command_needs_operator_and_never_runs(self):
        stop = self.envelope("/stop", "old-p")
        self.make_v0([self.v0_row(stop, "pending")])
        w = self.open_world()
        self.assertEqual(w.auth.needs_operator(), [("telegram", "old-p")])
        res = w.router.handle(stop)
        self.assertEqual((res.outcome, res.reason), ("ignored", "needs_operator"))
        self.assertFalse(w.notifier.kill.is_set())
        self.assertTrue(w.auth.burn_needs_operator("telegram", "old-p"))
        self.assertEqual(w.auth.needs_operator(), [])
        self.assertEqual(w.router.handle(stop).reason, "replayed")   # burned, never executed
        self.assertFalse(w.notifier.kill.is_set())

    def test_high_water_is_seeded_at_migration(self):
        old = self.envelope("/noop", "old-2", sent_at=T)
        self.make_v0([self.v0_row(old, "processed", processed_at=T + 50)])
        w = self.open_world(now=T + 10)
        hw = w.auth.high_water("telegram")
        self.assertEqual(hw, {"max_sent_at": T + 50, "max_clock": T + 50})   # max(now=T, T+50)
        # A pre-migration envelope whose row v0 had already pruned: below the floor.
        pruned_earlier = self.envelope("/stop", "old-gone", sent_at=T - 400)
        w.now = T + 60
        self.assertEqual(w.router.handle(pruned_earlier).reason, "stale_transport")

    def test_interrupted_migration_rolls_back_then_restart_migrates_cleanly(self):
        from notifier import _schema
        from runtime import JournalUnavailable
        stop = self.envelope("/stop", "old-3")
        self.make_v0([self.v0_row(stop, "processed"), self.v0_row(
            self.envelope("/ack x", "old-4"), "pending")])

        def crash():
            raise sqlite3.OperationalError("simulated crash mid-migration")
        _schema._MIGRATION_FAULT = crash
        with self.assertRaises(JournalUnavailable):
            self.open_world()
        # Nothing half-done: still e1.v0, original table and rows intact.
        self.assertEqual(self.schema_version(), "e1.v0")
        with Database(self.path).read() as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(inbound_seen)")]
            n = c.execute("SELECT COUNT(*) FROM inbound_seen").fetchone()[0]
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("sent_at", cols)
        self.assertEqual(n, 2)
        self.assertNotIn("inbound_seen_v0", tables)
        _schema._MIGRATION_FAULT = None
        w = self.open_world()                           # restart: clean migration
        self.assertEqual(self.schema_version(), "e1.v1")
        self.assertEqual(w.auth.needs_operator(), [("telegram", "old-4")])
        self.assertEqual(w.router.handle(stop).reason, "replayed")

    def test_unknown_future_schema_version_is_refused(self):
        from runtime import JournalUnavailable
        db = self.make_v0()
        with db.tx() as c:
            c.execute("UPDATE meta SET value='e1.v9' WHERE key='notifier_schema'")
        with self.assertRaises(JournalUnavailable):
            self.open_world()
        self.assertEqual(self.schema_version(), "e1.v9")      # untouched

    def test_unrecognised_v0_layout_is_refused(self):
        from runtime import JournalUnavailable
        db = self.make_v0()
        with db.tx() as c:                               # e.g. an older pre-225e7df layout
            c.execute("DROP TABLE inbound_seen")
            c.execute("CREATE TABLE inbound_seen (channel TEXT, update_id TEXT, at REAL,"
                      " PRIMARY KEY (channel, update_id))")
        with self.assertRaises(JournalUnavailable):
            self.open_world()
        self.assertEqual(self.schema_version(), "e1.v0")

    def test_v0_schema_text_is_verbatim_from_225e7df(self):
        import ast
        import subprocess
        try:
            src = subprocess.run(["git", "-C", str(ROOT), "show", "225e7df:notifier/_schema.py"],
                                 capture_output=True, text=True, check=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            self.skipTest("225e7df not available in this checkout (shallow clone)")
        tree = ast.parse(src)
        real = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", "") == "SCHEMA")
        self.assertEqual(tuple(real), _v0_ddl())

    def assertRefusedUntouched(self, ddl, why):
        from runtime import JournalUnavailable
        stop = self.envelope("/stop", "old-x")
        self.make_v0(ddl=ddl)
        with Database(self.path).tx() as c:
            c.execute("INSERT INTO inbound_seen (channel, update_id, at, status, principal,"
                      " envelope_digest, processed_at) VALUES (?,?,?,?,?,?,?)",
                      self.v0_row(stop, "processed"))
        with Database(self.path).read() as c:
            before = sorted(c.execute("SELECT type, name, sql FROM sqlite_master"
                                      " WHERE substr(name, 1, 7) <> 'sqlite_'").fetchall(), key=str)
        with self.assertRaises(JournalUnavailable, msg=why):
            self.open_world()
        self.assertEqual(self.schema_version(), "e1.v0", why)
        with Database(self.path).read() as c:
            after = sorted(c.execute("SELECT type, name, sql FROM sqlite_master"
                                     " WHERE substr(name, 1, 7) <> 'sqlite_'").fetchall(), key=str)
            n = c.execute("SELECT COUNT(*) FROM inbound_seen").fetchone()[0]
        self.assertEqual(after, before, f"{why}: schema changed")   # no partial migration
        self.assertEqual(n, 1, why)

    def variant(self, table, old, new):
        ddl, hits = [], 0
        for stmt in _v0_ddl():
            if f"TABLE IF NOT EXISTS {table} " in stmt and old in stmt:
                stmt, hits = stmt.replace(old, new, 1), hits + 1
            ddl.append(stmt)
        self.assertEqual(hits, 1, f"variant {table}: {old!r} not found")
        return tuple(ddl)

    def test_altered_check_constraint_is_refused(self):
        self.assertRefusedUntouched(self.variant(
            "inbound_seen", "('pending','processed')", "('pending','processed','other')"),
            "CHECK allows a third status")

    def test_dropped_not_null_is_refused(self):
        self.assertRefusedUntouched(self.variant(
            "inbound_seen", "envelope_digest TEXT NOT NULL", "envelope_digest TEXT"),
            "NOT NULL dropped")

    def test_changed_primary_key_is_refused(self):
        self.assertRefusedUntouched(self.variant(
            "inbound_seen", "PRIMARY KEY (channel, update_id)", "PRIMARY KEY (update_id, channel)"),
            "primary key changed")

    def test_changed_type_is_refused(self):
        self.assertRefusedUntouched(self.variant(
            "inbound_seen", "at REAL NOT NULL", "at TEXT NOT NULL"), "column type changed")

    def test_extra_index_is_refused(self):
        self.assertRefusedUntouched(
            _v0_ddl() + ("CREATE INDEX inbound_seen_extra ON inbound_seen (status)",),
            "extra index")

    def test_missing_index_is_refused(self):
        self.assertRefusedUntouched(
            tuple(st for st in _v0_ddl() if "approval_requests_task" not in st),
            "missing index")

    def test_extra_trigger_is_refused(self):
        self.assertRefusedUntouched(_v0_ddl() + (
            "CREATE TRIGGER inbound_seen_t AFTER INSERT ON inbound_seen BEGIN SELECT 1; END",),
            "extra trigger")

    def test_same_column_changed_literal_is_refused(self):
        # Dolores: the CHECK literal 'pending' becomes 'IF NOT EXISTS pending'.
        self.assertRefusedUntouched(self.variant(
            "inbound_seen", "('pending','processed')", "('IF NOT EXISTS pending','processed')"),
            "changed literal inside CHECK")

    def test_whitespace_inside_literal_is_refused(self):
        self.assertRefusedUntouched(self.variant(
            "inbound_seen", "('pending','processed')", "('pending ','processed')"),
            "changed whitespace inside a literal")

    def test_extra_view_is_refused(self):
        self.assertRefusedUntouched(
            _v0_ddl() + ("CREATE VIEW leaked AS SELECT * FROM inbound_seen",), "extra view")

    def test_extra_trigger_on_non_v0_table_referencing_v0_is_refused(self):
        self.assertRefusedUntouched(_v0_ddl() + (
            "CREATE TRIGGER outbox_leak AFTER INSERT ON outbox BEGIN"
            " DELETE FROM inbound_seen; END",), "trigger on outbox touching inbound_seen")

    def test_view_named_like_an_internal_object_is_refused(self):
        # '_' is a LIKE wildcard: sqliteXleak must not be mistaken for sqlite_*.
        for name in ("sqliteXleak", "SQLITEXleak", "sqlite"):
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                self.assertRefusedUntouched(
                    _v0_ddl() + (f"CREATE VIEW {name} AS SELECT * FROM inbound_seen",),
                    f"view {name}")

    def test_extra_unrelated_table_is_refused(self):
        self.assertRefusedUntouched(
            _v0_ddl() + ("CREATE TABLE stray (x TEXT)",), "unexpected table")

    def test_fresh_database_starts_at_v1_and_reopen_is_idempotent(self):
        w = self.open_world()
        self.assertEqual(self.schema_version(), "e1.v1")
        self.assertEqual(w.router.handle(w.tg_msg("/stop")).outcome, "stopped")
        self.open_world()                                # second start: no migration, no error
        self.assertEqual(self.schema_version(), "e1.v1")


# ============================================================ refusal is atomic (Dolores, #28/#29 @ 0d8395b)

class AtomicRefusal(unittest.TestCase):
    """A refused database is left byte-identical: nothing at all is written."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "potter.db"

    def tearDown(self):
        self._tmp.cleanup()

    def c2_only(self, **meta):
        db = Database(self.path)                       # C2 only: no C3 outbox, no E1
        with db.tx() as c:
            for k, v in meta.items():
                c.execute("INSERT INTO meta (key, value) VALUES (?,?)", (k, v))
        return db

    def snapshot(self):
        c = sqlite3.connect(self.path)
        try:
            schema = c.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"
                               " ORDER BY type, name").fetchall()
            meta = c.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
            return schema, meta
        finally:
            c.close()

    def assertRefusedByteIdentical(self, db, why):
        from notifier import _schema, KillFlag
        from runtime import JournalUnavailable
        before = self.snapshot()
        refusals = [
            ("ensure", lambda: _schema.ensure(db)),
            ("ChannelAuth", lambda: ChannelAuth(db=db, transport_keys=TRANSPORT_KEYS,
                                                inbound_bindings={}, outbound_addresses={})),
            ("KillFlag", lambda: KillFlag(db)),
        ]
        # Notifier: auth from a separate, valid database; only `db` is under test.
        ok_db = Database(Path(self._tmp.name) / "other.db")
        ok_auth = ChannelAuth(db=ok_db, transport_keys=TRANSPORT_KEYS, inbound_bindings={},
                              outbound_addresses={JOSH: {"telegram": "a", "email": "b"}})
        refusals.append(("Notifier", lambda: Notifier(
            db, channels={"telegram": FakeChannel("telegram"), "email": FakeChannel("email")},
            auth=ok_auth)))
        for name, make in refusals:
            with self.subTest(path=name):
                with self.assertRaises(JournalUnavailable, msg=f"{why} via {name}"):
                    make()
                self.assertEqual(self.snapshot(), before, f"{why} via {name}: database mutated")

    def test_future_version_on_c2_only_db_is_refused_byte_identical(self):
        db = self.c2_only(notifier_schema="e1.v9")
        self.assertRefusedByteIdentical(db, "future notifier schema")
        tables = {r[1] for r in self.snapshot()[0]}
        self.assertNotIn("outbox", tables)

    def test_v0_mismatch_on_c2_only_db_is_refused_byte_identical(self):
        from notifier import _schema
        db = self.c2_only(notifier_schema="e1.v0")
        with db.tx() as c:                                    # v0 tables, but no C3 outbox
            for stmt in _schema.V0_SCHEMA:
                c.execute(stmt)
        self.assertRefusedByteIdentical(db, "e1.v0 catalog mismatch (no outbox)")

    def test_foreign_outbox_schema_is_refused_byte_identical(self):
        db = self.c2_only(outbox_schema="c3.v9")
        self.assertRefusedByteIdentical(db, "unsupported C3 outbox schema")

    def test_accepted_fresh_c2_only_db_is_set_up_in_one_step(self):
        from notifier import _schema
        db = self.c2_only()
        _schema.ensure(db)
        schema, meta = self.snapshot()
        self.assertIn("outbox", {r[1] for r in schema})
        self.assertIn(("notifier_schema", "e1.v1"), meta)
        self.assertIn(("outbox_schema", "c3.v0"), meta)
        before = self.snapshot()
        _schema.ensure(db)                                    # idempotent
        self.assertEqual(self.snapshot(), before)


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
