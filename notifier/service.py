"""The one notifier (T19 PASS 2 items 1, 3, 5, 6, 7).

Every message to Josh - brief, alert, digest, approval prompt, job result,
long-work progress, watchdog alert, /stop confirmation - is enqueued here, in
the caller's transaction, through the C3 outbox. Nothing else holds a channel.

* Jobs: each scheduled job declares ``JobSpec(name, delivery="josh"|"silent")``
  and receives a ``JobHandle``. The handle holds only an opaque handle id and
  its job's name/target; the notifier, outbox and channels are looked up in a
  private registry on each call, so nothing reachable from the handle (or from
  a ``LongWork`` it returns) leads to a channel. ``notifier.lint`` fails the
  build on channel/outbox/network imports outside ``notifier/`` and on
  reach-through (``_n``, ``_channels``, ``__closure__``, ``.send(``, dynamic
  ``getattr``, ``gc``/``inspect``). In-process Python is not a hard isolation
  boundary: this is defence in depth, and hard enforcement is the C8 process
  boundary (jobs run where the notifier's channels do not exist).
* ID before send: the outbox row (stable ``message_id`` = ``<notification
  id>:<channel>``) is committed with the business change, before any send.
  Unknown outcomes reuse C3: routine not resent, urgent resent once.
* Ack on any channel: all copies of one notification share its
  ``notification_id``; ``ack`` marks one and C3 suppresses the rest.
* Outage: ``failure_threshold`` (3) consecutive primary (Telegram) failures
  mark it down. While down, urgent notifications are copied to the fallback
  channel (email) and routine ones wait: their primary rows are deferred
  (no attempts, so no failure count) until a probe succeeds.
* Watchdog: ``watchdog()`` reports C3 ``stale_backlog`` older than 30 min and
  raises one urgent alert per 30-min bucket.
* /stop: ``stop()`` sets the durable kill flag, cancels (``suppressed``) every
  queued or unresolved send, cancels pending approvals, and enqueues one
  confirmation listing what was cancelled. Boundary: every send passes a gate
  that checks the flag and records the send in ``notifier_sending`` in ONE
  ``BEGIN IMMEDIATE`` transaction; ``stop()`` sets the flag under the same
  write lock and then waits until every recorded send has returned. So a
  send already past the gate may complete (before ``stop()`` returns), and no
  send that has not passed the gate can start afterwards.
  Immediately before ``channel.send`` the sender re-checks, in its own
  transaction, that its gate row still exists, that it is younger than the
  dispatch timeout and that the kill flag is clear; otherwise it removes its
  row and aborts.

  The contract (honest: Telegram/email cannot be fenced at the provider):
  no send starts its final recheck after ``stop()`` commits; a sender
  already past its final recheck may still deliver once; ``stop()`` returns
  every such sender as unconfirmed (and expired if aged). ``stop()`` waits
  (up to ``stop_wait_s``) for unexpired rows, then returns EVERY row still
  present in ``StopReport.unconfirmed_in_flight`` - age never removes one;
  rows past the dispatch timeout are also listed in ``expired_in_flight``.
  An empty ``unconfirmed_in_flight`` is the only all-clear.
* Long work: ``LongWork`` sends one acknowledgement after 30 s, an update only
  when the state changes, and a final message with the receipt link.

Staging-only (T22): channels are injected; only fakes exist in this package.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from identity.seed import JOSH
from runtime import (ABANDONED_UNKNOWN, ROUTINE, SUPPRESSED, URGENT, Database, Outbox,
                     OutboxMessage, QueuedMessage, RelayReport, SendFailed)
from runtime.db import as_database

from . import _schema
from .auth import AlreadyProcessed, AuthenticatedSender, ChannelAuth
from .channels import EMAIL, TELEGRAM, Channel
from .kill import KillFlag
from .traces import Sink, emit_trace

PENDING, UNKNOWN, FAILED, ACKED = "pending", "unknown", "failed", "acked"

JOB_KINDS = frozenset({"brief", "alert", "digest", "job_result"})
INTERNAL_KINDS = frozenset({"approval", "work_ack", "work_update", "work_final", "watchdog",
                            "stop_ack"})
DELIVERY_TARGETS = frozenset({"josh", "silent"})
LONG_WORK_ACK_S = 30.0
STOP_PREFIX = "stop:"


class NotifierError(Exception):
    pass


class Stopped(NotifierError):
    """The kill flag is set: nothing new may be queued or sent."""


@dataclass(frozen=True)
class JobSpec:
    """Every scheduled job declares where its messages go (T19 item 1)."""
    name: str
    delivery: str

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("job name required")
        if self.delivery not in DELIVERY_TARGETS:
            raise ValueError("job must declare delivery 'josh' or 'silent'")


@dataclass
class RunReport:
    sent: list[str] = field(default_factory=list)
    retry_scheduled: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    resent_unknown: list[str] = field(default_factory=list)
    abandoned_unknown: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    fallback_enqueued: list[str] = field(default_factory=list)

    def merge(self, r: RelayReport) -> bool:
        moved = False
        for name in ("sent", "retry_scheduled", "failed", "resent_unknown",
                     "abandoned_unknown", "suppressed"):
            items = getattr(r, name)
            getattr(self, name).extend(items)
            moved = moved or bool(items)
        return moved


@dataclass(frozen=True)
class StopReport:
    cancelled_messages: tuple[str, ...]
    cancelled_notifications: tuple[str, ...]
    cancelled_approvals: tuple[str, ...]
    already_stopped: bool
    reply_notification_id: str
    unconfirmed_in_flight: tuple[str, ...] = ()   # every unreturned send, never filtered by age
    expired_in_flight: tuple[str, ...] = ()       # subset past dispatch timeout (still unconfirmed)

    @property
    def all_clear(self) -> bool:
        return not self.unconfirmed_in_flight


class Notifier:
    def __init__(self, db: Database | str, *, channels: Mapping[str, Channel], auth: ChannelAuth,
                 primary: str = TELEGRAM, fallback: str = EMAIL, recipient: str = JOSH,
                 failure_threshold: int = 3, probe_interval_s: float = 60.0,
                 max_attempts: int = 8, backoff_s: float = 30.0,
                 dispatch_timeout_s: float = 300.0, clock: Callable[[], float] = time.time,
                 trace_sink: Sink | None = None, environment: str = "synthetic",
                 owner: str = "e1-notifier", stop_wait_s: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep):
        if failure_threshold < 1 or max_attempts <= failure_threshold:
            # Routine messages must outlive the outage detector: they wait, not fail.
            raise ValueError("need 1 <= failure_threshold < max_attempts")
        if primary not in channels or fallback not in channels or primary == fallback:
            raise ValueError("primary and fallback must be distinct configured channels")
        self.db = as_database(db)
        self.outbox = Outbox(self.db, clock=clock, dispatch_timeout_s=dispatch_timeout_s,
                             max_attempts=max_attempts, backoff_s=backoff_s)
        _schema.ensure(self.db)
        self.kill = KillFlag(self.db)
        self._channels = dict(channels)
        self._auth = auth
        self._primary, self._fallback, self._recipient = primary, fallback, recipient
        self._threshold = failure_threshold
        self._probe_interval = probe_interval_s
        self._clock = clock
        self._sink = trace_sink
        self._env = environment
        self._owner = owner
        self._run_now: float | None = None
        self._claimed = False
        self._dispatch_timeout = dispatch_timeout_s
        self._stop_wait = stop_wait_s
        self._sleep = sleep
        self._local = threading.local()          # this thread's sends past the gate
        # Test seams for the /stop race (deterministic pauses); None in production.
        self._hook_before_gate: Callable[[QueuedMessage], None] | None = None
        self._hook_after_gate: Callable[[QueuedMessage], None] | None = None
        self._hook_after_recheck: Callable[[QueuedMessage], None] | None = None
        for ch in (primary, fallback):
            if auth.address(recipient, ch) is None:
                raise ValueError(f"no {ch} address for {recipient}")

    # ------------------------------------------------------------------ enqueue
    def _now(self, now: float | None = None) -> float:
        if now is not None:
            return now
        return self._run_now if self._run_now is not None else self._clock()

    def handle_for(self, spec: JobSpec) -> "JobHandle":
        if not isinstance(spec, JobSpec):
            raise TypeError("jobs must declare a JobSpec")
        return JobHandle(self, spec)

    def enqueue(self, tx: sqlite3.Connection, *, notification_id: str, kind: str, text: str,
                urgency: str = ROUTINE, extra: Mapping[str, Any] | None = None,
                channel: str | None = None, now: float | None = None) -> str:
        """Write one notification for Josh in the caller's open transaction.
        Raises ``Stopped`` if the kill flag is set (except the /stop reply)."""
        if kind not in JOB_KINDS | INTERNAL_KINDS:
            raise ValueError("unknown notification kind")
        if not notification_id or ":" not in notification_id:
            raise ValueError("notification_id must be namespaced, e.g. 'job:<name>:<key>'")
        if kind != "stop_ack" and self.kill.is_set(tx):
            raise Stopped("kill flag set")
        now = self._now(now)
        body = {"kind": kind, "text": text, "notification_id": notification_id}
        if extra:
            body.update({k: v for k, v in extra.items() if k not in body})
        ch = channel or self._primary
        self._put(tx, notification_id, ch, body, urgency, now)
        if urgency == URGENT and ch == self._primary and self._is_down(tx):
            self._put(tx, notification_id, self._fallback, body, urgency, now)
        return notification_id

    def _put(self, tx, nid: str, channel: str, body: dict, urgency: str, now: float) -> None:
        addr = self._auth.address(self._recipient, channel)
        if addr is None:
            raise NotifierError(f"no {channel} address for recipient")
        self.outbox.enqueue(tx, OutboxMessage(f"{nid}:{channel}", channel, addr, body, urgency,
                                              notification_id=nid), now=now)

    # ------------------------------------------------------------------ relay
    def run(self, now: float | None = None, *, max_messages: int = 10_000) -> RunReport:
        """Deliver everything due, one claim at a time so the kill flag and
        primary health are re-checked between messages."""
        report = RunReport()
        self._run_now = now
        try:
            for _ in range(max_messages):
                self._housekeeping(report)
                self._claimed = False
                r = self.outbox.relay(self._send, self._reconcile, owner=self._owner,
                                      now=now, limit=1)
                # A claim either suppresses (reported) or calls send/reconcile
                # (flagged), even when its settle is pre-empted by /stop or ack.
                if not (report.merge(r) or self._claimed):
                    break
            self._housekeeping(report)
        finally:
            self._run_now = None
        self._trace_sends(report, self._now(now))
        return report

    def _inflight(self) -> set[str]:
        got = getattr(self._local, "ids", None)
        if got is None:
            got = self._local.ids = set()
        return got

    def _send(self, msg: QueuedMessage) -> Any:
        self._claimed = True
        if self._hook_before_gate is not None:
            self._hook_before_gate(msg)
        channel = self._channels.get(msg.channel)
        if channel is None:
            raise SendFailed("no such channel")
        # The gate: kill check + in-flight record, atomically (see module doc).
        with self.db.tx() as c:
            if msg.body.get("kind") != "stop_ack" and self.kill.is_set(c):
                raise SendFailed("stopped")      # row stays suppressed by the sweep
            c.execute("INSERT OR REPLACE INTO notifier_sending (message_id, owner, since)"
                      " VALUES (?,?,?)", (msg.message_id,
                                          f"{self._owner}:{os.getpid()}:{threading.get_ident()}",
                                          self._now()))
        mine = self._inflight()
        mine.add(msg.message_id)
        is_primary = msg.channel == self._primary
        try:
            if self._hook_after_gate is not None:
                self._hook_after_gate(msg)
            self._recheck(msg)                   # last word before the channel call
            if self._hook_after_recheck is not None:
                self._hook_after_recheck(msg)
            try:
                result = channel.send(msg)
            except BaseException:
                if is_primary:
                    self._record_health(ok=False)
                raise
            if is_primary:
                self._record_health(ok=True)
            return result
        finally:
            mine.discard(msg.message_id)
            try:
                with self.db.tx() as c:
                    c.execute("DELETE FROM notifier_sending WHERE message_id=?",
                              (msg.message_id,))
            except Exception:
                pass   # a leftover row only delays stop() until it ages out

    def _recheck(self, msg: QueuedMessage) -> None:
        """Abort unless this send's gate row is still ours, unexpired, and the
        kill flag is clear (stop_ack exempt from the flag). The finally-block
        in ``_send`` removes the row on abort."""
        with self.db.tx() as c:
            row = c.execute("SELECT since FROM notifier_sending WHERE message_id=?",
                            (msg.message_id,)).fetchone()
            if row is None:
                raise SendFailed("aborted: gate record missing")
            if self._now() >= row[0] + self._dispatch_timeout:
                raise SendFailed("aborted: gate expired")
            if msg.body.get("kind") != "stop_ack" and self.kill.is_set(c):
                raise SendFailed("aborted: stopped after gate")

    def _reconcile(self, msg: QueuedMessage) -> str:
        self._claimed = True
        channel = self._channels.get(msg.channel)
        return channel.reconcile(msg) if channel is not None else "still_unknown"

    # ------------------------------------------------------------------ health / fallback
    _HEALTH = "health:primary"

    def _health(self, c: sqlite3.Connection) -> dict:
        raw = _schema.get_state(c, self._HEALTH)
        return json.loads(raw) if raw else {"failures": 0, "down_since": None, "last_probe": None}

    def _is_down(self, c: sqlite3.Connection) -> bool:
        return self._health(c)["down_since"] is not None

    def primary_down(self) -> bool:
        with self.db.read() as c:
            return self._is_down(c)

    def _record_health(self, *, ok: bool) -> None:
        now = self._now()
        with self.db.tx() as c:
            h = self._health(c)
            if ok:
                h = {"failures": 0, "down_since": None, "last_probe": None}
            else:
                h["failures"] += 1
                if h["failures"] >= self._threshold and h["down_since"] is None:
                    h["down_since"], h["last_probe"] = now, now
            _schema.set_state(c, self._HEALTH, json.dumps(h, sort_keys=True))

    def _housekeeping(self, report: RunReport) -> None:
        now = self._now()
        killed = self.kill.is_set()
        if killed:
            # Everything but the /stop confirmation is cancelled; the outage
            # handling below still runs so that confirmation can fall back.
            self._sweep(now)
        with self.db.read() as c:
            h = self._health(c)
        if h["down_since"] is None:
            return
        if h["last_probe"] is None or now - h["last_probe"] >= self._probe_interval:
            try:
                up = self._channels[self._primary].probe() is True
            except Exception:
                up = False
            with self.db.tx() as c:
                if up:
                    _schema.set_state(c, self._HEALTH, json.dumps(
                        {"failures": 0, "down_since": None, "last_probe": None}, sort_keys=True))
                    # Deferred primary messages become due now; acked ones get suppressed.
                    c.execute("UPDATE outbox SET next_attempt_at=?, updated_at=? WHERE channel=?"
                              " AND status=? AND next_attempt_at>?",
                              (now, now, self._primary, PENDING, now))
                    return
                h["last_probe"] = now
                _schema.set_state(c, self._HEALTH, json.dumps(h, sort_keys=True))
        with self.db.tx() as c:
            # Routine (and urgent) primary rows wait without burning attempts.
            until = now + self._probe_interval
            c.execute("UPDATE outbox SET next_attempt_at=?, updated_at=? WHERE channel=?"
                      " AND status=? AND next_attempt_at<?",
                      (until, now, self._primary, PENDING, until))
            # Urgent notifications not yet delivered go via the fallback channel.
            # While stopped, only the explicitly permitted stop_ack may fall back.
            only_stop = " AND o.notification_id LIKE ?" if killed else ""
            rows = c.execute(
                "SELECT o.notification_id, o.body FROM outbox o WHERE o.channel=? AND"
                " o.urgency=? AND o.status IN (?,?,?,?) AND NOT EXISTS (SELECT 1 FROM outbox s"
                " WHERE s.notification_id=o.notification_id AND (s.channel=? OR"
                " s.acked_at IS NOT NULL))" + only_stop,
                (self._primary, URGENT, PENDING, UNKNOWN, FAILED, ABANDONED_UNKNOWN,
                 self._fallback) + ((STOP_PREFIX + "%",) if killed else ())).fetchall()
            for nid, body in rows:
                self._put(c, nid, self._fallback, json.loads(body), URGENT, now)
                report.fallback_enqueued.append(nid)

    # ------------------------------------------------------------------ ack / stop / watchdog
    def ack(self, notification_id: str, sender: AuthenticatedSender,
            now: float | None = None) -> bool:
        """Josh acknowledged ``notification_id`` on ``sender.channel``. Marks
        that copy (or, if none was sent there, the primary copy) acked; C3
        suppresses every undelivered sibling on every channel."""
        if not self._auth.vouches(sender) or sender.principal != self._recipient:
            return False
        cmd, target = sender.command()
        if cmd != "/ack" or target != notification_id:
            return False                      # voucher was minted for another command
        candidates = [f"{notification_id}:{sender.channel}",
                      f"{notification_id}:{self._primary}", f"{notification_id}:{self._fallback}"]
        for mid in candidates:
            if self.outbox.get(mid) is not None:
                self.outbox.ack(mid, now=self._now(now))
                return True
        return False

    def stop(self, sender: AuthenticatedSender, now: float | None = None) -> StopReport:
        """/stop (T19 item 6). One transaction: set the kill flag, cancel queued
        and unresolved sends, cancel pending approvals, enqueue the reply."""
        if not self._auth.vouches(sender) or sender.principal != self._recipient:
            raise PermissionError("stop needs an authenticated allowlisted sender")
        if sender.command()[0] != "/stop":
            raise PermissionError("voucher was not minted for /stop")
        now = self._now(now)
        with self.db.tx() as c:
            # Mark the /stop update processed in the SAME transaction as its
            # effect: a crash before commit leaves it pending (redelivery
            # resumes it); a duplicate delivery rolls back here.
            if not self._auth.complete(c, sender):
                raise AlreadyProcessed("this /stop was already executed")
            first = self.kill.set(c, by=sender.principal, at=now)
            msgs = self._sweep_tx(c, now)
            appr = [r[0] for r in c.execute(
                "SELECT approval_id FROM approval_requests WHERE status='pending'"
                " ORDER BY approval_id")]
            c.execute("UPDATE approval_requests SET status='cancelled', status_reason='stop'"
                      " WHERE status='pending'")
            nids = sorted({m.rsplit(":", 1)[0] for m in msgs})
            reply = f"{STOP_PREFIX}{int(now * 1000)}"
            channel = sender.channel if self._auth.address(self._recipient, sender.channel) \
                else self._primary
            text = (f"Stopped. Cancelled {len(nids)} queued notification(s) and"
                    f" {len(appr)} pending approval(s).")
            self.enqueue(c, notification_id=reply, kind="stop_ack", text=text, urgency=URGENT,
                         channel=channel, now=now,
                         extra={"cancelled_notifications": nids, "cancelled_approvals": appr})
        unconfirmed, expired = self._await_in_flight()
        return StopReport(tuple(msgs), tuple(nids), tuple(appr), not first, reply,
                          tuple(unconfirmed), tuple(expired))

    def _await_in_flight(self) -> tuple[list[str], list[str]]:
        """Wait (bounded by ``stop_wait_s``) for unexpired sends past the gate
        to return, then report EVERY row still present - age never removes an
        entry from the report. Expired rows are not waited for (a sender that
        has not yet reached its final recheck will abort there), but they stay
        in the report: one already past its final recheck may still deliver
        once. The caller's own in-flight send (``/stop``
        handled from inside a send on this thread) is excluded.

        Returns (unconfirmed, expired). When the wait expires the unconfirmed
        set is returned as-is; it is never an all-clear."""
        deadline = time.monotonic() + self._stop_wait
        mine = self._inflight()
        while True:
            now = self._now()
            with self.db.read() as c:
                rows = c.execute("SELECT message_id, since FROM notifier_sending"
                                 " ORDER BY message_id").fetchall()
            rows = [(m, since) for m, since in rows if m not in mine]
            expired = [m for m, since in rows if now >= since + self._dispatch_timeout]
            waiting = [m for m, since in rows if now < since + self._dispatch_timeout]
            if not waiting or time.monotonic() >= deadline:
                return [m for m, _ in rows], expired
            self._sleep(0.005)

    def _sweep(self, now: float) -> list[str]:
        with self.db.tx() as c:
            return self._sweep_tx(c, now)

    @staticmethod
    def _sweep_tx(c: sqlite3.Connection, now: float) -> list[str]:
        # In-flight rows are included: their send completes and C3 records the
        # delivery, but the row stays suppressed, so no retry or resend follows.
        ids = [r[0] for r in c.execute(
            "SELECT message_id FROM outbox WHERE status IN (?,?) AND notification_id NOT LIKE ?"
            " ORDER BY message_id", (PENDING, UNKNOWN, STOP_PREFIX + "%"))]
        c.execute("UPDATE outbox SET status=?, last_error='cancelled by /stop', updated_at=?"
                  " WHERE status IN (?,?) AND notification_id NOT LIKE ?",
                  (SUPPRESSED, now, PENDING, UNKNOWN, STOP_PREFIX + "%"))
        return ids

    def watchdog(self, now: float | None = None, max_age: float = 1800.0) -> list[QueuedMessage]:
        """Report notifier backlog older than ``max_age`` (T19 item 7)."""
        now = self._now(now)
        stale = [m for m in self.outbox.stale_backlog(now=now, max_age=max_age)
                 if not m.notification_id.startswith("watchdog:")]
        if stale and not self.kill.is_set():
            nid = f"watchdog:backlog:{int(now // max_age)}"
            with self.db.tx() as c:
                # One alert per bucket: its first content stands, even if the
                # backlog changes later in the same bucket.
                if c.execute("SELECT 1 FROM outbox WHERE notification_id=? LIMIT 1",
                             (nid,)).fetchone():
                    return stale
                self.enqueue(c, notification_id=nid, kind="watchdog", urgency=URGENT, now=now,
                             text=f"Notifier backlog: {len(stale)} message(s) older than"
                                  f" {int(max_age // 60)} min.",
                             extra={"stale_messages": [m.message_id for m in stale]})
        return stale

    # ------------------------------------------------------------------ traces
    def _trace_sends(self, report: RunReport, now: float) -> None:
        for outcome, ids in (("completed", report.sent), ("failed", report.failed),
                             ("unknown", report.abandoned_unknown)):
            for mid in ids:
                nid = mid.rsplit(":", 1)[0]
                emit_trace(self._sink, task_key=f"notification:{nid}", trace_key=f"send:{mid}",
                           component="effect", outcome=outcome,
                           descriptor=f"e1.send|{mid}|{outcome}", subject_key=f"message:{mid}",
                           now=now, environment=self._env)


# Private registry: handle id -> (notifier, spec). Handles never hold the
# notifier; they hold an unguessable id that only this module can resolve.
_HANDLES: dict[str, tuple[Notifier, JobSpec]] = {}
_HANDLES_LOCK = threading.Lock()


def _register_handle(notifier: Notifier, spec: JobSpec) -> str:
    hid = secrets.token_hex(16)
    with _HANDLES_LOCK:
        _HANDLES[hid] = (notifier, spec)
    return hid


def _resolve(hid: str) -> tuple[Notifier, JobSpec]:
    with _HANDLES_LOCK:
        entry = _HANDLES.get(hid)
    if entry is None:
        raise NotifierError("unknown job handle")
    return entry


class JobHandle:
    """What a scheduled job gets: enqueue-only, bound to its declared target.
    Holds only strings and a bool; no reference to the notifier or channels."""
    __slots__ = ("_hid", "job", "silent")

    def __init__(self, notifier: Notifier, spec: JobSpec):
        object.__setattr__(self, "_hid", _register_handle(notifier, spec))
        object.__setattr__(self, "job", spec.name)
        object.__setattr__(self, "silent", spec.delivery == "silent")

    def __setattr__(self, name, value):
        raise AttributeError("JobHandle is read-only")

    def __reduce__(self):
        raise TypeError("JobHandle cannot be serialized")

    def notify(self, tx: sqlite3.Connection, key: str, *, kind: str, text: str,
               urgency: str = ROUTINE, now: float | None = None) -> str | None:
        """Enqueue in the job's own transaction. Silent jobs enqueue nothing."""
        if kind not in JOB_KINDS:
            raise ValueError(f"jobs may send {sorted(JOB_KINDS)}")
        if self.silent:
            return None
        notifier, spec = _resolve(self._hid)
        return notifier.enqueue(tx, notification_id=f"job:{spec.name}:{key}", kind=kind,
                                text=text, urgency=urgency, now=now)

    def long_work(self, work_id: str, started_at: float) -> "LongWork":
        return LongWork(self._hid, f"{self.job}:{work_id}", started_at, silent=self.silent)


