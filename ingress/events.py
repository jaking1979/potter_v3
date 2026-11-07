"""Normalized inbound events and provenance kinds (package E3; T11/T12, T19 items 2+4).

Provenance is structural. It is decided by the gateway from authentication
results only (``gateway.py``), never from what a message says. An event has no
approval field and no way to carry one: approvals come only from E1.

C1 trusts exactly ``principal_direct`` and ``system_policy``. Ingress never
emits ``system_policy``, and emits ``principal_direct`` only for the command
text of an authenticated, allowlisted principal on an authenticated channel.
Every other kind below is untrusted, and C1 treats any request it shaped as
tainted (a write then needs an approval).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from policy import TRUSTED_SOURCES

# ---- provenance kinds ----------------------------------------------------------

PRINCIPAL_DIRECT = "principal_direct"     # authenticated Josh command (the only trusted kind)
EXTERNAL_SENDER = "external_sender"       # message text from anyone not authenticated as Josh
FORWARDED_CONTENT = "forwarded_content"   # forwarded material, even inside Josh's own message
QUOTED_CONTENT = "quoted_content"         # quoted earlier messages ("> ...")
ATTACHMENT = "attachment"                 # attachments (stored by reference, never read)
WEBHOOK_PAYLOAD = "webhook_payload"       # signed third-party webhook body
WEB_CONTENT = "web_content"               # fetched web pages

UNTRUSTED_KINDS = frozenset({EXTERNAL_SENDER, FORWARDED_CONTENT, QUOTED_CONTENT, ATTACHMENT,
                             WEBHOOK_PAYLOAD, WEB_CONTENT})
ALL_KINDS = UNTRUSTED_KINDS | {PRINCIPAL_DIRECT}

# Import-time invariant: nothing ingress labels untrusted may be trusted by C1,
# and ingress can never emit C1's other trusted kind.
assert not (UNTRUSTED_KINDS & TRUSTED_SOURCES), "untrusted kind trusted by C1"
assert PRINCIPAL_DIRECT in TRUSTED_SOURCES
assert "system_policy" not in ALL_KINDS

TELEGRAM, EMAIL, WEBHOOK, WEB = "telegram", "email", "webhook", "web"
CHANNELS = frozenset({TELEGRAM, EMAIL, WEBHOOK, WEB})


def canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Segment:
    """One run of content with a single provenance kind."""
    provenance: str
    text: str = field(repr=False)

    def __post_init__(self):
        if self.provenance not in ALL_KINDS or not isinstance(self.text, str):
            raise ValueError("invalid segment")


@dataclass(frozen=True)
class AttachmentRef:
    """An attachment held in the injected store. The event carries only this
    reference; ingress never reads, decodes or interprets the bytes.
    ``filename`` and ``media_type`` are sender-supplied (untrusted)."""
    ref: str
    sha256: str
    size: int
    media_type: str = field(repr=False)
    filename: str | None = field(default=None, repr=False)
    provenance: str = ATTACHMENT


@dataclass(frozen=True)
class InboundEvent:
    """What the runner consumes. Immutable. Construct only via IngressGateway;
    the runner must check ``gateway.vouches(event)`` before acting on it, so a
    hand-built event claiming ``principal_direct`` is refused."""
    event_id: str                          # UUIDv5(channel, dedupe key)
    channel: str
    channel_message_id: str
    received_at: int
    principal: str | None                  # set only when a principal_direct segment exists
    segments: tuple[Segment, ...] = field(repr=False)
    attachments: tuple[AttachmentRef, ...] = field(default=(), repr=False)
    flags: frozenset[str] = frozenset()    # injection indicators: informational, never upgrade

    @property
    def provenance(self) -> tuple[str, ...]:
        """Source kinds for C1 ``PolicyRequest.provenance`` (sorted, distinct).
        Any request shaped by any part of this event must carry all of them."""
        kinds = {s.provenance for s in self.segments} | {a.provenance for a in self.attachments}
        return tuple(sorted(kinds))

    @property
    def trusted(self) -> bool:
        return self.provenance == (PRINCIPAL_DIRECT,)

    @property
    def tainted(self) -> bool:
        """True if any part is untrusted. Taint is per event, not per segment:
        anything derived from a tainted event (summaries, plans, requests)
        must carry ``provenance`` in full, never only its direct segments."""
        return not self.trusted

    def digest(self) -> str:
        return sha256(canon({
            "event_id": self.event_id, "channel": self.channel,
            "channel_message_id": self.channel_message_id, "received_at": self.received_at,
            "principal": self.principal,
            "segments": [[s.provenance, s.text] for s in self.segments],
            "attachments": [[a.ref, a.sha256, a.size, a.media_type, a.filename, a.provenance]
                            for a in self.attachments],
            "flags": sorted(self.flags)}))
