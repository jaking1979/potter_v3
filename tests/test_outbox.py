"""C3 merge evidence (decisions T4/T17, T19 PASS 2 items 1/3/7): crash between
commit and send yields exactly one send; an unknown-outcome routine message is
not resent; an urgent one is resent exactly once with the same idempotency key;
a rolled-back business transaction leaves no message; 8 concurrent relays send
each message at most once; ack suppresses sibling repeats; the watchdog sees a
stale backlog.

Offline only: the "channel" is a local SQLite file that logs every send call
and every delivery per idempotency key (no dedupe, so a double send is visible).
"""
import contextlib
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

from runtime import (ABANDONED_UNKNOWN, ACKED, ROUTINE, SENT, SUPPRESSED, URGENT,  # noqa: E402
                     Database, Outbox, OutboxConflict, OutboxMessage, RelayReport, SendFailed,
                     outbox_idempotency_key)

T = 1_800_000_000.0
TIMEOUT = 20.0
AFTER_TIMEOUT = T + TIMEOUT + 1
SPAWN = multiprocessing.get_context("spawn")


@contextlib.contextmanager
def _sql(path):
    c = sqlite3.connect(path, timeout=30)
    try:
        with c:
            yield c
    finally:
        c.close()


class Channel:
    """Fake notification channel. ``calls`` = send invocations (what "a send"
    means for the merge evidence); ``delivered`` = messages that actually landed."""

    def __init__(self, path):
        self.path = str(path)
        with _sql(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS calls (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                      " message_id TEXT NOT NULL, key TEXT NOT NULL, pid INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS delivered (key TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS fences (key TEXT PRIMARY KEY,"
                      " upto INTEGER NOT NULL)")

    def log_call(self, msg):
        with _sql(self.path) as c:
            c.execute("INSERT INTO calls (message_id, key, pid) VALUES (?,?,?)",
                      (msg.message_id, msg.idempotency_key, os.getpid()))

    def deliver(self, msg):
        """Provider side: a dispatch at or below the key's fence is refused."""
        with _sql(self.path) as c:
            row = c.execute("SELECT upto FROM fences WHERE key=?", (msg.idempotency_key,)).fetchone()
            if row and msg.dispatches <= row[0]:
                raise SendFailed(f"fenced: dispatch {msg.dispatches} <= {row[0]}")
            c.execute("INSERT INTO delivered VALUES (?)", (msg.idempotency_key,))

    def fence(self, msg):
        """Enforceable provider fence: refuse every dispatch so far of this key."""
        with _sql(self.path) as c:
            c.execute("INSERT INTO fences VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                      " upto = max(upto, excluded.upto)", (msg.idempotency_key, msg.dispatches))
        return True

    def send(self, msg):
        self.log_call(msg)
        self.deliver(msg)
        return {"delivered": msg.idempotency_key}

    def calls(self, message_id=None):
        with _sql(self.path) as c:
            if message_id is None:
                return c.execute("SELECT message_id, key FROM calls ORDER BY seq").fetchall()
            return [r[0] for r in c.execute("SELECT key FROM calls WHERE message_id=? ORDER BY seq",
                                            (message_id,))]

    def delivered(self, key=None):
        with _sql(self.path) as c:
            if key is None:
                return c.execute("SELECT COUNT(*) FROM delivered").fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM delivered WHERE key=?", (key,)).fetchone()[0]

    # Reconcilers: an honest read-back, and the common channel that cannot be queried.
    def reconcile(self, msg):
        return "succeeded" if self.delivered(msg.idempotency_key) else "failed"

    @staticmethod
    def blind(msg):
        return "still_unknown"


def msg(mid="esc:task:1:telegram", urgency=ROUTINE, notification_id=None, not_before=None):
    return OutboxMessage(mid, "telegram", "chat:josh", {"text": f"note {mid}"}, urgency,
                         notification_id, not_before)


def business_change(c, mid):
    c.execute("CREATE TABLE IF NOT EXISTS business (id TEXT PRIMARY KEY)")
    c.execute("INSERT INTO business VALUES (?)", (mid,))


def business_rows(db):
    with _sql(db) as c:
        c.execute("CREATE TABLE IF NOT EXISTS business (id TEXT PRIMARY KEY)")
        return [r[0] for r in c.execute("SELECT id FROM business")]


# -- spawned children (module level so `spawn` can import them) ---------------
def _child(db, chan_path, boundary, urgency, ready):
    database = Database(db)
    ob, ch = Outbox(database, dispatch_timeout_s=TIMEOUT), Channel(chan_path)
    m = msg(urgency=urgency)
    if boundary == "before_commit":
        with database.tx() as c:
            business_change(c, m.message_id)
            ob.enqueue(c, m, now=T)
            ready.set()
            time.sleep(600)                      # killed holding the uncommitted tx
    with database.tx() as c:
        business_change(c, m.message_id)
        ob.enqueue(c, m, now=T)
    if boundary == "after_commit":
        ready.set()
        time.sleep(600)                          # killed before any relay ran

    def send(q):
        ch.log_call(q)
        if boundary == "after_send":
            ch.deliver(q)                        # it landed...
        ready.set()
        time.sleep(600)                          # ...killed before the outcome is recorded
    ob.relay(send, Channel.blind, owner="relay-1", now=T)


def _relay_worker(db, chan_path, barrier, flaky, q):
    ob, ch = Outbox(db, dispatch_timeout_s=TIMEOUT), Channel(chan_path)
    seen = set()

    def send(m):
        ch.log_call(m)
        if flaky and m.message_id not in seen and m.dispatches == 1:
            seen.add(m.message_id)
            raise TimeoutError("channel timed out")   # ambiguous on first dispatch
        ch.deliver(m)
        return None
    barrier.wait(30)
    r = ob.relay(send, Channel.blind, owner=f"relay-{os.getpid()}", now=T)
    q.put((len(r.sent), len(r.resent_unknown), len(r.abandoned_unknown)))


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "potter.sqlite")
        self.database = Database(self.db)
        self.ob = Outbox(self.database, dispatch_timeout_s=TIMEOUT)
        self.ch = Channel(os.path.join(self.tmp.name, "channel.sqlite"))

    def enqueue(self, m, now=T):
        with self.database.tx() as c:
            business_change(c, m.message_id)
            return self.ob.enqueue(c, m, now=now)


# -- crash at each boundary ---------------------------------------------------
class CrashBoundaries(_Tmp):
    MID = "esc:task:1:telegram"

    def _crash(self, boundary, urgency):
        ready = SPAWN.Event()
        p = SPAWN.Process(target=_child, args=(self.db, self.ch.path, boundary, urgency, ready))
        p.start()
        self.assertTrue(ready.wait(60), f"child never reached {boundary}")
        p.kill()
        p.join(30)
        self.assertNotEqual(p.exitcode, 0)

    def _restart_relay(self, reconcile=Channel.blind, fence=None):
        ob = Outbox(Database(self.db), dispatch_timeout_s=TIMEOUT,   # fresh objects = restart
                    fence=fence)
        return ob, ob.relay(self.ch.send, reconcile, owner="relay-2", now=AFTER_TIMEOUT)

    def test_crash_before_commit_leaves_no_message_and_no_send(self):
        for urgency in (ROUTINE, URGENT):
            with self.subTest(urgency=urgency):
                self.setUp()
                self._crash("before_commit", urgency)
                ob, _ = self._restart_relay()
                self.assertIsNone(ob.get(self.MID))
                self.assertEqual((business_rows(self.db), self.ch.calls()), ([], []))

    def test_crash_between_commit_and_send_yields_exactly_one_send(self):
        for urgency in (ROUTINE, URGENT):
            with self.subTest(urgency=urgency):
                self.setUp()
                self._crash("after_commit", urgency)
                ob = Outbox(Database(self.db), dispatch_timeout_s=TIMEOUT)
                self.assertEqual(ob.get(self.MID).status, "pending")   # ID durable before send
                _, report = self._restart_relay()
                key = outbox_idempotency_key(self.MID)
                self.assertEqual(report.sent, [self.MID])
                self.assertEqual(self.ch.calls(self.MID), [key])
                self.assertEqual(self.ch.delivered(), 1)
                # Further relays never send it again.
                ob.relay(self.ch.send, Channel.blind, owner="relay-3", now=AFTER_TIMEOUT + 100)
                self.assertEqual(len(self.ch.calls()), 1)

    def test_routine_unknown_outcome_is_not_resent(self):
        for boundary in ("during_send", "after_send"):
            with self.subTest(boundary=boundary):
                self.setUp()
                self._crash(boundary, ROUTINE)
                ob = Outbox(Database(self.db), dispatch_timeout_s=TIMEOUT)
                self.assertEqual(ob.get(self.MID).status, "unknown")  # never left "pending"
                # Dispatcher's in-flight window still live: nobody touches it.
                ob.relay(self.ch.send, Channel.blind, owner="relay-2", now=T + 5)
                self.assertEqual(len(self.ch.calls()), 1)
                ob, report = self._restart_relay()
                self.assertEqual(report.abandoned_unknown, [self.MID])
                self.assertEqual(len(self.ch.calls()), 1)              # exactly one send
                m = ob.get(self.MID)
                self.assertEqual((m.status, m.dispatches, m.blind_resends),
                                 (ABANDONED_UNKNOWN, 1, 0))
                self.assertEqual([x.message_id for x in ob.needs_reconciliation()], [self.MID])
                ob.relay(self.ch.send, Channel.blind, owner="relay-3", now=AFTER_TIMEOUT + 999)
                self.assertEqual(len(self.ch.calls()), 1)

    def test_urgent_unknown_outcome_is_resent_exactly_once_with_same_key(self):
        for boundary in ("during_send", "after_send"):
            with self.subTest(boundary=boundary):
                self.setUp()
                self._crash(boundary, URGENT)
                ob, report = self._restart_relay()
                key = outbox_idempotency_key(self.MID)
                self.assertEqual(report.resent_unknown, [self.MID])
                self.assertEqual(self.ch.calls(self.MID), [key, key])  # identical key
                m = ob.get(self.MID)
                self.assertEqual((m.status, m.dispatches, m.blind_resends), (SENT, 2, 1))
                for later in (100, 10_000):
                    ob.relay(self.ch.send, Channel.blind, owner="relay-3", now=AFTER_TIMEOUT + later)
                self.assertEqual(len(self.ch.calls()), 2)

    def test_urgent_resend_that_crashes_is_never_resent_again(self):
        self._crash("during_send", URGENT)
        ob = Outbox(Database(self.db), dispatch_timeout_s=TIMEOUT)

        def dies(m):
            self.ch.log_call(m)
            raise TimeoutError("again")
        r = ob.relay(dies, Channel.blind, owner="relay-2", now=AFTER_TIMEOUT)
        self.assertEqual((r.resent_unknown, r.abandoned_unknown), ([self.MID], [self.MID]))
        ob.relay(self.ch.send, Channel.blind, owner="relay-3", now=AFTER_TIMEOUT + 999)
        self.assertEqual(len(self.ch.calls()), 2)
        self.assertEqual(ob.get(self.MID).status, ABANDONED_UNKNOWN)

    def test_unfenced_not_delivered_verdict_never_authorizes_redispatch(self):
        """An honest read-back that says "not delivered yet" is not enough after a
        restart: without a fence the earlier sender might still land it."""
        key = outbox_idempotency_key(self.MID)
        for urgency, status, deliveries, calls in ((ROUTINE, ABANDONED_UNKNOWN, 0, 1),
                                                   (URGENT, SENT, 1, 2)):   # T19 single resend
            with self.subTest(urgency=urgency):
                self.setUp()
                self._crash("during_send", urgency)
                ob, _ = self._restart_relay(self.ch.reconcile)
                m = ob.get(self.MID)
                self.assertEqual((m.status, self.ch.delivered(key), len(self.ch.calls())),
                                 (status, deliveries, calls))
                self.assertEqual(m.definite_failures, 0)

    def test_honest_reconcile_after_delivery_resolves_without_resend(self):
        for urgency in (ROUTINE, URGENT):
            with self.subTest(urgency=urgency):
                self.setUp()
                self._crash("after_send", urgency)
                ob, _ = self._restart_relay(self.ch.reconcile)
                m = ob.get(self.MID)
                self.assertEqual((m.status, m.blind_resends, len(self.ch.calls())), (SENT, 0, 1))

    def test_fenced_restart_gives_exactly_one_delivery_at_every_boundary(self):
        for boundary in ("after_commit", "during_send", "after_send"):
            for urgency in (ROUTINE, URGENT):
                with self.subTest(boundary=boundary, urgency=urgency):
                    self.setUp()
                    self._crash(boundary, urgency)
                    ob, _ = self._restart_relay(self.ch.reconcile, fence=self.ch.fence)
                    m = ob.get(self.MID)
                    self.assertEqual((m.status, m.blind_resends), (SENT, 0))
                    self.assertEqual(self.ch.delivered(outbox_idempotency_key(self.MID)), 1)


# -- enqueue semantics ---------------------------------------------------------
class Enqueue(_Tmp):
    def test_business_rollback_leaves_no_message(self):
        with self.assertRaises(RuntimeError):
            with self.database.tx() as c:
                business_change(c, "m1")
                self.ob.enqueue(c, msg("m1"), now=T)
                raise RuntimeError("business rule rejected the change")
        self.assertIsNone(self.ob.get("m1"))
        self.assertEqual(business_rows(self.db), [])
        self.ob.relay(self.ch.send, self.ch.reconcile, owner="r", now=T + 1)
        self.assertEqual(self.ch.calls(), [])

    def test_commit_writes_message_with_stable_key(self):
        q = self.enqueue(msg("m1", URGENT))
        self.assertEqual((q.status, q.urgency, q.idempotency_key, q.notification_id),
                         ("pending", URGENT, outbox_idempotency_key("m1"), "m1"))
        self.assertEqual(self.ob.get("m1"), q)

    def test_replayed_enqueue_is_one_message_and_conflict_is_refused(self):
        self.enqueue(msg("m1"))
        with self.database.tx() as c:
            self.ob.enqueue(c, msg("m1"), now=T + 5)
        with self.assertRaises(OutboxConflict), self.database.tx() as c:
            self.ob.enqueue(c, msg("m1", URGENT), now=T + 5)
        with _sql(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)

    def test_enqueue_requires_open_transaction_and_valid_urgency(self):
        with self.database.read() as c, self.assertRaises(ValueError):
            self.ob.enqueue(c, msg("m1"))
        with self.assertRaises(ValueError), self.database.tx() as c:
            self.ob.enqueue(c, OutboxMessage("m2", "telegram", "x", {}, "whenever"))


# -- relay behaviour -------------------------------------------------------------
class Relay(_Tmp):
    def test_dispatch_recorded_before_send_and_key_passed(self):
        self.enqueue(msg("m1"))
        seen = []

        def send(m):
            seen.append((self.ob.get(m.message_id).status, m.idempotency_key))
            return self.ch.send(m)
        self.ob.relay(send, self.ch.reconcile, owner="r", now=T)
        self.assertEqual(seen, [("unknown", outbox_idempotency_key("m1"))])
        self.assertEqual(self.ob.get("m1").status, SENT)

    def test_in_process_ambiguous_error_routine_vs_urgent(self):
        self.enqueue(msg("r1", ROUTINE))
        self.enqueue(msg("u1", URGENT))
        first = set()

        def flaky(m):
            self.ch.log_call(m)
            if m.message_id not in first:
                first.add(m.message_id)
                raise ConnectionResetError("lost response")
            self.ch.deliver(m)
        r = self.ob.relay(flaky, Channel.blind, owner="r", now=T)
        self.assertEqual((r.sent, r.resent_unknown, r.abandoned_unknown), (["u1"], ["u1"], ["r1"]))
        self.assertEqual(len(self.ch.calls("r1")), 1)
        self.assertEqual(len(self.ch.calls("u1")), 2)

    def test_reconcile_errors_and_bad_verdicts_count_as_unknown(self):
        for i, rec in enumerate((lambda m: "maybe", lambda m: 1 / 0)):
            mid = f"r{i}"
            self.enqueue(msg(mid))

            def boom(m):
                self.ch.log_call(m)
                raise TimeoutError()
            self.ob.relay(boom, rec, owner="r", now=T)
            self.assertEqual(self.ob.get(mid).status, ABANDONED_UNKNOWN)
            self.assertEqual(len(self.ch.calls(mid)), 1)

    def test_definite_failure_retries_with_backoff_then_fails(self):
        ob = Outbox(self.database, max_attempts=2, backoff_s=10)
        self.enqueue(msg("m1"))

        def reject(m):
            self.ch.log_call(m)
            raise SendFailed("400 chat not found")
        self.assertEqual(ob.relay(reject, self.ch.reconcile, owner="r", now=T).retry_scheduled, ["m1"])
        self.assertEqual(ob.relay(reject, self.ch.reconcile, owner="r", now=T + 5).failed, [])  # backoff
        self.assertEqual(ob.relay(reject, self.ch.reconcile, owner="r", now=T + 10).failed, ["m1"])
        self.assertEqual(ob.get("m1").status, "failed")
        self.assertEqual(set(self.ch.calls("m1")), {outbox_idempotency_key("m1")})
        self.assertEqual([m.message_id for m in ob.needs_reconciliation()], ["m1"])

    def test_urgent_sent_before_routine(self):
        self.enqueue(msg("a-routine", ROUTINE))
        self.enqueue(msg("b-urgent", URGENT))
        self.ob.relay(self.ch.send, self.ch.reconcile, owner="r", now=T)
        self.assertEqual([mid for mid, _ in self.ch.calls()], ["b-urgent", "a-routine"])

    def test_not_before_is_respected(self):
        self.enqueue(msg("later", not_before=T + 60))
        self.assertEqual(self.ob.relay(self.ch.send, self.ch.reconcile, owner="r", now=T).sent, [])
        self.assertEqual(self.ob.relay(self.ch.send, self.ch.reconcile, owner="r", now=T + 60).sent,
                         ["later"])

    def test_long_drain_refreshes_lease_for_every_claim(self):
        """Regression: timeout 10 s, three 6 s sends. With one clock sample per
        relay() the third send ran on a lease that expired at T+10 while the
        clock read T+12, and a second relay could claim and reconcile it."""
        clock = [T]
        ob = Outbox(self.database, clock=lambda: clock[0], dispatch_timeout_s=10)
        other = Outbox(self.database, clock=lambda: clock[0], dispatch_timeout_s=10)
        for i in range(3):
            self.enqueue(msg(f"m{i}"), now=T)
        probes = []

        def slow_send(m):
            self.ch.log_call(m)
            clock[0] += 6                                    # the send takes 6 s
            row = ob.get(m.message_id)
            probes.append(row.in_flight_until > clock[0])    # lease still valid at its end
            if m.message_id == "m2":
                # Only m2 is left; a concurrent relay may claim it only if its lease expired.
                r = other.relay(self.ch.send, Channel.blind, owner="relay-2")
                probes.append(r == RelayReport())
            self.ch.deliver(m)

        report = ob.relay(slow_send, Channel.blind, owner="relay-1")  # no explicit now
        self.assertEqual(sorted(report.sent), ["m0", "m1", "m2"])
        self.assertEqual(probes, [True] * 4)
        self.assertEqual(sorted(mid for mid, _ in self.ch.calls()), ["m0", "m1", "m2"])
        for i in range(3):
            self.assertEqual(self.ch.delivered(outbox_idempotency_key(f"m{i}")), 1)
        m2 = ob.get("m2")
        self.assertEqual((m2.status, m2.dispatches, m2.sent_at), (SENT, 1, T + 18))  # claimed at T+12, lease to T+22

    def test_expired_lease_never_authorizes_routine_resend(self):
        self.enqueue(msg("r1", ROUTINE))
        with self.database.tx() as c:          # a dispatcher claimed it, then went silent
            c.execute("UPDATE outbox SET status='unknown', dispatches=1, in_flight_owner='dead',"
                      " in_flight_until=?", (T + TIMEOUT,))
        for later in (TIMEOUT + 1, 10 * TIMEOUT, 1000 * TIMEOUT):
            self.ob.relay(self.ch.send, Channel.blind, owner="r", now=T + later)
        self.assertEqual(self.ch.calls(), [])
        m = self.ob.get("r1")
        self.assertEqual((m.status, m.dispatches), (ABANDONED_UNKNOWN, 1))
        self.assertEqual([x.message_id for x in self.ob.needs_reconciliation()], ["r1"])

    def test_blind_resend_cap_is_a_database_invariant(self):
        self.enqueue(msg("u1", URGENT))
        with self.assertRaises(sqlite3.IntegrityError), _sql(self.db) as c:
            c.execute("UPDATE outbox SET blind_resends=2")


# -- concurrency -------------------------------------------------------------------
class ConcurrentRelays(_Tmp):
    N = 24

    def _race(self, flaky):
        for i in range(self.N):
            self.enqueue(msg(f"m{i:02d}", URGENT if i % 2 else ROUTINE))
        barrier, q = SPAWN.Barrier(8), SPAWN.Queue()
        procs = [SPAWN.Process(target=_relay_worker, args=(self.db, self.ch.path, barrier, flaky, q))
                 for _ in range(8)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(120)
            self.assertEqual(p.exitcode, 0)
        return [q.get(timeout=5) for _ in procs]

    def test_eight_relays_send_each_message_exactly_once(self):
        self._race(flaky=False)
        calls = self.ch.calls()
        self.assertEqual(len(calls), self.N)
        self.assertEqual(len({mid for mid, _ in calls}), self.N)
        for mid, key in calls:
            self.assertEqual(key, outbox_idempotency_key(mid))
            self.assertEqual(self.ob.get(mid).status, SENT)

    def test_eight_relays_with_unknown_outcomes(self):
        results = self._race(flaky=True)
        for i in range(self.N):
            mid = f"m{i:02d}"
            keys = self.ch.calls(mid)
            if i % 2:   # urgent: twice, only via the unknown-resend path, same key
                self.assertEqual(keys, [outbox_idempotency_key(mid)] * 2)
                m = self.ob.get(mid)
                self.assertEqual((m.status, m.blind_resends, m.dispatches), (SENT, 1, 2))
            else:       # routine: once, abandoned
                self.assertEqual(len(keys), 1)
                self.assertEqual(self.ob.get(mid).status, ABANDONED_UNKNOWN)
        self.assertEqual(sum(r[1] for r in results), self.N // 2)


# -- ack suppression + watchdog ------------------------------------------------------
class AckAndWatchdog(_Tmp):
    def test_ack_suppresses_repeats_on_other_channels(self):
        n = "notif:escalation:1"
        self.enqueue(OutboxMessage("n1:telegram", "telegram", "chat:josh", {"t": 1}, URGENT, n))
        self.enqueue(OutboxMessage("n1:email", "email", "josh@example.invalid", {"t": 1}, URGENT, n,
                                   T + 600))                     # repeat if not acked in 10 min
        self.ob.relay(self.ch.send, self.ch.reconcile, owner="r", now=T)
        self.assertEqual(self.ch.calls("n1:email"), [])
        self.assertEqual(self.ob.ack("n1:telegram", now=T + 60).status, ACKED)
        r = self.ob.relay(self.ch.send, self.ch.reconcile, owner="r", now=T + 600)
        self.assertEqual((r.sent, self.ch.calls("n1:email")), ([], []))
        self.assertEqual(self.ob.get("n1:email").status, SUPPRESSED)
        self.assertEqual(self.ob.ack("n1:telegram", now=T + 999).acked_at, T + 60)   # idempotent

    def test_ack_stops_urgent_resend_and_resolves_unknown(self):
        self.enqueue(msg("u1", URGENT))
        ob = Outbox(self.database, dispatch_timeout_s=TIMEOUT)
        with self.database.tx() as c:          # simulate a dead dispatcher's claim
            c.execute("UPDATE outbox SET status='unknown', dispatches=1, in_flight_owner='dead',"
                      " in_flight_until=?", (T + TIMEOUT,))
        ob.ack("u1", now=T + 1)
        r = ob.relay(self.ch.send, Channel.blind, owner="r", now=AFTER_TIMEOUT)
        self.assertEqual((r.resent_unknown, self.ch.calls()), ([], []))
        self.assertEqual(ob.get("u1").status, ACKED)

    def test_ack_unknown_message_raises(self):
        with self.assertRaises(KeyError):
            self.ob.ack("nope")

    def test_stale_backlog_flags_old_unsent_messages(self):
        self.enqueue(msg("a-done"), now=T)
        self.enqueue(msg("b-old"), now=T)
        self.enqueue(msg("c-new"), now=T + 1700)
        self.ob.relay(self.ch.send, self.ch.reconcile, owner="r", now=T, limit=1)  # sends a-done
        self.assertEqual(self.ob.get("a-done").status, SENT)
        stale = [m.message_id for m in self.ob.stale_backlog(now=T + 1801)]   # default 30 min
        self.assertEqual(stale, ["b-old"])
        self.assertEqual(self.ob.stale_backlog(now=T + 1801, max_age=3600), [])
        # A dispatch stuck in "unknown" also counts as backlog.
        with self.database.tx() as c:
            c.execute("UPDATE outbox SET status='unknown' WHERE message_id='c-new'")
        self.assertEqual([m.message_id for m in self.ob.stale_backlog(now=T + 3501)],
                         ["b-old", "c-new"])


# -- claim deadlines under lock contention, paused senders, fences -----------------
import threading  # noqa: E402


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class LeaseSafety(_Tmp):
    def test_claim_under_lock_contention_is_not_born_expired(self):
        """Dolores's probe: relay A waits on the write lock while the clock
        passes its timeout; its claim must not commit already expired, so a
        second relay cannot claim and resend it first."""
        clock = _Clock(T)
        a = Outbox(self.database, clock=clock, dispatch_timeout_s=10)
        b = Outbox(self.database, clock=clock, dispatch_timeout_s=10)
        self.enqueue(msg("u1", URGENT))
        in_send, release_send = threading.Event(), threading.Event()
        self.addCleanup(release_send.set)        # never leave the sender parked
        seen = {}

        def slow_send(m):
            self.ch.log_call(m)
            seen["until"], seen["clock"] = a.get("u1").in_flight_until, clock()
            in_send.set()
            release_send.wait(30)
            self.ch.deliver(m)

        holder = sqlite3.connect(self.db, timeout=30, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")                  # hold the write lock
        t = threading.Thread(target=lambda: seen.setdefault(
            "a", a.relay(slow_send, Channel.blind, owner="relay-A")))
        t.start()
        time.sleep(0.5)                                    # A is queued on the lock
        clock.t = T + 100                                  # ...while time passes its timeout
        holder.execute("COMMIT")
        holder.close()
        self.assertTrue(in_send.wait(30))
        self.assertGreater(seen["until"], seen["clock"])   # A's claim is live, not born expired
        rb = b.relay(self.ch.send, Channel.blind, owner="relay-B")
        self.assertEqual(rb, RelayReport())                # B finds nothing it may claim
        release_send.set()
        t.join(30)
        self.assertEqual(seen["a"].sent, ["u1"])
        self.assertEqual(self.ch.calls("u1"), [outbox_idempotency_key("u1")])
        m = a.get("u1")
        self.assertEqual((m.status, m.dispatches, m.blind_resends), (SENT, 1, 0))

    def _paused_sender(self, urgency, fence):
        """Relay A claims and starts sending, then stalls past its lease. Relay B
        reconciles (honestly: "not delivered yet"). Then A wakes up and delivers."""
        clock = _Clock(T)
        a = Outbox(self.database, clock=clock, dispatch_timeout_s=10)
        b = Outbox(self.database, clock=clock, dispatch_timeout_s=10, fence=fence)
        self.enqueue(msg("m1", urgency))
        out = {}

        def paused_send(m):
            self.ch.log_call(m)
            clock.t = T + 60                       # A stalls far past its 10 s lease
            out["b"] = b.relay(self.ch.send, self.ch.reconcile, owner="relay-B")
            try:
                self.ch.deliver(m)                 # ...then A's request lands late
                out["late"] = "delivered"
            except SendFailed:
                out["late"] = "refused"
                raise
        a.relay(paused_send, Channel.blind, owner="relay-A")
        return a.get("m1"), out

    def test_paused_sender_routine_unfenced_is_not_redispatched(self):
        m, out = self._paused_sender(ROUTINE, fence=None)
        self.assertEqual(out["b"].abandoned_unknown, ["m1"])
        self.assertEqual(out["late"], "delivered")
        self.assertEqual(len(self.ch.calls("m1")), 1)
        self.assertEqual(self.ch.delivered(), 1)           # exactly one, despite B
        self.assertEqual((m.status, m.dispatches), (ABANDONED_UNKNOWN, 1))

    def test_paused_sender_urgent_unfenced_keeps_t19_cap(self):
        m, out = self._paused_sender(URGENT, fence=None)
        self.assertEqual(out["b"].resent_unknown, ["m1"])
        self.assertEqual(len(self.ch.calls("m1")), 2)       # never more than one resend
        self.assertLessEqual(self.ch.delivered(), 2)
        self.assertEqual((m.status, m.dispatches, m.blind_resends, m.definite_failures),
                         (SENT, 2, 1, 0))

    def test_paused_sender_with_provider_fence_delivers_exactly_once(self):
        for urgency in (ROUTINE, URGENT):
            with self.subTest(urgency=urgency):
                self.setUp()
                m, out = self._paused_sender(urgency, fence=self.ch.fence)
                self.assertEqual(out["b"].sent, ["m1"])          # fenced redispatch, same key
                self.assertEqual(out["late"], "refused")         # provider refuses A
                self.assertEqual(self.ch.delivered(), 1)
                self.assertEqual(set(self.ch.calls("m1")), {outbox_idempotency_key("m1")})
                self.assertEqual((m.status, m.dispatches, m.blind_resends), (SENT, 2, 0))

    def test_fence_errors_or_non_true_answers_refuse(self):
        for i, fence in enumerate((lambda m: 1 / 0, lambda m: "yes", lambda m: None)):
            with self.subTest(i=i):
                self.setUp()
                m, out = self._paused_sender(ROUTINE, fence=fence)
                self.assertEqual(out["b"].abandoned_unknown, ["m1"])
                self.assertEqual(len(self.ch.calls()), 1)

    def test_clock_regression_cannot_write_an_expired_deadline(self):
        ahead, behind = _Clock(T + 100), _Clock(T)
        a = Outbox(self.database, clock=ahead, dispatch_timeout_s=10)
        self.enqueue(msg("m1", URGENT))
        self.enqueue(msg("m2", URGENT))
        a.relay(self.ch.send, Channel.blind, owner="relay-A", limit=1)    # m1 at T+100
        # Restart with a clock that stepped back 100 s; it claims m2 and stalls.
        r = Outbox(Database(self.db), clock=behind, dispatch_timeout_s=10)
        seen = {}

        def stalled(m):
            self.ch.log_call(m)
            seen["until"] = r.get("m2").in_flight_until
            seen["c"] = Outbox(self.database, clock=_Clock(T + 105), dispatch_timeout_s=10) \
                .relay(self.ch.send, Channel.blind, owner="relay-C")
            self.ch.deliver(m)
        r.relay(stalled, Channel.blind, owner="relay-R")
        self.assertEqual(seen["until"], T + 110)          # not T + 10
        self.assertEqual(seen["c"], RelayReport())         # C at T+105 sees a live claim
        self.assertEqual(len(self.ch.calls("m2")), 1)
        self.assertEqual(r.get("m2").dispatches, 1)