class LongWork:
    """T19 item 5: one ack after 30 s, updates only on state change, final
    message with receipt link.

    Durable: what has been sent lives in the outbox, and every tick reads it
    back inside its own transaction. The ack records the state at ack time;
    update ``n`` is ``work:<id>:state:<n>`` with ``n`` = 1 + the highest
    update already queued. A fresh ``LongWork`` after a restart therefore
    continues the numbering and compares against the last state Josh was
    told, so a replayed tick sends nothing and no id is reused."""

    __slots__ = ("_hid", "_id", "_start", "_silent", "_state", "_done")

    def __init__(self, handle_id: str, work_id: str, started_at: float, *,
                 silent: bool = False):
        # Like JobHandle: a handle id, never the notifier itself.
        self._hid, self._id, self._start, self._silent = handle_id, work_id, started_at, silent
        self._state: str | None = None      # in-memory only before the ack
        self._done = False

    @property
    def _prefix(self) -> str:
        return f"work:{self._id}:"

    def _durable(self, c: sqlite3.Connection) -> tuple[str | None, int, str | None, bool]:
        """(state at ack or None if no ack, last update seq, its state, finished)."""
        prefix = self._prefix
        rows = c.execute("SELECT DISTINCT notification_id, body FROM outbox"
                         " WHERE substr(notification_id, 1, ?)=?",
                         (len(prefix), prefix)).fetchall()
        ack_state, seq, last, done = None, 0, None, False
        for nid, body in rows:
            suffix, b = nid[len(prefix):], json.loads(body)
            if suffix == "ack":
                ack_state = b.get("state", "")
            elif suffix == "final":
                done = True
            elif suffix.startswith("state:") and suffix[6:].isdigit():
                n = int(suffix[6:])
                if n > seq:
                    seq, last = n, b.get("state")
        return ack_state, seq, last, done

    def _enqueue(self, c, suffix: str, kind: str, text: str, now: float, extra) -> None:
        _resolve(self._hid)[0].enqueue(c, notification_id=f"{self._prefix}{suffix}",
                                       kind=kind, text=text, now=now, extra=extra)

    def tick(self, state: str, now: float) -> None:
        if self._done:
            raise NotifierError("work already finished")
        if self._silent:
            self._state = state
            return
        with _resolve(self._hid)[0].db.tx() as c:
            ack_state, seq, last, done = self._durable(c)
            if done:
                self._done = True
                raise NotifierError("work already finished")
            if ack_state is None:
                if now - self._start >= LONG_WORK_ACK_S:
                    self._enqueue(c, "ack", "work_ack",
                                  "Still working on this; I'll update you on changes.", now,
                                  {"state": state})
                self._state = state
                return
            told = last if seq else ack_state
            if state != told:
                self._enqueue(c, f"state:{seq + 1}", "work_update", f"Now: {state}", now,
                              {"state": state})
            self._state = state

    def finish(self, receipt_link: str, now: float, *, outcome: str = "done") -> None:
        if not receipt_link:
            raise ValueError("final message needs a receipt link")
        self._done = True
        if self._silent:
            return
        with _resolve(self._hid)[0].db.tx() as c:
            self._enqueue(c, "final", "work_final", f"Finished ({outcome}).", now,
                          {"receipt": receipt_link, "outcome": outcome})
