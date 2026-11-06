"""ApprovalIssuer - the only place ApprovalRecords are minted (C0, T19 item 4).

Lifecycle of one approval (one action ID):

1. ``request(tx, action, ...)`` - in the caller's transaction: stores the exact
   canonical BoundAction (and so its preconditions) under a fresh
   ``approval_id``, valid for 24 h from now, and enqueues the prompt through
   the notifier. Refused while the kill flag is set.
2. ``decide(approval_id, decision, sender, live_action)`` - Josh answered.
   Refused unless ALL hold:
     * ``sender`` was minted by this ``ChannelAuth`` for ``human:josh``;
     * the channel can carry an approval under C0 v0 (``telegram``,
       ``potter_home``, ``passkey_step_up``) - see note on email below;
     * the kill flag is clear and the request is still ``pending``
       (single use: the state flip is one ``BEGIN IMMEDIATE`` update);
     * it is less than 24 h since the request;
     * the action recomputed against live state hashes to the stored hash
       (any precondition/parameter change -> ``voided``), and, if the C2 task
       exists, it is still ``waiting_approval`` (any other state -> voided).
   On success the record is signed with the injected issuer key; its
   ``expires_at`` is the request's 24 h deadline, so the whole window never
   exceeds 24 h. C1 then verifies it and consumes the nonce exactly once.
3. ``state_changed(task_id)`` - voids every pending request for the task.

Email: the fallback email channel authenticates Josh (DKIM) for acks and
/stop, but C0 v0's ``channel`` enum has no ``email`` value, so an approval
reply by email is refused (``channel_not_approval_capable``) rather than
recorded under a false channel claim. Widening this is a C0 contract change.

Keys: ``IssuerKey`` is injected. ``HmacIssuerKey`` is a TEST key wrapper only;
production key custody is T11/T12 and out of scope here.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from identity.seed import JOSH
from policy._contracts import approval as c0
from runtime import ROUTINE, Database
from runtime.db import as_database
from runtime.tasks import WAITING_APPROVAL

from . import _schema
from .auth import AuthenticatedSender, ChannelAuth
from .kill import KillFlag
from .service import Notifier, Stopped
from .traces import Sink, emit_trace

APPROVE, DENY = "approve", "deny"
REFUSALS = frozenset({
    "unauthenticated", "channel_not_approval_capable", "stopped", "unknown_approval",
    "already_decided", "expired", "state_changed", "bad_decision", "issuer_error",
})


class IssuerKey(Protocol):
    key_id: str

    def sign(self, payload: bytes) -> str: ...


class HmacIssuerKey:
    """TEST ONLY. Wraps the C0 reference HMAC so a C0 ``HmacAuthenticator`` with
    the same test key verifies what this signs."""
    def __init__(self, key_id: str, test_secret: bytes):
        self.key_id = key_id
        self._auth = c0.HmacAuthenticator({key_id: test_secret})

    def sign(self, payload: bytes) -> str:
        return self._auth.sign(self.key_id, payload)


class ApprovalRefusal(Exception):
    def __init__(self, code: str):
        assert code in REFUSALS
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PendingApproval:
    approval_id: str
    task_id: str
    action_hash: str
    status: str
    requested_at: int
    expires_at: int
    status_reason: str | None


def _new_id() -> str:
    return f"appr-{uuid.uuid4().hex}"


def _new_nonce() -> str:
    return secrets.token_hex(16)


class ApprovalIssuer:
    def __init__(self, db: Database | str, *, key: IssuerKey, notifier: Notifier,
                 auth: ChannelAuth, clock: Callable[[], float] = time.time,
                 new_id: Callable[[], str] = _new_id, new_nonce: Callable[[], str] = _new_nonce,
                 trace_sink: Sink | None = None, environment: str = "synthetic"):
        if not getattr(key, "key_id", None):
            raise ValueError("issuer key needs a key_id")
        self.db = as_database(db)
        _schema.ensure(self.db)
        self.kill = KillFlag(self.db)
        self._key = key
        self._notifier = notifier
        self._auth = auth
        self._clock = clock
        self._new_id = new_id
        self._new_nonce = new_nonce
        self._sink = trace_sink
        self._env = environment

    def _now(self, now: float | None) -> int:
        return int(self._clock() if now is None else now)

    # ------------------------------------------------------------------ request
    def request(self, tx: sqlite3.Connection, action: c0.BoundAction, *, summary: str,
                urgency: str = ROUTINE, screenshot_ref: str | None = None,
                now: float | None = None) -> str:
        if not isinstance(action, c0.BoundAction):
            raise TypeError("action must be a C0 BoundAction")
        if action.capability in c0.SCREENSHOT_CAPS and not screenshot_ref:
            raise ValueError("T38: browser/computer approvals need a screenshot_ref")
        if self.kill.is_set(tx):
            raise Stopped("kill flag set")
        now = self._now(now)
        approval_id = self._new_id()
        nid = f"approval:{approval_id}"
        expires = now + c0.MAX_VALIDITY_S
        tx.execute("INSERT INTO approval_requests (approval_id, task_id, action_hash,"
                   " action_canonical, capability, screenshot_ref, notification_id,"
                   " requested_at, expires_at, status) VALUES (?,?,?,?,?,?,?,?,?,'pending')",
                   (approval_id, action.task_id, action.hash(),
                    action.canonical_json().decode("utf-8"), action.capability, screenshot_ref,
                    nid, now, expires))
        # The prompt carries the action ID and a caller-written summary only,
        # never the action's parameters (redaction boundary).
        self._notifier.enqueue(tx, notification_id=nid, kind="approval", text=summary,
                               urgency=urgency, now=now,
                               extra={"approval_id": approval_id, "expires_at": expires,
                                      "action_hash": action.hash()})
        self._trace(action.task_id, approval_id, "issue", "accepted", action.hash(), now)
        return approval_id

    # ------------------------------------------------------------------ decide
    def decide(self, approval_id: str, decision: str, sender: AuthenticatedSender,
               live_action: c0.BoundAction | None, now: float | None = None) -> dict:
        """Return a signed ApprovalRecord or raise ApprovalRefusal."""
        now = self._now(now)
        if decision not in (APPROVE, DENY):
            raise ApprovalRefusal("bad_decision")
        # Verify and retire the voucher in one step: a voucher drives at most
        # one approval decision, and a lookalike never passes.
        if not self._auth.consume(sender) or sender.principal != JOSH:
            raise ApprovalRefusal("unauthenticated")
        if sender.channel not in c0.CHANNELS:
            raise ApprovalRefusal("channel_not_approval_capable")
        outcome: dict | ApprovalRefusal
        with self.db.tx() as c:
            outcome = self._decide_tx(c, approval_id, decision, sender, live_action, now)
            if not isinstance(outcome, ApprovalRefusal):
                # Inside the transaction: a failing trace sink rolls the decision back.
                self._trace(outcome["task_id"], approval_id, "decide",
                            "accepted" if decision == APPROVE else "blocked",
                            outcome["action_hash"], now)
        # Status changes (expired / voided) are committed before refusing.
        if isinstance(outcome, ApprovalRefusal):
            row = self.get(approval_id)
            self._trace(row.task_id if row else approval_id, approval_id, "decide", "blocked",
                        row.action_hash if row else "", now)
            raise outcome
        return outcome

    def _decide_tx(self, c, approval_id, decision, sender, live_action, now):
        if self.kill.is_set(c):
            return ApprovalRefusal("stopped")
        row = c.execute("SELECT task_id, action_hash, screenshot_ref, expires_at, status"
                        " FROM approval_requests WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            return ApprovalRefusal("unknown_approval")
        task_id, action_hash, screenshot_ref, expires_at, status = row
        if status != "pending":
            return ApprovalRefusal("already_decided" if status in ("approved", "denied")
                                   else "expired" if status == "expired" else "state_changed")
        if now >= expires_at:
            self._close(c, approval_id, "expired", "24h")
            return ApprovalRefusal("expired")
        task = c.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is not None and task[0] != WAITING_APPROVAL:
            self._close(c, approval_id, "voided", "task_state_changed")
            return ApprovalRefusal("state_changed")
        if not isinstance(live_action, c0.BoundAction) or live_action.hash() != action_hash:
            self._close(c, approval_id, "voided", "action_or_preconditions_changed")
            return ApprovalRefusal("state_changed")
        nonce = self._new_nonce()
        record = {"schema": "potter.approval/v0", "approval_id": approval_id,
                  "task_id": task_id, "action_hash": action_hash, "decision": decision,
                  "decided_by": "josh", "channel": sender.channel, "issued_at": now,
                  "expires_at": expires_at, "nonce": nonce,
                  "issuer_key_id": self._key.key_id, "issuer_signature": ""}
        if screenshot_ref:
            record["screenshot_ref"] = screenshot_ref
        record["issuer_signature"] = self._key.sign(c0.signing_payload(record))
        try:
            c0.check_shape(record)
        except c0.ApprovalRefused:
            raise RuntimeError("issuer produced a malformed record")   # rolls back
        cur = c.execute("UPDATE approval_requests SET status=?, nonce=?, decided_at=?,"
                        " decided_channel=?, record=? WHERE approval_id=? AND status='pending'",
                        ("approved" if decision == APPROVE else "denied", nonce, now,
                         sender.channel, json.dumps(record, sort_keys=True), approval_id))
        if cur.rowcount != 1:
            return ApprovalRefusal("already_decided")
        return record

    @staticmethod
    def _close(c, approval_id: str, status: str, reason: str) -> None:
        c.execute("UPDATE approval_requests SET status=?, status_reason=? WHERE approval_id=?"
                  " AND status='pending'", (status, reason, approval_id))

    # ------------------------------------------------------------------ state changes
    def state_changed(self, task_id: str, now: float | None = None) -> list[str]:
        """Any state change on the task voids its pending approvals (T19 item 4)."""
        with self.db.tx() as c:
            ids = [r[0] for r in c.execute("SELECT approval_id FROM approval_requests WHERE"
                                           " task_id=? AND status='pending'", (task_id,))]
            c.execute("UPDATE approval_requests SET status='voided', status_reason="
                      "'state_changed' WHERE task_id=? AND status='pending'", (task_id,))
        return ids

    def get(self, approval_id: str) -> PendingApproval | None:
        with self.db.read() as c:
            row = c.execute("SELECT approval_id, task_id, action_hash, status, requested_at,"
                            " expires_at, status_reason FROM approval_requests"
                            " WHERE approval_id=?", (approval_id,)).fetchone()
        return PendingApproval(*row) if row else None

    def _trace(self, task_id: str, approval_id: str, event: str, outcome: str,
               action_hash: str, now: int) -> None:
        emit_trace(self._sink, task_key=task_id, trace_key=f"approval:{approval_id}",
                   component="task", outcome=outcome,
                   descriptor=f"e1.{event}|{approval_id}|{action_hash}|{outcome}",
                   subject_key=f"approval:{approval_id}", now=now, environment=self._env)
