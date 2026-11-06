"""Inbound commands from any channel (T19 PASS 2 items 2, 3, 4, 6).

Every inbound message is authenticated by ``ChannelAuth`` first. A sender that
is not an enabled, allowlisted principal - or whose channel signal fails
(no DKIM pass, no verified home session) - is ignored: nothing is acted on and
the attempt is written to ``inbound_log`` (channel, sender ref, reason; never
the text).

Commands from Josh:

* ``/stop``                       -> kill flag, cancel queue + approvals, reply
* ``/ack <notification_id>``      -> ack; suppresses repeats on every channel
  (or ``/ack`` as a reply to a notification)
* ``/approve <approval_id>`` / ``/deny <approval_id>`` (or as a reply to the
  approval prompt) -> ``ApprovalIssuer.decide`` with the action recomputed
  against live state by the injected ``live_action`` callback.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from policy._contracts import approval as c0

from .auth import ChannelAuth
from .channels import InboundMessage
from .issuer import APPROVE, DENY, ApprovalIssuer, ApprovalRefusal
from .service import Notifier

LiveAction = Callable[[str, str], "c0.BoundAction | None"]   # (approval_id, task_id)


@dataclass(frozen=True)
class RouteResult:
    outcome: str          # ignored | stopped | acked | approved | denied | refused | unknown_command
    reason: str = "ok"
    detail: Any = None


class CommandRouter:
    def __init__(self, *, auth: ChannelAuth, notifier: Notifier, issuer: ApprovalIssuer,
                 live_action: LiveAction, clock: Callable[[], float] = time.time):
        self._auth, self._n, self._issuer = auth, notifier, issuer
        self._live = live_action
        self._clock = clock

    def handle(self, msg: InboundMessage, now: float | None = None) -> RouteResult:
        now = self._clock() if now is None else now
        res = self._auth.authenticate(msg)
        if not res.ok:
            self._log(msg, "ignored", res.reason, now)
            return RouteResult("ignored", res.reason)
        sender = res.sender
        words = (msg.text or "").strip().split()
        cmd = words[0].lower() if words else ""
        arg = words[1] if len(words) > 1 else None

        if cmd == "/stop":
            return RouteResult("stopped", "ok", self._n.stop(sender, now))
        if cmd == "/ack":
            nid = arg or msg.reply_to
            ok = bool(nid) and self._n.ack(nid, sender, now)
            return RouteResult("acked" if ok else "refused", "ok" if ok else "unknown_notification")
        if cmd in ("/approve", "/deny"):
            approval_id = arg or _approval_from_reply(msg.reply_to)
            if not approval_id:
                return RouteResult("refused", "unknown_approval")
            row = self._issuer.get(approval_id)
            live = None
            if row is not None:
                try:
                    live = self._live(approval_id, row.task_id)
                except Exception:
                    live = None     # cannot revalidate live state -> voided, fail closed
            try:
                record = self._issuer.decide(approval_id, APPROVE if cmd == "/approve" else DENY,
                                             sender, live, now)
            except ApprovalRefusal as r:
                return RouteResult("refused", r.code)
            return RouteResult("approved" if cmd == "/approve" else "denied", "ok", record)
        self._log(msg, "unknown_command", "unknown_command", now)
        return RouteResult("unknown_command", "unknown_command")

    def _log(self, msg: InboundMessage, outcome: str, reason: str, now: float) -> None:
        with self._n.db.tx() as c:
            c.execute("INSERT INTO inbound_log (at, channel, sender_ref, outcome, reason)"
                      " VALUES (?,?,?,?,?)", (now, str(getattr(msg, "channel", "?")),
                                              str(getattr(msg, "sender_ref", "?"))[:128],
                                              outcome, reason))

    def ignored(self) -> list[tuple[str, str, str]]:
        with self._n.db.read() as c:
            return [tuple(r) for r in c.execute(
                "SELECT channel, sender_ref, reason FROM inbound_log WHERE outcome='ignored'"
                " ORDER BY seq")]


def _approval_from_reply(reply_to: str | None) -> str | None:
    if reply_to and reply_to.startswith("approval:"):
        return reply_to.split(":", 1)[1]
    return None
