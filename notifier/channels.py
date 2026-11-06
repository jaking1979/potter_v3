"""Channel adapter interface and offline fakes (T19 PASS 2; staging-only, T22).

Only the notifier holds channel adapters. Jobs never receive one: they get a
``JobHandle`` (see ``service.py``), and ``notifier.lint`` fails the build if
code outside ``notifier/`` imports this module or the outbox relay.

Real Telegram / email connectors are out of scope for E1. ``FakeChannel`` is
the only implementation here; it delivers into an in-memory list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from runtime import QueuedMessage, SendFailed

TELEGRAM, EMAIL, POTTER_HOME = "telegram", "email", "potter_home"


class Channel(Protocol):
    name: str

    def send(self, message: QueuedMessage) -> Any:
        """Deliver ``message`` passing ``message.idempotency_key`` to the
        provider. Raise ``SendFailed`` only when delivery DEFINITELY did not
        happen; any other exception is an unknown outcome."""
        ...

    def reconcile(self, message: QueuedMessage) -> str:
        """Ask the provider whether the key landed: ``succeeded``, ``failed``
        or ``still_unknown``."""
        ...

    def probe(self) -> bool:
        """Cheap health check used to end an outage (no message sent)."""
        ...


@dataclass(frozen=True)
class InboundMessage:
    """A message arriving from a channel. ``sender_ref`` is the channel's own
    sender identity (Telegram user id, email From address, home session
    principal). Authentication signals (DKIM, session) are NOT fields here:
    they come from verifiers injected into ``ChannelAuth``, so a caller cannot
    simply claim them."""
    channel: str
    sender_ref: str
    text: str
    reply_to: str | None = None        # notification id being answered, if any
    raw_ref: str | None = None         # opaque handle the verifiers look up


@dataclass
class FakeChannel:
    """In-memory channel. ``up=False`` makes every send fail definitely.
    ``ambiguous_after_delivery`` delivers and then raises a non-SendFailed
    error (the classic unknown outcome). ``on_send`` runs inside the call
    (used to simulate /stop arriving while a send is in flight)."""
    name: str
    up: bool = True
    ambiguous_after_delivery: bool = False
    crash_after_delivery: BaseException | None = None
    reconcile_verdict: str = "still_unknown"
    on_send: Callable[[QueuedMessage], None] | None = None
    delivered: list[QueuedMessage] = field(default_factory=list)
    calls: int = 0

    def send(self, message: QueuedMessage) -> Any:
        self.calls += 1
        if not self.up:
            raise SendFailed(f"{self.name} unavailable")
        if self.on_send is not None:
            self.on_send(message)
        self.delivered.append(message)
        if self.crash_after_delivery is not None:
            exc, self.crash_after_delivery = self.crash_after_delivery, None
            raise exc
        if self.ambiguous_after_delivery:
            raise TimeoutError("response lost after delivery")
        return {"channel": self.name, "provider_id": f"{self.name}-{len(self.delivered)}"}

    def reconcile(self, message: QueuedMessage) -> str:
        return self.reconcile_verdict

    def probe(self) -> bool:
        return self.up

    def delivered_notifications(self) -> list[str]:
        return [m.notification_id for m in self.delivered]
