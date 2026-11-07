"""IngressGateway: inbound Telegram / email / webhook / web -> InboundEvent (package E3).

The untrusted-source boundary (T11/T12; T19 PASS 2 items 2 and 4):

* Provenance comes from authentication results only. ``principal_direct`` is
  emitted only for the command text of a sender that (a) passes the channel's
  own authentication and (b) E1's ``ChannelAuth`` maps to an allowlisted,
  enabled principal. Everything else is an untrusted kind (``events.py``).
* Telegram: the transport verifier must confirm the update came from Telegram
  (webhook secret token, checked by the injected verifier). The user id must
  be bound and allowlisted in ``ChannelAuth``; the chat must be the private
  chat with that user; forwarded or via-bot content is untrusted.
* Email: the injected verifier returns DKIM/SPF verdicts for the exact raw
  bytes. ``principal_direct`` requires one From header with one address, no
  header defects, no address inside the display name, Reply-To and Sender
  (if present) equal to From, DKIM ``pass`` with ``d=`` equal to the From
  domain, SPF not ``fail``/``softfail``, and ``ChannelAuth`` (which itself
  requires a DKIM pass) for the address. Any failure -> ``external_sender``.
  Forwarded and quoted runs are untrusted even in Josh's own mail.
* Webhooks need a per-source signature verifier; unsigned or unknown sources
  are rejected. Verified payloads are still ``webhook_payload`` (untrusted).
* Content: nothing in the text is parsed for provenance or approval. Events
  have no approval field. Attachments go to the injected store; the event
  holds a reference. Size limits reject oversized input. Injection indicators
  are flags only.
* Dedupe by channel message id, scoped by the authentication namespace for
  email (an unauthenticated sender cannot squat Josh's Message-ID). The same
  content re-delivered -> ``duplicate`` (no new event); different content
  under a seen id -> ``replay_conflict``.
* One C6 trace per decision; reasons are fixed codes; no text is logged.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from email import policy as email_policy
from email.parser import BytesParser
from typing import Callable, Mapping

from notifier import InboundMessage
from notifier.auth import ChannelAuth

from .content import IngressLimits, injection_flags, is_forward_subject, split_body
from .events import (EMAIL, EXTERNAL_SENDER, FORWARDED_CONTENT, PRINCIPAL_DIRECT, TELEGRAM,
                     WEB, WEB_CONTENT, WEBHOOK, WEBHOOK_PAYLOAD, AttachmentRef, InboundEvent,
                     Segment, canon, sha256)
from .store import AttachmentStore, IngressStore
from .traces import Sink, emit_ingress_trace, ingress_uuid

# Closed reason table (decision reasons and trust reasons).
REASONS = frozenset({
    # accepted
    "accepted",
    # rejected
    "duplicate", "replay_conflict", "transport_unverified", "webhook_unverified",
    "malformed", "missing_message_id", "oversized_raw", "oversized_body",
    "oversized_attachment", "too_many_attachments", "bot_sender", "sender_not_allowlisted",
    "trace_error", "ingress_error",
    # trust reasons (why an accepted event is untrusted; "ok" when principal_direct)
    "ok", "multiple_from", "header_defect", "display_name_spoof", "reply_to_mismatch",
    "sender_mismatch", "dkim_missing", "dkim_failed", "dkim_unaligned", "spf_failed",
    "verifier_error", "unknown_sender", "not_allowlisted", "principal_disabled",
    "dkim_unverified", "unvouched_sender", "non_private_chat", "via_bot", "unauthenticated_channel",
})


# ---- raw inputs (what an adapter hands the gateway) --------------------------------

@dataclass(frozen=True)
class RawAttachment:
    data: bytes = field(repr=False)
    media_type: str = "application/octet-stream"
    filename: str | None = None


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    message_id: int
    chat_id: int
    chat_type: str
    from_user_id: int | None
    text: str = field(default="", repr=False)
    from_is_bot: bool = False
    forwarded: bool = False          # any forward_origin / forward_from on the message
    via_bot: bool = False
    attachments: tuple[RawAttachment, ...] = ()
    delivery_ref: str | None = None  # handle the transport verifier checks


@dataclass(frozen=True)
class WebhookDelivery:
    source: str
    delivery_id: str
    body: bytes = field(repr=False)
    delivery_ref: str | None = None  # handle the signature verifier checks


@dataclass(frozen=True)
class EmailAuthVerdict:
    """From the injected verifier for one exact raw message."""
    dkim: str                        # "pass" | "fail" | "none" | ...
    dkim_domain: str | None = None   # d= of the passing signature
    spf: str = "none"                # "pass" | "fail" | "softfail" | "neutral" | "none"


EmailVerifier = Callable[[bytes], EmailAuthVerdict]
TransportVerifier = Callable[[TelegramUpdate], bool]
WebhookVerifier = Callable[[WebhookDelivery], bool]


@dataclass(frozen=True)
class IngressResult:
    accepted: bool
    reason: str
    event_id: str | None = None
    event: InboundEvent | None = field(default=None, repr=False)
    duplicate: bool = False
    trust_reason: str | None = None  # accepted events only: "ok" or why untrusted


class _Reject(Exception):
    def __init__(self, reason: str, *, record: bool):
        super().__init__(reason)
        self.reason, self.record = reason, record
        self.draft: "_Draft | None" = None

    def with_draft(self, draft: "_Draft") -> "_Reject":
        self.draft = draft
        return self


@dataclass
class _Draft:
    channel: str
    dedupe_key: str
    message_id: str
    content_digest: str
    principal: str | None
    segments: tuple[Segment, ...]
    attachments: tuple[RawAttachment, ...]
    trust_reason: str


class IngressGateway:
    def __init__(self, *, store: IngressStore, attachments: AttachmentStore,
                 channel_auth: ChannelAuth, clock: Callable[[], int],
                 telegram_transport: TransportVerifier | None = None,
                 email_verifier: EmailVerifier | None = None,
                 webhook_verifiers: Mapping[str, WebhookVerifier] | None = None,
                 limits: IngressLimits = IngressLimits(),
                 trace_sink: Sink | None = None, environment: str = "synthetic"):
        self._store = store
        self._attachments = attachments
        self._auth = channel_auth
        self._clock = clock
        self._tg = telegram_transport
        self._email = email_verifier
        self._webhooks = dict(webhook_verifiers or {})
        self._limits = limits
        self._sink = trace_sink
        self._env = environment

    # ---- public adapters -------------------------------------------------------

    def ingest_telegram(self, update: TelegramUpdate) -> IngressResult:
        return self._run(TELEGRAM, lambda: self._telegram(update))

    def ingest_email(self, raw: bytes) -> IngressResult:
        return self._run(EMAIL, lambda: self._email_draft(raw))

    def ingest_webhook(self, delivery: WebhookDelivery) -> IngressResult:
        return self._run(WEBHOOK, lambda: self._webhook(delivery))

    def ingest_web(self, *, fetch_id: str, url: str, body: str) -> IngressResult:
        return self._run(WEB, lambda: self._web(fetch_id, url, body))

    def vouches(self, event: object) -> bool:
        """True only for an event this gateway minted and recorded, unchanged."""
        if not isinstance(event, InboundEvent):
            return False
        try:
            stored = self._store.lookup_event(event.event_id)
            return stored is not None and stored == event.digest()
        except Exception:
            return False

    # ---- channel parsing -----------------------------------------------------

    def _telegram(self, u: TelegramUpdate) -> _Draft:
        if not isinstance(u, TelegramUpdate):
            raise _Reject("malformed", record=False)
        if not self._verified(self._tg, u):
            raise _Reject("transport_unverified", record=False)
        for v in (u.update_id, u.message_id, u.chat_id):
            if not isinstance(v, int) or isinstance(v, bool):
                raise _Reject("malformed", record=False)
        if not isinstance(u.text, str) or not isinstance(u.chat_type, str) or not isinstance(
                u.attachments, tuple):
            raise _Reject("malformed", record=False)
        key = f"telegram|{u.chat_id}|{u.message_id}"
        digest = sha256(canon({
            "chat": u.chat_id, "chat_type": u.chat_type, "from": u.from_user_id,
            "bot": u.from_is_bot, "fwd": u.forwarded, "via": u.via_bot, "text": u.text,
            "att": [sha256(a.data) for a in u.attachments]}))
        draft = _Draft(TELEGRAM, key, f"{u.chat_id}:{u.message_id}", digest, None, (),
                       u.attachments, "ok")
        self._check_sizes(u.text, u.attachments, draft)
        if u.from_user_id is None or not isinstance(u.from_user_id, int):
            raise _Reject("malformed", record=False)
        if u.from_is_bot:
            raise _Reject("bot_sender", record=True).with_draft(draft)
        auth = self._auth.authenticate(InboundMessage(TELEGRAM, str(u.from_user_id), "",
                                                      raw_ref=u.delivery_ref))
        if self._vouched_principal(auth, TELEGRAM) is None:
            # A stranger DMing the bot does not drive the runner (E1: ignored + logged).
            raise _Reject("sender_not_allowlisted", record=True).with_draft(draft)
        if u.via_bot:
            trust = "via_bot"
        elif u.chat_type != "private" or u.chat_id != u.from_user_id:
            trust = "non_private_chat"
        else:
            trust = "ok"
        own = PRINCIPAL_DIRECT if trust == "ok" else EXTERNAL_SENDER
        if u.forwarded:
            segs = (Segment(FORWARDED_CONTENT, u.text),) if u.text else ()
        else:
            segs = (Segment(own, u.text),) if u.text else ()
        draft.segments = segs
        draft.principal = (self._vouched_principal(auth, TELEGRAM)
                           if own == PRINCIPAL_DIRECT else None)
        draft.trust_reason = trust
        return draft

    def _email_draft(self, raw: bytes) -> _Draft:
        if not isinstance(raw, (bytes, bytearray)):
            raise _Reject("malformed", record=False)
        raw = bytes(raw)
        if len(raw) > self._limits.max_raw_bytes:
            raise _Reject("oversized_raw", record=False)
        try:
            msg = BytesParser(policy=email_policy.default).parsebytes(raw)
        except Exception:
            raise _Reject("malformed", record=False)
        mids = msg.get_all("Message-ID") or []
        mid = str(mids[0]).strip() if len(mids) == 1 else ""
        if not mid or len(mid) > self._limits.max_message_id_len:
            raise _Reject("missing_message_id", record=False)

        # -- sender authentication -------------------------------------------
        trust, addr, principal = self._email_trust(msg, raw, mid)
        own = PRINCIPAL_DIRECT if trust == "ok" else EXTERNAL_SENDER
        ns = principal if own == PRINCIPAL_DIRECT else f"untrusted:{sha256(addr.encode())}"
        key = f"email|{ns}|{mid}"

        # -- content ---------------------------------------------------------------
        text, atts = self._email_parts(msg)
        subject = str(msg.get("Subject", "") or "")
        draft = _Draft(EMAIL, key, mid, sha256(raw), None, (), tuple(atts), trust)
        self._check_sizes(subject + text, draft.attachments, draft)
        fwd = is_forward_subject(subject)
        segs: list[Segment] = []
        if subject:
            segs.append(Segment(FORWARDED_CONTENT if fwd else own, subject))
        segs.extend(split_body(text, own, forwarded=fwd))
        draft.segments = tuple(segs)
        if any(s.provenance == PRINCIPAL_DIRECT for s in segs):
            draft.principal = principal
        return draft

    def _email_trust(self, msg, raw: bytes, mid: str) -> tuple[str, str, str | None]:
        froms = msg.get_all("From") or []
        addr = ""
        try:
            if len(froms) != 1 or len(froms[0].addresses) != 1:
                return "multiple_from", str(froms[0]) if froms else "", None
            hdr = froms[0]
            a = hdr.addresses[0]
            addr = (a.addr_spec or "").lower()
            if hdr.defects or not a.username or not a.domain:
                return "header_defect", addr, None
            if "@" in (a.display_name or ""):
                return "display_name_spoof", addr, None
            for name, code in (("Reply-To", "reply_to_mismatch"), ("Sender", "sender_mismatch")):
                hs = msg.get_all(name) or []
                for h in hs:
                    if h.defects or any((x.addr_spec or "").lower() != addr
                                        for x in h.addresses) or not h.addresses:
                        return code, addr, None
        except Exception:
            return "header_defect", addr, None
        domain = addr.rsplit("@", 1)[-1]
        if self._email is None:
            return "dkim_missing", addr, None
        try:
            v = self._email(raw)
            if not isinstance(v, EmailAuthVerdict):
                return "verifier_error", addr, None
        except Exception:
            return "verifier_error", addr, None
        if v.dkim == "none":
            return "dkim_missing", addr, None
        if v.dkim != "pass":
            return "dkim_failed", addr, None
        if not isinstance(v.dkim_domain, str) or v.dkim_domain.lower() != domain:
            return "dkim_unaligned", addr, None
        if v.spf in ("fail", "softfail"):
            return "spf_failed", addr, None
        auth = self._auth.authenticate(InboundMessage(EMAIL, addr, "", raw_ref=mid))
        principal = self._vouched_principal(auth, EMAIL)
        if principal is None:
            reason = getattr(auth, "reason", None)
            return (reason if reason in REASONS and reason != "ok" else "unvouched_sender"), \
                addr, None
        return "ok", addr, principal

    def _vouched_principal(self, auth: object, channel: str) -> str | None:
        """The principal only if E1 ``ChannelAuth`` itself minted this exact
        voucher for this channel (``vouches``). A hand-constructed or copied
        ``AuthenticatedSender`` never yields ``principal_direct``."""
        try:
            sender = getattr(auth, "sender", None)
            if sender is None or not self._auth.vouches(sender):
                return None
            if sender.channel != channel or not isinstance(sender.principal, str):
                return None
            return sender.principal or None
        except Exception:
            return None

    @staticmethod
    def _email_parts(msg) -> tuple[str, list[RawAttachment]]:
        texts: list[str] = []
        htmls: list[RawAttachment] = []
        atts: list[RawAttachment] = []
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disp = part.get_content_disposition()
            fname = part.get_filename()
            if disp != "attachment" and fname is None and ctype == "text/plain":
                try:
                    texts.append(part.get_content())
                    continue
                except Exception:
                    pass  # undecodable -> keep as an opaque attachment
            data = part.get_payload(decode=True)
            if data is None:
                data = part.as_bytes()
            ra = RawAttachment(data, ctype, fname)
            (htmls if ctype == "text/html" and disp != "attachment" and fname is None
             else atts).append(ra)
        if not texts:
            atts = htmls + atts       # HTML-only body: by reference, never parsed
        return "\n".join(texts), atts

    def _webhook(self, d: WebhookDelivery) -> _Draft:
        if not isinstance(d, WebhookDelivery) or not isinstance(d.source, str) or not isinstance(
                d.delivery_id, str) or not d.delivery_id or not isinstance(d.body, bytes):
            raise _Reject("malformed", record=False)
        if not self._verified(self._webhooks.get(d.source), d):
            raise _Reject("webhook_unverified", record=False)
        if len(d.body) > self._limits.max_raw_bytes:
            raise _Reject("oversized_raw", record=False)
        try:
            text = d.body.decode("utf-8")
        except UnicodeDecodeError:
            raise _Reject("malformed", record=False)
        draft = _Draft(WEBHOOK, f"webhook|{d.source}|{d.delivery_id}", d.delivery_id,
                       sha256(d.body), None, (Segment(WEBHOOK_PAYLOAD, text),), (),
                       "unauthenticated_channel")
        self._check_sizes(text, (), draft)
        return draft

    def _web(self, fetch_id: str, url: str, body: str) -> _Draft:
        if not all(isinstance(x, str) for x in (fetch_id, url, body)) or not fetch_id:
            raise _Reject("malformed", record=False)
        draft = _Draft(WEB, f"web|{fetch_id}", fetch_id, sha256(canon([url, body])), None,
                       (Segment(WEB_CONTENT, body),), (), "unauthenticated_channel")
        self._check_sizes(body, (), draft)
        return draft

    # ---- shared --------------------------------------------------------------

    def _check_sizes(self, text: str, atts: tuple[RawAttachment, ...], draft: _Draft) -> None:
        if len(text.encode("utf-8", "surrogatepass")) > self._limits.max_text_bytes:
            raise _Reject("oversized_body", record=True).with_draft(draft)
        if len(atts) > self._limits.max_attachments:
            raise _Reject("too_many_attachments", record=True).with_draft(draft)
        for a in atts:
            if not isinstance(a, RawAttachment) or not isinstance(a.data, bytes):
                raise _Reject("malformed", record=False)
            if len(a.data) > self._limits.max_attachment_bytes:
                raise _Reject("oversized_attachment", record=True).with_draft(draft)

    @staticmethod
    def _verified(verifier, item) -> bool:
        if verifier is None:
            return False
        try:
            return verifier(item) is True
        except Exception:
            return False

    def _run(self, channel: str, parse: Callable[[], _Draft]) -> IngressResult:
        try:
            now = self._clock()
            if not isinstance(now, int) or isinstance(now, bool):
                raise RuntimeError("clock")
        except Exception:
            return IngressResult(False, "ingress_error")
        try:
            try:
                draft = parse()
            except _Reject as r:
                draft = r.draft
                if not r.record or draft is None:
                    return self._unrecorded(channel, r.reason, now)
                return self._finalize(draft, now, reject=r.reason)
            return self._finalize(draft, now, reject=None)
        except Exception:
            return self._unrecorded(channel, "ingress_error", now)

    def _unrecorded(self, channel: str, reason: str, now: int) -> IngressResult:
        event_id = ingress_uuid(f"unrecorded|{uuid.uuid4()}")
        try:
            emit_ingress_trace(self._sink, event_id=event_id, delivery_key=str(uuid.uuid4()),
                               channel=channel, accepted=False, reason=reason, trust="none",
                               content_digest="0" * 64, now=now, environment=self._env)
        except Exception:
            pass   # already a rejection; nothing to roll back
        return IngressResult(False, reason)

    def _finalize(self, d: _Draft, now: int, *, reject: str | None) -> IngressResult:
        key_digest = sha256(d.dedupe_key.encode())
        event_id = ingress_uuid(d.dedupe_key)
        delivery = str(uuid.uuid4())
        trace_failed = False

        def trace(accepted: bool, reason: str, trust: str) -> None:
            nonlocal trace_failed
            trace_failed = True
            emit_ingress_trace(self._sink, event_id=event_id, delivery_key=delivery,
                               channel=d.channel, accepted=accepted, reason=reason, trust=trust,
                               content_digest=d.content_digest, now=now, environment=self._env)
            trace_failed = False

        try:
            with self._store.transaction() as tx:
                prior = tx.get(key_digest)
                if prior is not None:
                    reason = ("duplicate" if prior.content_digest == d.content_digest
                              else "replay_conflict")
                    trace(False, reason, "none")
                    return IngressResult(False, reason, event_id=prior.event_id,
                                         duplicate=reason == "duplicate")
                if reject is not None:
                    tx.put(dedupe_key=key_digest, channel=d.channel, event_id=event_id,
                           content_digest=d.content_digest, event_digest=None,
                           decision="rejected", reason=reject, trust="none", received_at=now)
                    trace(False, reject, "none")
                    return IngressResult(False, reject, event_id=event_id)
                event = self._mint(d, event_id, now)
                trust = "principal_direct" if event.trusted else "untrusted"
                tx.put(dedupe_key=key_digest, channel=d.channel, event_id=event_id,
                       content_digest=d.content_digest, event_digest=event.digest(),
                       decision="accepted", reason="accepted", trust=trust, received_at=now)
                trace(True, d.trust_reason, trust)
                return IngressResult(True, "accepted", event_id=event_id, event=event,
                                     trust_reason=d.trust_reason)
        except Exception:
            # Rolled back (trace sink or store failure): nothing recorded, so a
            # re-delivery is processed afresh. Fail closed.
            return IngressResult(False, "trace_error" if trace_failed else "ingress_error")

    def _mint(self, d: _Draft, event_id: str, now: int) -> InboundEvent:
        refs = tuple(AttachmentRef(self._attachments.put(a.data), sha256(a.data), len(a.data),
                                   a.media_type, a.filename) for a in d.attachments)
        segs = d.segments
        principal = d.principal if any(s.provenance == PRINCIPAL_DIRECT for s in segs) else None
        if principal is None and any(s.provenance == PRINCIPAL_DIRECT for s in segs):
            raise RuntimeError("principal_direct without principal")
        return InboundEvent(event_id=event_id, channel=d.channel,
                            channel_message_id=d.message_id, received_at=now,
                            principal=principal, segments=segs, attachments=refs,
                            flags=injection_flags(segs))
