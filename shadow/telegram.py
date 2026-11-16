"""Telegram front door for the shadow runtime: E3 ingress -> ShadowRuntime.

Every update goes through ``IngressGateway.ingest_telegram`` first. That call
authenticates the sender (E1), dedupes durably and decides provenance. This
adapter then lets only a *trusted* event drive a turn:

* rejected, duplicate or tampered events are dropped silently (no reply, no
  model call, no policy call);
* anything whose provenance is not exactly ``principal_direct`` (forwards, group
  chats, via-bot) is acked and dropped, so content cannot trigger a tool;
* the turn's task id is the event id, so a redelivery maps to the same task.

``ack`` happens only after ``handle`` returns, so a crash between accept and
reply leaves the event pending for ``recover``.
"""
from __future__ import annotations

import uuid

from shadow.turn import Inbound, Outcome, ShadowRuntime


class TelegramFrontend:
    def __init__(self, *, gateway, runtime: ShadowRuntime):
        self._gw = gateway
        self._rt = runtime

    def handle_update(self, update) -> Outcome:
        try:
            res = self._gw.ingest_telegram(update)
        except Exception:
            return Outcome("ignored", "ingress_error")
        if not res.accepted:
            return Outcome("ignored", res.reason)
        if res.duplicate:
            return Outcome("ignored", "duplicate")
        return self._run(res.event, str(update.chat_id))

    def recover(self, limit: int = 100) -> list[Outcome]:
        """Replay events accepted before a crash and never acked."""
        out = []
        for ev in self._gw.pending(limit):
            out.append(self._run(ev, None))
        return out

    def _run(self, ev, chat_id) -> Outcome:
        if not self._gw.vouches(ev):
            return Outcome("ignored", "not_vouched")
        if not ev.trusted or ev.principal is None or len(ev.segments) != 1:
            self._gw.ack(ev.event_id)
            return Outcome("ignored", "untrusted_provenance")
        if chat_id is None:                      # recovered: chat id is in the channel message id
            chat_id = ev.channel_message_id.split(":", 1)[0]
        try:
            task_id = str(uuid.UUID(ev.event_id))
        except (ValueError, AttributeError):
            return Outcome("ignored", "bad_event_id")
        msg = Inbound(chat_id=chat_id, text=ev.segments[0].text, event_id=ev.event_id,
                      provenance=tuple(ev.provenance), task_id=task_id)
        outcome = self._rt.handle(msg)
        self._gw.ack(ev.event_id)
        return outcome
