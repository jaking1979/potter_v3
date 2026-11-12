"""Transactional outbox (package C3, decisions T4/T17; T19 PASS 2 items 1, 3, 7).

A notification is written to the ``outbox`` table in the SAME SQLite
transaction as the business state change that caused it (``Outbox.enqueue(tx,
message)`` with a connection from C2's ``Database.tx()``). If the business
transaction rolls back, no message exists; if it commits, the message exists
durably with its stable ``message_id`` and idempotency key *before* anything is
sent (T19 item 1: notification ID written before send).

A relay (``Outbox.relay(send, reconcile, owner)``) delivers due messages. It
mirrors C2 ``EffectJournal`` semantics:

* ``pending``  -> claimed: status ``unknown`` + ``dispatches+1`` + in-flight
  window are COMMITTED before ``send`` is called, so a crash, kill or ambiguous
  error during the call leaves ``unknown``, never ``pending``.
* ``send`` returns            -> ``sent``.
* ``send`` raises SendFailed  -> definitely not delivered: back to ``pending``
  with backoff, ``failed`` after ``max_attempts`` definite failures.
* any other exception / crash -> ``unknown``; resolved below.

Unknown outcomes (T19 PASS 2 item 1) are first given to ``reconcile(message)``:

* ``"succeeded"`` -> ``sent``; nothing is resent.
* ``"failed"``    -> the channel proves the key was never delivered: treated as
  a definite failure (retry with the SAME key, bounded by ``max_attempts``),
  except after an urgent blind resend, which is final (``abandoned_unknown``).
* ``"still_unknown"``, anything unrecognised, or a reconcile error:
  - ``routine`` -> NOT resent. Marked ``abandoned_unknown`` and surfaced by
    ``needs_reconciliation()`` for the report.
  - ``urgent``  -> resent ONCE with the same idempotency key
    (``blind_resends`` 0 -> 1, enforced by a CHECK constraint). Any
    non-success outcome after that resend -> ``abandoned_unknown``.

Re-dispatch after an unknown outcome needs a fence. A lease cannot preempt a
paused sender: a stalled process, or a request that timed out locally, can
still deliver after its claim expired. So a reconcile verdict of ``"failed"``
("not delivered yet") authorizes a re-dispatch ONLY if the injected ``fence``
(the C4 / provider-side seam, mirroring C2 ``EffectJournal(fence=...)``)
confirms every earlier dispatch is fenced off at the channel. With no fence,
or if it errors or returns anything but ``True``, the verdict is treated as
``still_unknown``: routine is not resent, urgent gets only its single T19
resend. An expired claim alone never authorizes anything but a reconcile.

Claim deadlines are computed INSIDE the write transaction, after BEGIN
IMMEDIATE has the lock, so waiting on the lock cannot commit a claim that is
already expired. Inside that transaction the time used is
``max(clock(), outbox_clock_hwm)``, a high-water mark kept in ``meta``. A
relay whose clock stepped backwards therefore cannot write a deadline that
other relays already consider past. A clock that jumps forward only freezes
recovery until real time catches up; it never shortens a live lease.

This is the one deliberate departure from T10's "never blind retry": T19
accepts a possible duplicate for urgent notifications in exchange for not
silently losing one, and caps it at one duplicate.

Acknowledgement suppression (T19 item 3): messages fanned out for one
notification share a ``notification_id``. ``ack(message_id)`` marks that
message ``acked`` and every sibling that has not been delivered (``pending``,
``unknown``, ``abandoned_unknown``) ``suppressed``; the relay never claims a
message whose notification is acked.

Watchdog (T19 item 7): ``stale_backlog(now, max_age=1800)`` lists messages due
for longer than ``max_age`` that are still ``pending`` or ``unknown``.

Staging-only (T22): nothing here talks to a real channel. ``send`` and
``reconcile`` are injected; real Telegram/email connectors are Phase E.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .db import Database, JournalUnavailable, as_database
from .effects import FAILED as VERDICT_FAILED, STILL_UNKNOWN, SUCCEEDED as VERDICT_SUCCEEDED

OUTBOX_SCHEMA_VERSION = "c3.v0"
OUTBOX_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:potter:v3:c3:outbox")

ROUTINE, URGENT = "routine", "urgent"
URGENCIES = (ROUTINE, URGENT)

PENDING, UNKNOWN, SENT, FAILED = "pending", "unknown", "sent", "failed"
ABANDONED_UNKNOWN, SUPPRESSED, ACKED = "abandoned_unknown", "suppressed", "acked"
STATUSES = (PENDING, UNKNOWN, SENT, FAILED, ABANDONED_UNKNOWN, SUPPRESSED, ACKED)

DEFAULT_DISPATCH_TIMEOUT_S = 300.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_S = 30.0
STALE_BACKLOG_S = 30 * 60.0          # T19 item 7

OUTBOX_SCHEMA = (
    f"""CREATE TABLE IF NOT EXISTS outbox (
        message_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        notification_id TEXT NOT NULL,
        channel TEXT NOT NULL,
        recipient TEXT NOT NULL,
        body TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        urgency TEXT NOT NULL CHECK (urgency IN ('routine','urgent')),
        status TEXT NOT NULL CHECK (status IN ({",".join(repr(s) for s in STATUSES)})),
        dispatches INTEGER NOT NULL DEFAULT 0,
        definite_failures INTEGER NOT NULL DEFAULT 0,
        blind_resends INTEGER NOT NULL DEFAULT 0 CHECK (blind_resends BETWEEN 0 AND 1),
        in_flight_owner TEXT,
        in_flight_until REAL,
        due_at REAL NOT NULL,
        next_attempt_at REAL NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        sent_at REAL,
        acked_at REAL,
        result TEXT,
        last_error TEXT)""",
    "CREATE INDEX IF NOT EXISTS outbox_due ON outbox (status, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS outbox_notification ON outbox (notification_id)",
)


def outbox_idempotency_key(message_id: str) -> str:
    """Stable UUIDv5 of the message id; every dispatch of a message reuses it."""
    return str(uuid.uuid5(OUTBOX_NAMESPACE, message_id))


class OutboxError(Exception):
    pass


class OutboxConflict(OutboxError):
    """The message id is already bound to different content."""


class SendFailed(OutboxError):
    """Raised by ``send`` when the channel DEFINITELY did not deliver (e.g. a
    4xx before any write). Any other exception is an unknown outcome."""


@dataclass(frozen=True)
class OutboxMessage:
    """What a caller enqueues. ``message_id`` must be stable for the business
    event (e.g. ``"escalation:task:<id>:telegram"``) so a replayed transaction
    cannot create a second message."""
    message_id: str
    channel: str
    recipient: str
    body: Any
    urgency: str = ROUTINE
    notification_id: str | None = None
    not_before: float | None = None

    def content_hash(self) -> str:
        blob = json.dumps([self.channel, self.recipient, self.body, self.urgency,
                           self.notification_id or self.message_id],
                          sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class QueuedMessage:
    message_id: str
    idempotency_key: str
    notification_id: str
    channel: str
    recipient: str
    body: Any
    urgency: str
    status: str
    dispatches: int
    definite_failures: int
    blind_resends: int
    in_flight_owner: str | None
    in_flight_until: float | None
    due_at: float
    next_attempt_at: float
    created_at: float
    sent_at: float | None
    acked_at: float | None
    result: Any
    last_error: str | None


@dataclass
class RelayReport:
    sent: list[str] = field(default_factory=list)
    retry_scheduled: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    resent_unknown: list[str] = field(default_factory=list)
    abandoned_unknown: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)


_COLS = ("message_id, idempotency_key, notification_id, channel, recipient, body, urgency,"
         " status, dispatches, definite_failures, blind_resends, in_flight_owner,"
         " in_flight_until, due_at, next_attempt_at, created_at, sent_at, acked_at, result,"
         " last_error")


def _row(row: tuple) -> QueuedMessage:
    r = list(row)
    r[5] = json.loads(r[5])
    r[18] = json.loads(r[18]) if r[18] is not None else None
    return QueuedMessage(*r)


def _load(c: sqlite3.Connection, message_id: str) -> QueuedMessage | None:
    row = c.execute(f"SELECT {_COLS} FROM outbox WHERE message_id=?", (message_id,)).fetchone()
    return _row(row) if row else None


def _notification_acked(c: sqlite3.Connection, notification_id: str) -> bool:
    return c.execute("SELECT 1 FROM outbox WHERE notification_id=? AND acked_at IS NOT NULL"
                     " LIMIT 1", (notification_id,)).fetchone() is not None


class Outbox:
    def __init__(self, db: Database | str | Path, *, clock: Callable[[], float] = time.time,
                 dispatch_timeout_s: float = DEFAULT_DISPATCH_TIMEOUT_S,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 backoff_s: float = DEFAULT_BACKOFF_S,
                 fence: Callable[[QueuedMessage], bool] | None = None):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.db = as_database(db)
        self._clock = clock
        self._dispatch_timeout = dispatch_timeout_s
        self._max_attempts = max_attempts
        self._backoff = backoff_s
        self._fence = fence   # C4/provider seam; None = refuse unfenced re-dispatch
        try:
            with self.db.tx() as c:
                for stmt in OUTBOX_SCHEMA:
                    c.execute(stmt)
                c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('outbox_schema', ?)",
                          (OUTBOX_SCHEMA_VERSION,))
                c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('outbox_clock_hwm', '0')")
                got = c.execute("SELECT value FROM meta WHERE key='outbox_schema'").fetchone()[0]
                if got != OUTBOX_SCHEMA_VERSION:
                    raise JournalUnavailable(
                        f"outbox schema {got!r}, expected {OUTBOX_SCHEMA_VERSION!r}")
        except sqlite3.Error as exc:
            raise JournalUnavailable(f"outbox unusable: {exc}") from exc

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    @staticmethod
    def _tx_now(c: sqlite3.Connection, clock: Callable[[], float]) -> float:
        """Time for a relay transition. Call ONLY inside ``db.tx()``: the clock
        is read after the write lock is held, and never below the shared
        high-water mark, so no deadline is written that is already past."""
        t = float(clock())
        hwm = float(c.execute("SELECT value FROM meta WHERE key='outbox_clock_hwm'").fetchone()[0])
        if t > hwm:
            c.execute("UPDATE meta SET value=? WHERE key='outbox_clock_hwm'", (repr(t),))
            return t
        return hwm

    def _fenced(self, msg: QueuedMessage) -> bool:
        """True only if the fence confirms every earlier dispatch of ``msg`` can
        no longer deliver. Default (no fence), an error or a non-True answer: refuse."""
        if self._fence is None:
            return False
        try:
            return self._fence(msg) is True
        except Exception:
            return False

    # -- enqueue (inside the caller's business transaction) -----------------
    def enqueue(self, tx: sqlite3.Connection, message: OutboxMessage, *,
                now: float | None = None) -> QueuedMessage:
        """Write ``message`` in the caller's open write transaction ``tx`` (from
        ``Database.tx()``). It commits or rolls back with the business change.
        Re-enqueueing the same id with the same content returns the existing
        row; different content raises OutboxConflict (and the caller's
        transaction rolls back if the exception escapes ``tx()``)."""
        if not isinstance(message, OutboxMessage):
            raise TypeError("message must be an OutboxMessage")
        if not isinstance(tx, sqlite3.Connection) or not tx.in_transaction:
            raise ValueError("enqueue needs the caller's open transaction (Database.tx())")
        if not message.message_id or not message.channel or not message.recipient:
            raise ValueError("message_id, channel and recipient are required")
        if message.urgency not in URGENCIES:
            raise ValueError(f"urgency must be one of {URGENCIES}")
        body = json.dumps(message.body, sort_keys=True, ensure_ascii=False)
        chash = message.content_hash()
        now = self._now(now)
        due = max(now, message.not_before) if message.not_before is not None else now
        existing = tx.execute("SELECT content_hash FROM outbox WHERE message_id=?",
                              (message.message_id,)).fetchone()
        if existing is not None:
            if existing[0] != chash:
                raise OutboxConflict(f"message {message.message_id!r} bound to other content")
            return _load(tx, message.message_id)
        tx.execute("INSERT INTO outbox (message_id, idempotency_key, notification_id, channel,"
                   " recipient, body, content_hash, urgency, status, due_at, next_attempt_at,"
                   " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (message.message_id, outbox_idempotency_key(message.message_id),
                    message.notification_id or message.message_id, message.channel,
                    message.recipient, body, chash, message.urgency, PENDING, due, due, now, now))
        return _load(tx, message.message_id)

    # -- reading ------------------------------------------------------------
    def get(self, message_id: str) -> QueuedMessage | None:
        with self.db.read() as c:
            return _load(c, message_id)

    def _select(self, where: str, args: tuple) -> list[QueuedMessage]:
        with self.db.read() as c:
            rows = c.execute(f"SELECT {_COLS} FROM outbox WHERE {where}"
                             " ORDER BY due_at, message_id", args).fetchall()
        return [_row(r) for r in rows]

    def stale_backlog(self, now: float | None = None,
                      max_age: float = STALE_BACKLOG_S) -> list[QueuedMessage]:
        """Watchdog input (T19 item 7): due for more than ``max_age`` seconds
        and still not delivered or resolved."""
        now = self._now(now)
        return self._select("status IN (?,?) AND due_at <= ?", (PENDING, UNKNOWN, now - max_age))

    def needs_reconciliation(self) -> list[QueuedMessage]:
        """Report input: messages that may or may not have been delivered and
        will not be resent, plus definite delivery failures."""
        return self._select("status IN (?,?)", (ABANDONED_UNKNOWN, FAILED))

    # -- acknowledgement ----------------------------------------------------
    def ack(self, message_id: str, *, now: float | None = None) -> QueuedMessage:
        """The recipient acknowledged ``message_id``: mark it ``acked`` (this
        also resolves an unknown outcome: it was delivered) and suppress every
        undelivered sibling of the same notification."""
        now = self._now(now)
        with self.db.tx() as c:
            msg = _load(c, message_id)
            if msg is None:
                raise KeyError(f"no outbox message {message_id}")
            if msg.acked_at is None:
                c.execute("UPDATE outbox SET status=?, acked_at=?, updated_at=?"
                          " WHERE message_id=?", (ACKED, now, now, message_id))
                c.execute("UPDATE outbox SET status=?, updated_at=?"
                          " WHERE notification_id=? AND message_id<>? AND status IN (?,?,?)",
                          (SUPPRESSED, now, msg.notification_id, message_id,
                           PENDING, UNKNOWN, ABANDONED_UNKNOWN))
            return _load(c, message_id)

    # -- relay --------------------------------------------------------------
    def relay(self, send: Callable[[QueuedMessage], Any],
              reconcile: Callable[[QueuedMessage], str], *, owner: str,
              now: float | None = None, limit: int | None = None) -> RelayReport:
        """Deliver everything that is due. ``send(message)`` must pass
        ``message.idempotency_key`` to the channel. ``reconcile(message)``
        returns ``"succeeded"``, ``"failed"`` or ``"still_unknown"``. Safe to
        run concurrently from any number of threads or processes: each claim
        is one ``BEGIN IMMEDIATE`` transaction."""
        if not owner:
            raise ValueError("owner required")
        # Re-read the clock for every claim, lease, settle and backoff: one
        # sample per relay() would hand later messages of a long drain leases
        # that are already expired. An explicit ``now`` (tests) stays fixed.
        clock: Callable[[], float] = (lambda: now) if now is not None else self._clock
        report = RelayReport()
        handled = 0
        while limit is None or handled < limit:
            claim = self._claim(owner, clock)
            if claim is None:
                break
            kind, msg = claim
            handled += 1
            if kind == "suppress":
                report.suppressed.append(msg.message_id)
            elif kind == "send":
                self._dispatch(msg, send, reconcile, owner, clock, report)
            else:
                self._resolve_unknown(msg, send, reconcile, owner, clock, report)
        return report

    def _claim(self, owner: str, clock: Callable[[], float]) -> tuple[str, QueuedMessage] | None:
        with self.db.tx() as c:
            now = self._tx_now(c, clock)          # sampled after BEGIN IMMEDIATE has the lock
            until = now + self._dispatch_timeout
            # 1. Unknown outcomes nobody holds (released, or dispatcher died).
            row = c.execute(f"SELECT {_COLS} FROM outbox WHERE status=? AND"
                            " (in_flight_owner IS NULL OR in_flight_until <= ?)"
                            " ORDER BY urgency='urgent' DESC, due_at, message_id LIMIT 1",
                            (UNKNOWN, now)).fetchone()
            kind = "reconcile"
            if row is None:
                # 2. Due pending messages, urgent first.
                row = c.execute(f"SELECT {_COLS} FROM outbox WHERE status=? AND"
                                " next_attempt_at <= ?"
                                " ORDER BY urgency='urgent' DESC, due_at, message_id LIMIT 1",
                                (PENDING, now)).fetchone()
                kind = "send"
            if row is None:
                return None
            msg = _row(row)
            if _notification_acked(c, msg.notification_id):
                c.execute("UPDATE outbox SET status=?, in_flight_owner=NULL,"
                          " in_flight_until=NULL, updated_at=? WHERE message_id=?",
                          (SUPPRESSED, now, msg.message_id))
                return "suppress", _load(c, msg.message_id)
            if kind == "send":
                # Dispatch is recorded and committed BEFORE send is called.
                c.execute("UPDATE outbox SET status=?, dispatches=dispatches+1,"
                          " in_flight_owner=?, in_flight_until=?, updated_at=?"
                          " WHERE message_id=? AND status=?",
                          (UNKNOWN, owner, until, now, msg.message_id, PENDING))
            else:
                c.execute("UPDATE outbox SET in_flight_owner=?, in_flight_until=?, updated_at=?"
                          " WHERE message_id=?", (owner, until, now, msg.message_id))
            return kind, _load(c, msg.message_id)

    def _dispatch(self, msg: QueuedMessage, send, reconcile, owner: str, clock,
                  report: RelayReport) -> None:
        try:
            result = send(msg)
        except SendFailed as exc:
            self._definite_failure(msg, owner, clock, f"send failed: {exc!r}", report)
            return
        except Exception as exc:
            # Ambiguous: stays "unknown" under our claim; resolve it right away.
            if self._update(msg, owner, "last_error=?", (f"ambiguous: {exc!r}",), clock):
                self._resolve_unknown(msg, send, reconcile, owner, clock, report)
            return
        if self._settle_sent(msg, owner, result, clock):
            report.sent.append(msg.message_id)

    def _resolve_unknown(self, msg: QueuedMessage, send, reconcile, owner: str, clock,
                         report: RelayReport) -> None:
        try:
            verdict = reconcile(msg)
        except Exception as exc:          # a failed read is not evidence of anything
            verdict, note = STILL_UNKNOWN, f"reconcile error: {exc!r}"
        else:
            note = None
        if verdict not in (VERDICT_SUCCEEDED, VERDICT_FAILED, STILL_UNKNOWN):
            verdict, note = STILL_UNKNOWN, f"unrecognised reconcile verdict {verdict!r}"
        if verdict == VERDICT_SUCCEEDED:
            if self._settle_sent(msg, owner, {"reconciled": True}, clock):
                report.sent.append(msg.message_id)
            return
        if verdict == VERDICT_FAILED:
            if self._fenced(msg):
                # Proven not delivered AND no earlier dispatch can still land:
                # retry now with the same key (counts towards max_attempts).
                self._definite_failure(msg, owner, clock, "reconciled: not delivered, fenced",
                                       report, retry_now=True)
                return
            # "Not delivered yet" is not "will never be delivered": a paused or
            # timed-out earlier sender may still land it. No fence -> unknown.
            verdict, note = STILL_UNKNOWN, "reconciled not delivered, but unfenced"
        reason = note or "outcome unknown"
        if msg.urgency == URGENT and msg.blind_resends == 0:
            # T19: urgent -> exactly one resend, same idempotency key.
            if self._update(msg, owner, "dispatches=dispatches+1, blind_resends=1,"
                            " in_flight_until=?, last_error=?",
                            lambda now: (now + self._dispatch_timeout, reason), clock):
                report.resent_unknown.append(msg.message_id)
                resent = self.get(msg.message_id)
                try:
                    result = send(resent)
                except Exception as exc:
                    self._abandon(resent, owner, clock, f"after one resend: {exc!r}", report)
                    return
                if self._settle_sent(resent, owner, result, clock):
                    report.sent.append(msg.message_id)
            return
        why = reason if msg.urgency == ROUTINE else f"{reason} after one resend"
        self._abandon(msg, owner, clock, why, report)

    # -- transitions (each conditioned on the claim still being ours) --------
    def _update(self, msg: QueuedMessage, owner: str, assignments: str,
                args: "tuple | Callable[[float], tuple]", clock: Callable[[], float]) -> bool:
        """``args`` may be a function of the in-transaction time, for values
        (deadlines, retry times) that must not be computed before the lock."""
        with self.db.tx() as c:
            now = self._tx_now(c, clock)
            if callable(args):
                args = args(now)
            cur = c.execute(f"UPDATE outbox SET {assignments}, updated_at=?"
                            " WHERE message_id=? AND status=? AND dispatches=?"
                            " AND in_flight_owner=?",
                            (*args, now, msg.message_id, UNKNOWN, msg.dispatches, owner))
            return cur.rowcount == 1

    def _settle_sent(self, msg: QueuedMessage, owner: str, result: Any,
                     clock: Callable[[], float]) -> bool:
        blob = json.dumps(result, sort_keys=True) if result is not None else None
        with self.db.tx() as c:
            now = self._tx_now(c, clock)
            cur = c.execute("UPDATE outbox SET status=?, result=?, sent_at=?, last_error=NULL,"
                            " in_flight_owner=NULL, in_flight_until=NULL, updated_at=?"
                            " WHERE message_id=? AND status=? AND dispatches=?"
                            " AND in_flight_owner=?",
                            (SENT, blob, now, now, msg.message_id, UNKNOWN, msg.dispatches, owner))
            if cur.rowcount == 1:
                return True
            # Acked/suppressed meanwhile: keep that status, still record delivery.
            c.execute("UPDATE outbox SET result=?, sent_at=?, in_flight_owner=NULL,"
                      " in_flight_until=NULL, updated_at=? WHERE message_id=? AND dispatches=?"
                      " AND in_flight_owner=?",
                      (blob, now, now, msg.message_id, msg.dispatches, owner))
            return False

    def _definite_failure(self, msg: QueuedMessage, owner: str, clock: Callable[[], float],
                          error: str, report: RelayReport, retry_now: bool = False) -> None:
        if msg.blind_resends:
            # The first dispatch's outcome is still unknown: never send a third time.
            self._abandon(msg, owner, clock, f"{error} after one resend", report)
            return
        failures = msg.definite_failures + 1
        final = failures >= self._max_attempts
        delay = 0.0 if retry_now else self._backoff * (2 ** (failures - 1))
        if self._update(msg, owner, "status=?, definite_failures=?, next_attempt_at=?,"
                        " last_error=?, in_flight_owner=NULL, in_flight_until=NULL",
                        lambda now: (FAILED if final else PENDING, failures, now + delay, error),
                        clock):
            (report.failed if final else report.retry_scheduled).append(msg.message_id)

    def _abandon(self, msg: QueuedMessage, owner: str, clock: Callable[[], float], error: str,
                 report: RelayReport) -> None:
        if self._update(msg, owner, "status=?, last_error=?, in_flight_owner=NULL,"
                        " in_flight_until=NULL", (ABANDONED_UNKNOWN, error), clock):
            report.abandoned_unknown.append(msg.message_id)
