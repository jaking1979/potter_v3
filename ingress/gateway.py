"""IngressGateway: inbound Telegram / email / webhook / web -> InboundEvent (package E3).

The untrusted-source boundary (T11/T12; T19 PASS 2 items 2 and 4):

* Provenance comes from authentication results only. ``principal_direct`` is
  emitted only for the command text of a sender that (a) passes the channel's
  own authentication and (b) E1's ``ChannelAuth`` maps to an allowlisted,
  enabled principal. Everything else is an untrusted kind (``events.py``).
* E1 handoff: ingress is the trusted channel adapter. After the platform proof
  passes, it signs the exact update with the channel's ``TransportSigner`` and
  ``ChannelAuth`` verifies MAC, freshness and replay; the resulting voucher
  must match this channel and this message. The envelope carries a SHA-256
  commitment to the text (never the body), the voucher is retired at once,
  and the E1 row is marked processed after E3 commits. Ingress never
  constructs an ``InboundMessage``.
* Telegram: the transport verifier must confirm the update came from Telegram
  (webhook secret token, checked by the injected verifier). The user id must
  be bound and allowlisted in ``ChannelAuth``; the chat must be the private
  chat with that user; forwarded or via-bot content is untrusted.
* Email: the injected verifier returns DKIM/SPF verdicts for the exact raw
  bytes. ``principal_direct`` requires one From header with one address, no
  header defects, no address inside the display name, Reply-To and Sender
  (if present) equal to From, DKIM ``pass`` with ``d=`` equal to the From
  domain, SPF not ``fail``/``softfail``, the From domain in the configured
  ``email_attested_domains`` (empty by default -> email is never direct), the
  verifier's ``mailbox_bound`` attestation, and a vouched ``ChannelAuth``
  sender. Any failure -> ``external_sender``. Only the text before the first
  quote/forward/pasted-header boundary can be direct; the rest of the body is
  untrusted (fail closed; see ``content.py`` for the residual limitation).
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

import json
import uuid
from dataclasses import dataclass, field
from email import policy as email_policy
from email.parser import BytesParser
from typing import Callable, Mapping

from notifier.auth import ChannelAuth, TransportSigner

from .content import IngressLimits, injection_flags, is_forward_subject, split_body
from .events import (EMAIL, EXTERNAL_SENDER, FORWARDED_CONTENT, PRINCIPAL_DIRECT, TELEGRAM,
                     WEB, WEB_CONTENT, WEBHOOK, WEBHOOK_PAYLOAD, AttachmentRef, InboundEvent,
                     Segment, canon, sha256)
from .store import AttachmentStore, IngressStore, attachment_ref
from .traces import Sink, emit_ingress_trace, ingress_uuid

# Closed reason table (decision reasons and trust reasons).
REASONS = frozenset({
    # accepted
    "accepted",
    # rejected / not new
    "duplicate", "duplicate_pending", "expired", "quarantined", "corrupt_pending", "quarantine_failed", "replay_conflict", "transport_unverified", "webhook_unverified",
    "malformed", "missing_message_id", "oversized_raw", "oversized_body",
    "oversized_attachment", "too_many_attachments", "bot_sender", "sender_not_allowlisted",
    "trace_error", "ingress_error", "attachment_reclaimed", "clock_regression",
    "needs_operator", "transport_conflict", "pending_conflict", "needs_reconciliation",
    # trust reasons (why an accepted event is untrusted; "ok" when principal_direct)
    "ok", "multiple_from", "header_defect", "display_name_spoof", "reply_to_mismatch",
    "sender_mismatch", "dkim_missing", "dkim_failed", "dkim_unaligned", "spf_failed",
    "email_domain_not_attested", "mailbox_unbound",
    "verifier_error", "unknown_sender", "not_allowlisted", "principal_disabled",
    "dkim_unverified", "unvouched_sender", "unauthenticated_transport",
    "stale_transport", "replayed", "unknown_channel", "non_private_chat", "via_bot", "unauthenticated_channel",
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
    # DKIM proves the signing DOMAIN and integrity, not that the mailbox owner
    # sent it. True only when the verifier can attest, for this exact message,
    # that the signing provider authenticated the submitting user AS the From
    # mailbox (e.g. a provider that refuses unverified From addresses, observed
    # via its own authenticated-submission evidence). Default False.
    mailbox_bound: bool = False


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


class _AttachmentLost(Exception):
    """A staged attachment was collected before the ingest could commit."""


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
    voucher: object = None      # E1 voucher (already retired) to finish() after commit


# E1 refusals that say nothing about the sender - the transport is paused
# (clock rollback) or the record awaits an operator. They must never become a
# recorded rejection (which would turn the sender's retry into a duplicate of
# it) nor an accepted untrusted event: unrecorded, retryable rejections.
# ``stale_transport`` is included: it is returned only after E3 has already
# re-signed once with a fresh sent_at, i.e. E3's and E1's clocks disagree.
# ``transport_conflict``: E1 answered ``pending_conflict`` (E1 f37086f) - a
# DIFFERENT update is pending under this key (e.g. an altered retry after an
# E3 rollback). Never recorded, so it cannot pre-empt the genuine update,
# whose identical envelope still resumes the pending row; never accepted.
E1_RETRYABLE = frozenset({"clock_regression", "needs_operator", "stale_transport",
                          "transport_conflict"})
# ``needs_reconciliation``: on a FIRST handoff (E3 has no committed decision
# for this key) E1 answered ``replayed`` - it holds a PROCESSED row E3 never
# committed (E3 store lost or restored, or an E1 row written by another path).
# Nothing can resume it, so retrying would loop forever. It is a durable,
# explicit safe failure: recorded (re-deliveries are then plain duplicates),
# never accepted, no event, no effect; listed by needs_reconciliation() for an
# operator.
NEEDS_RECONCILIATION = "needs_reconciliation"
# E1 refusals after which E1 holds a settled (processed) row for the update.
_E1_SETTLED = frozenset({"replayed", "unknown_sender", "not_allowlisted",
                         "principal_disabled", "dkim_unverified", "session_unverified"})


def bind_update(text: str, content_digest: str) -> str:
    """What the E1 envelope carries: a SHA-256 commitment to the message text
    AND the update's full content digest - every trust- and event-relevant
    transport field (Telegram: chat, chat type, sender, bot / forwarded /
    via-bot flags, text, attachment bytes, media types and filenames; email:
    the raw message bytes, so all headers and attachments). The MAC therefore
    binds the exact update, so E1's pending recovery can only resume an
    identical update, never one whose provenance or attachments changed;
    and E1 never stores or holds the body."""
    return "e3-sha256:" + sha256(canon([text, content_digest]))


class IngressGateway:
    def __init__(self, *, store: IngressStore, attachments: AttachmentStore,
                 channel_auth: ChannelAuth, clock: Callable[[], int],
                 transport_signers: Mapping[str, TransportSigner] | None = None,
                 attachment_grace_s: int = 3600,
                 telegram_transport: TransportVerifier | None = None,
                 email_verifier: EmailVerifier | None = None,
                 email_attested_domains: frozenset[str] = frozenset(),
                 webhook_verifiers: Mapping[str, WebhookVerifier] | None = None,
                 limits: IngressLimits = IngressLimits(),
                 trace_sink: Sink | None = None, environment: str = "synthetic"):
        self._store = store
        self._attachments = attachments
        self._auth = channel_auth
        # Replay-record retention is E1's (225e7df prunes its own processed
        # rows with its own max_skew and never prunes pending ones). E3 never
        # deletes from E1's table, so no E3 setting can reopen replay.
        if attachment_grace_s < 0:
            raise ValueError("attachment_grace_s must be >= 0")
        self._grace = int(attachment_grace_s)
        # E1 c8c8422: ingress is the trusted channel adapter. After the platform
        # proof passes, it signs the exact update (sender, text, ids, time)
        # with the channel's transport key; ChannelAuth verifies that MAC,
        # freshness and envelope replay. No signer for a channel -> nothing on
        # it is ever authenticated. Ingress never builds an InboundMessage.
        self._signers = {ch: s for ch, s in (transport_signers or {}).items()
                         if isinstance(s, TransportSigner) and s.channel == ch}
        self._clock = clock
        self._tg = telegram_transport
        self._email = email_verifier
        # Domains whose signing provider is attested (configuration, a Design
        # Council decision) to bind DKIM signatures to the authenticated
        # mailbox. Empty by default: email is then never principal_direct.
        self._email_domains = frozenset(d.lower() for d in email_attested_domains)
        self._webhooks = dict(webhook_verifiers or {})
        self._limits = limits
        self._sink = trace_sink
        self._env = environment
        # event_id -> (reason, attempts) for rows whose quarantine AND block
        # record both failed; process-local fallback so pending() still moves on.
        self._volatile_blocked: dict[str, tuple[str, int]] = {}

    # ---- public adapters -------------------------------------------------------

    def ingest_telegram(self, update: TelegramUpdate) -> IngressResult:
        return self._run(TELEGRAM, lambda: self._telegram(update))

    def ingest_email(self, raw: bytes) -> IngressResult:
        return self._run(EMAIL, lambda: self._email_draft(raw))

    def ingest_webhook(self, delivery: WebhookDelivery) -> IngressResult:
        return self._run(WEBHOOK, lambda: self._webhook(delivery))

    def ingest_web(self, *, fetch_id: str, url: str, body: str) -> IngressResult:
        return self._run(WEB, lambda: self._web(fetch_id, url, body))

    # ---- durable handoff (runner side) ----------------------------------------

    MAX_SCAN = 1000     # rows examined per pending() call (bounded work)
    RETRY_BLOCKED = 16  # blocked rows whose quarantine is retried per pending() call

    def pending(self, limit: int = 100, max_scan: int | None = None) -> list[InboundEvent]:
        """Accepted events not yet acked as handed off, oldest first (up to
        ``limit`` valid ones). Call on runner start-up to recover anything
        accepted before a crash.

        No starvation: the scan uses a rowid cursor and examines at most
        ``max_scan`` rows (default ``MAX_SCAN``) per call. A row that cannot
        be restored - ``malformed`` or ``digest_mismatch`` against the digest
        minted at acceptance - is quarantined in one transaction: durably moved
        out of ``pending`` (never returned, accepted, acked or retried), its
        untrustworthy text dropped, traced as ``corrupt_pending``, and listed
        by ``quarantined()`` as (event_id, reason). Each call therefore makes
        forward progress, so later valid rows are reached on this or a
        following call, also after a restart.

        Quarantine failure is not silent and cannot stall recovery: the row is
        recorded as *recovery blocked* (``recovery_blocked()``), traced as
        ``quarantine_failed``, and excluded from later scans, so the next call
        reaches later valid rows. If even that record cannot be written, the
        row is held in this process's memory instead. Each call first retries
        quarantine for at most ``RETRY_BLOCKED`` blocked rows (least recently
        tried first); a blocked row is never returned or acked."""
        cap = self.MAX_SCAN if max_scan is None else max_scan
        self._retry_blocked()
        out: list[InboundEvent] = []
        cursor, scanned = 0, 0
        while len(out) < limit and scanned < cap:
            batch = self._store.pending_records(min(limit - len(out), cap - scanned),
                                                after=cursor)
            if not batch:
                break
            for rowid, event_id, raw in batch:
                cursor = rowid
                if event_id in self._volatile_blocked:
                    continue                    # already known bad; not re-examined
                scanned += 1
                ev, why = self._restore_checked(raw, self._store.lookup_event(event_id))
                if ev is not None:
                    out.append(ev)
                else:
                    self._quarantine(event_id, why)
        return out

    def recovery_blocked(self) -> list[tuple[str, str, int]]:
        """(event_id, reason, attempts) of rows that failed to restore and
        whose quarantine write failed. For an operator: no content. They are
        excluded from ``pending()`` and retried on each call until the
        quarantine succeeds (or ``expire_pending`` drops them)."""
        try:
            rows = self._store.blocked()
        except Exception:
            rows = []
        known = {r[0] for r in rows}
        return rows + [(e, why, n) for e, (why, n) in self._volatile_blocked.items()
                       if e not in known]

    def _retry_blocked(self) -> None:
        try:
            durable = self._store.blocked(self.RETRY_BLOCKED)
        except Exception:
            durable = []
        volatile = [(e, why, n) for e, (why, n) in self._volatile_blocked.items()]
        for event_id, why, _n in (durable + volatile)[:self.RETRY_BLOCKED]:
            self._quarantine(event_id, why)

    def quarantined(self) -> list[tuple[str, str]]:
        """(event_id, reason) of inbox rows quarantined as ``malformed`` or
        ``digest_mismatch``. For an operator; no stored text, never returned
        by ``pending()``."""
        return self._store.quarantined()

    def _quarantine(self, event_id: str, why: str) -> bool:
        now = self._clock()
        try:
            done = self._store.quarantine(event_id, now, why)
        except Exception:
            self._block(event_id, why, now)
            return False
        self._volatile_blocked.pop(event_id, None)
        if done:
            try:
                emit_ingress_trace(self._sink, event_id=event_id,
                                   delivery_key=str(uuid.uuid4()), channel="inbox",
                                   accepted=False, reason="corrupt_pending", trust="none",
                                   content_digest="0" * 64, now=now, environment=self._env)
            except Exception:
                pass
        return done

    def _block(self, event_id: str, why: str, now: int) -> None:
        """Quarantine failed: record the row as recovery blocked (durably if
        possible, else in memory) and trace it. Fail closed either way: the
        row stays out of ``pending()`` results."""
        try:
            durable = self._store.block(event_id, why, now)
        except Exception:
            durable = False
        if durable:
            self._volatile_blocked.pop(event_id, None)
        else:
            _why, n = self._volatile_blocked.get(event_id, (why, 0))
            self._volatile_blocked[event_id] = (why, n + 1)
        try:
            emit_ingress_trace(self._sink, event_id=event_id,
                               delivery_key=str(uuid.uuid4()), channel="inbox",
                               accepted=False, reason="quarantine_failed", trust="none",
                               content_digest="0" * 64, now=now, environment=self._env)
        except Exception:
            pass

    def ack(self, event_id: str) -> bool:
        """The runner has durably enqueued this event (idempotently, keyed on
        ``event_id``). Drops the stored text; later re-deliveries are plain
        ``duplicate``. Idempotent."""
        if event_id in self._volatile_blocked:
            return False
        try:
            return self._store.ack(event_id, self._clock())
        except Exception:
            return False

    def expire_pending(self, max_age_s: int) -> list[str]:
        """Retention control: drop the text of events pending longer than
        ``max_age_s`` (they are then lost on purpose, and traced)."""
        now = self._clock()
        ids = self._store.expire(now - max_age_s, now)
        for event_id in ids:
            self._volatile_blocked.pop(event_id, None)
            try:
                emit_ingress_trace(self._sink, event_id=event_id,
                                   delivery_key=str(uuid.uuid4()), channel="expired",
                                   accepted=False, reason="expired", trust="none",
                                   content_digest="0" * 64, now=now, environment=self._env)
            except Exception:
                pass
        return ids

    def needs_reconciliation(self) -> list[tuple[str, str]]:
        """(channel, event_id) of deliveries refused as ``needs_reconciliation``:
        E1 holds a processed record E3 never committed. For an operator; E3
        never accepts or retries them."""
        return [(ch, ev) for ch, ev, _dec, reason, _t, _r in self._store.log()
                if reason == NEEDS_RECONCILIATION]

    def gc_attachments(self, grace_s: int | None = None) -> list[str]:
        """Reclaim blobs left by rolled-back or crashed ingests and blobs
        released by ``expire_pending``: only those staged/released longer than
        ``grace_s`` ago and referenced by no committed event."""
        grace = self._grace if grace_s is None else grace_s
        now = self._clock()
        collected = self._store.collect_garbage(now - grace, self._attachments.delete)
        self._store.mark_orphans(self._attachments.refs(), now)
        return collected

    @staticmethod
    def _restore_checked(raw: str | None, digest: str | None
                         ) -> tuple[InboundEvent | None, str | None]:
        """Rebuild a stored event: (event, None), or (None, "malformed" |
        "digest_mismatch") - it must match the digest minted at acceptance."""
        if not raw or not digest:
            return None, "malformed"
        try:
            ev = InboundEvent.from_record(json.loads(raw))
        except Exception:
            return None, "malformed"
        return (ev, None) if ev.digest() == digest else (None, "digest_mismatch")

    @classmethod
    def _restore(cls, raw: str | None, digest: str | None) -> InboundEvent | None:
        return cls._restore_checked(raw, digest)[0]

    def vouches(self, event: object) -> bool:
        """True only for an event this gateway minted and recorded, unchanged.
        An integrity check, NOT consumption: it stays true on repeated calls,
        so the runner must enforce its own durable idempotency on event_id."""
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
            "att": [[sha256(a.data), a.media_type, a.filename] for a in u.attachments]}))
        draft = _Draft(TELEGRAM, key, f"{u.chat_id}:{u.message_id}", digest, None, (),
                       u.attachments, "ok")
        self._check_sizes(u.text, u.attachments, draft)
        if u.from_user_id is None or not isinstance(u.from_user_id, int):
            raise _Reject("malformed", record=False)
        if u.from_is_bot:
            raise _Reject("bot_sender", record=True).with_draft(draft)
        seen = self._store.seen_content(sha256(key.encode()))
        if seen is not None and seen != digest:
            return draft      # E3 already committed a different update here: replay_conflict
        if seen == digest:
            # Re-delivery of a decision E3 already committed: answer from E3's
            # dedupe, but first settle E1's record for this message. If the
            # process died after E3 committed and before E1 finish(), the row
            # is still pending; re-signing the same deterministic envelope
            # lets E1 resume it and we finish it now. If it is already
            # processed, E1 answers "replayed" and nothing changes.
            _, voucher, _ = self._authenticate(TELEGRAM, str(u.from_user_id),
                                               bind_update(u.text, digest), key,
                                               raw_ref=u.delivery_ref, settle=True)
            self._release(voucher)
            return draft
        principal, draft.voucher, reason = self._authenticate(
            TELEGRAM, str(u.from_user_id), bind_update(u.text, digest), key,
            raw_ref=u.delivery_ref)
        if reason in E1_RETRYABLE:
            raise _Reject(reason, record=False)
        if reason == NEEDS_RECONCILIATION:
            raise _Reject(NEEDS_RECONCILIATION, record=True).with_draft(draft)
        if principal is None:
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
        draft.principal = principal if own == PRINCIPAL_DIRECT else None
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
        text, atts = self._email_parts(msg)
        subject = str(msg.get("Subject", "") or "")
        # The envelope binds the whole message text (subject + plain body), so
        # the voucher is for this exact message, not a blank command.
        # Dedupe namespace: "verified:" only for a fully authenticated sender,
        # so an unauthenticated sender can never squat or pre-empt that key.
        verified_key = f"email|verified:{sha256(self._from_addr(msg).encode())}|{mid}"
        seen = self._store.seen_content(sha256(verified_key.encode()))
        if seen is not None and seen != sha256(raw):
            # E3 already committed a different message under this verified key.
            return _Draft(EMAIL, verified_key, mid, sha256(raw), None, (), (), "ok")
        if seen == sha256(raw):
            addr = self._from_addr(msg)       # settle E1's record (see _telegram)
            _, voucher, _ = self._authenticate(EMAIL, addr,
                                               bind_update(f"{subject}\n{text}", sha256(raw)),
                                               f"email|{sha256(addr.encode())}|{mid}",
                                               raw_ref=mid, settle=True)
            self._release(voucher)
            return _Draft(EMAIL, verified_key, mid, sha256(raw), None, (), (), "ok")
        trust, addr, principal, voucher = self._email_trust(
            msg, raw, mid, bind_update(f"{subject}\n{text}", sha256(raw)))
        if trust in E1_RETRYABLE:
            raise _Reject(trust, record=False)
        if trust == NEEDS_RECONCILIATION:
            # Recorded under the verified key, so re-deliveries are duplicates.
            raise _Reject(NEEDS_RECONCILIATION, record=True).with_draft(
                _Draft(EMAIL, verified_key, mid, sha256(raw), None, (), (), trust))
        own = PRINCIPAL_DIRECT if trust == "ok" else EXTERNAL_SENDER
        key = (verified_key if own == PRINCIPAL_DIRECT
               else f"email|untrusted:{sha256(addr.encode())}|{mid}")

        # -- content ---------------------------------------------------------------
        draft = _Draft(EMAIL, key, mid, sha256(raw), None, (), tuple(atts), trust)
        draft.voucher = voucher
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

    def _email_trust(self, msg, raw: bytes, mid: str,
                     bound_text: str) -> tuple[str, str, str | None, object]:
        froms = msg.get_all("From") or []
        addr = ""
        try:
            if len(froms) != 1 or len(froms[0].addresses) != 1:
                return "multiple_from", str(froms[0]) if froms else "", None, None
            hdr = froms[0]
            a = hdr.addresses[0]
            addr = (a.addr_spec or "").lower()
            if hdr.defects or not a.username or not a.domain:
                return "header_defect", addr, None, None
            if "@" in (a.display_name or ""):
                return "display_name_spoof", addr, None, None
            for name, code in (("Reply-To", "reply_to_mismatch"), ("Sender", "sender_mismatch")):
                hs = msg.get_all(name) or []
                for h in hs:
                    if h.defects or any((x.addr_spec or "").lower() != addr
                                        for x in h.addresses) or not h.addresses:
                        return code, addr, None, None
        except Exception:
            return "header_defect", addr, None, None
        domain = addr.rsplit("@", 1)[-1]
        if self._email is None:
            return "dkim_missing", addr, None, None
        try:
            v = self._email(raw)
            if not isinstance(v, EmailAuthVerdict):
                return "verifier_error", addr, None, None
        except Exception:
            return "verifier_error", addr, None, None
        if v.dkim == "none":
            return "dkim_missing", addr, None, None
        if v.dkim != "pass":
            return "dkim_failed", addr, None, None
        if not isinstance(v.dkim_domain, str) or v.dkim_domain.lower() != domain:
            return "dkim_unaligned", addr, None, None
        if v.spf in ("fail", "softfail"):
            return "spf_failed", addr, None, None
        if domain not in self._email_domains:
            return "email_domain_not_attested", addr, None, None
        if v.mailbox_bound is not True:
            return "mailbox_unbound", addr, None, None
        principal, voucher, reason = self._authenticate(
            EMAIL, addr, bound_text, f"email|{sha256(addr.encode())}|{mid}", raw_ref=mid)
        if principal is None:
            return reason, addr, None, voucher
        return "ok", addr, principal, voucher

    def _authenticate(self, channel: str, sender_ref: str, bound: str, key: str, *,
                      raw_ref: str | None, settle: bool = False
                      ) -> tuple[str | None, object, str]:
        """Hand the platform-verified update to E1 as a signed envelope and
        return ``(principal or None, voucher or None, reason)``. Bounded lifetime:

        * The envelope carries ``bound`` = ``bind_update(text, content_digest)``
          (a SHA-256 commitment to the text and the whole update), not the
          text: the MAC binds this exact update,
          but neither E1's in-memory voucher nor its durable ``inbound_seen``
          row ever holds the body. ``e3-sha256:`` is not an E1 command word,
          so an E3 voucher cannot drive ``/stop`` / ``/ack`` / ``/approve``.
        * The voucher is checked and then retired (``consume``) before this
          returns, on every path: nothing stays in ``ChannelAuth._minted``.
        * ``update_id`` is deterministic per platform message
          (``e3:<key digest>``), so retries never add rows.
        * E1 (225e7df) resumes a ``pending`` row only for the byte-identical
          envelope, and ``sent_at`` is inside the MAC. So E3 durably records
          the ``sent_at`` it used for this message BEFORE presenting it
          (``transport_handoff``), and every retry or re-delivery re-presents
          the identical envelope. Only if E1 answers ``stale_transport`` -
          which it does only when it holds NO row for the update - is a
          fresh ``sent_at`` taken (a first presentation, never a resumption).
        * The stored ``sent_at`` is dropped once E1's row is known settled
          (``finish`` after commit, or E1 refused it as already processed)."""
        signer = self._signers.get(channel)
        if signer is None:
            return None, None, "unvouched_sender"
        hkey = sha256(key.encode())
        update_id = f"e3:{hkey[:32]}"
        sent_at = self._store.handoff_sent_at(hkey, float(self._clock()), self._clock())
        env = signer.envelope(sender_ref, bound, update_id=update_id, sent_at=sent_at,
                              raw_ref=raw_ref)
        auth = self._auth.authenticate(env)
        if getattr(auth, "reason", None) == "stale_transport":
            sent_at = self._store.handoff_refresh(hkey, float(self._clock()))
            env = signer.envelope(sender_ref, bound, update_id=update_id, sent_at=sent_at,
                                  raw_ref=raw_ref)
            auth = self._auth.authenticate(env)
        sender = getattr(auth, "sender", None)
        e1_reason = getattr(auth, "reason", None)
        # E1 f37086f: "replayed" = processed (settled), "pending_conflict" = a
        # different update is pending. Keep the stored sent_at on a conflict so
        # the genuine update can still resume; drop it once E1 has settled.
        if sender is None and e1_reason in _E1_SETTLED:
            self._store.handoff_drop(hkey)    # E1 holds a settled (processed) row
        sender = (sender, hkey) if sender is not None else None
        try:
            principal = self._vouched_principal(auth, channel, bound)  # checks before retire
            reason = e1_reason
            if principal is not None:
                reason = "ok"
            elif reason == "pending_conflict":
                reason = "transport_conflict"
            elif reason == "replayed" and not settle:
                reason = NEEDS_RECONCILIATION
            elif reason not in REASONS or reason == "ok":
                reason = "unvouched_sender"
            return principal, sender, reason
        finally:
            if sender is not None:
                try:
                    self._auth.consume(sender[0])
                except Exception:
                    pass

    @staticmethod
    def _from_addr(msg) -> str:
        try:
            froms = msg.get_all("From") or []
            if len(froms) == 1 and len(froms[0].addresses) == 1:
                return (froms[0].addresses[0].addr_spec or "").lower()
        except Exception:
            pass
        return ""

    def _vouched_principal(self, auth: object, channel: str, bound: str) -> str | None:
        """The principal only if E1 ``ChannelAuth`` itself minted this exact
        voucher for this channel and this exact message (its text must be
        ``bound``, i.e. ``bind_update(text, content_digest)``). A copied or
        ``AuthenticatedSender`` never yields ``principal_direct``."""
        try:
            sender = getattr(auth, "sender", None)
            if sender is None or not self._auth.vouches(sender):
                return None
            if sender.channel != channel or not isinstance(sender.principal, str):
                return None
            if sender.text != bound:
                return None
            return sender.principal or None
        except Exception:
            return None

    def _release(self, voucher: object) -> None:
        """Mark E1's (digest-only) replay row processed once E3 has committed,
        then drop the stored ``sent_at``. If ``finish`` fails the row stays
        pending and the stored ``sent_at`` is kept, so a re-delivery
        re-presents the identical envelope and settles it."""
        if voucher is None:
            return
        sender, hkey = voucher
        try:
            self._auth.finish(sender)
        except Exception:
            return
        try:
            self._store.handoff_drop(hkey)
        except Exception:
            pass

    @staticmethod
    def _email_parts(msg) -> tuple[str, list[RawAttachment]]:
        """Inline ``text/plain`` of the outer message only. Attachment and
        message boundaries are decided BEFORE descending: any ``message/*``
        part (an attached or forwarded email, inline or not), and any part
        with ``Content-Disposition: attachment`` or a filename (including a
        multipart one), is kept whole as one opaque attachment and never
        parsed - so an attached email's body can never become the outer
        sender's text, nor vanish from provenance."""
        texts: list[str] = []
        htmls: list[RawAttachment] = []
        atts: list[RawAttachment] = []

        def opaque(part) -> RawAttachment:
            data = None
            if not part.is_multipart() and part.get_content_maintype() != "message":
                data = part.get_payload(decode=True)
            if data is None:
                data = part.as_bytes()
            return RawAttachment(data, part.get_content_type(), part.get_filename())

        def visit(part, depth: int) -> None:
            if depth > 32:
                raise _Reject("malformed", record=False)
            disp = part.get_content_disposition()
            fname = part.get_filename()
            if (part.get_content_maintype() == "message" or disp == "attachment"
                    or fname is not None):
                atts.append(opaque(part))
                return
            if part.is_multipart():
                for sub in part.iter_parts():
                    visit(sub, depth + 1)
                return
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    texts.append(part.get_content())
                    return
                except Exception:
                    pass  # undecodable -> keep as an opaque attachment
            (htmls if ctype == "text/html" else atts).append(opaque(part))

        visit(msg, 0)
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
        stage_id = None
        if reject is None and d.attachments:
            # Stage (committed) -> put -> commit refs in the decision's
            # transaction. Any failure after staging leaves a marker that
            # gc_attachments() reclaims; a committed event's blobs never are.
            stage_id = f"ingest:{uuid.uuid4()}"
            refs = [attachment_ref(sha256(a.data)) for a in d.attachments]
            try:
                self._store.stage_attachments(stage_id, refs, now)
                for a, ref in zip(d.attachments, refs):
                    if self._attachments.put(a.data) != ref:
                        raise RuntimeError("attachment store is not content-addressed")
            except Exception:
                return IngressResult(False, "ingress_error")
        result = self._decide(d, now, reject=reject, stage_id=stage_id)
        if result.reason not in ("trace_error", "ingress_error", "attachment_reclaimed"):
            self._release(d.voucher)     # committed: E1 row -> processed
        return result

    def _decide(self, d: _Draft, now: int, *, reject: str | None,
                stage_id: str | None = None) -> IngressResult:
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
                if stage_id is not None:
                    # Stage ownership, checked under the same write lock GC
                    # needs: if GC collected our markers (a paused or slow
                    # writer), or a blob is missing, refuse - retryable, and
                    # nothing is recorded - rather than commit a dangling ref.
                    refs = [attachment_ref(sha256(a.data)) for a in d.attachments]
                    if not tx.stage_intact(stage_id, refs) or not all(
                            self._attachments.exists(r) for r in refs):
                        raise _AttachmentLost()
                    tx.unstage(stage_id)      # committed with whatever is decided below
                prior = tx.get(key_digest)
                if prior is not None:
                    if prior.content_digest != d.content_digest:
                        trace(False, "replay_conflict", "none")
                        return IngressResult(False, "replay_conflict", event_id=prior.event_id)
                    # Same content again. If the original was accepted but never
                    # acked as durably handed off (e.g. crash after commit, before
                    # the runner enqueued it), return it again: recoverable, and
                    # the runner's enqueue is idempotent on event_id.
                    recovered = None
                    if prior.decision == "accepted":
                        st = tx.inbox_status(prior.event_id)
                        if st is not None and st[0] == "pending":
                            recovered, why = self._restore_checked(st[1], prior.event_digest)
                            if recovered is None:                      # corrupt: never returned
                                tx.quarantine(prior.event_id, now, why)
                    reason = "duplicate_pending" if recovered is not None else "duplicate"
                    trace(False, reason, "none")
                    return IngressResult(False, reason, event_id=prior.event_id,
                                         event=recovered, duplicate=True)
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
                tx.inbox_put(event_id, canon(event.to_record()).decode(), now)
                tx.attach(event_id, [a.ref for a in event.attachments])
                trace(True, d.trust_reason, trust)
                return IngressResult(True, "accepted", event_id=event_id, event=event,
                                     trust_reason=d.trust_reason)
        except _AttachmentLost:
            return IngressResult(False, "attachment_reclaimed")
        except Exception:
            # Rolled back (trace sink or store failure): nothing recorded, so a
            # re-delivery is processed afresh. Fail closed.
            return IngressResult(False, "trace_error" if trace_failed else "ingress_error")

    def _mint(self, d: _Draft, event_id: str, now: int) -> InboundEvent:
        refs = tuple(AttachmentRef(attachment_ref(sha256(a.data)), sha256(a.data), len(a.data),
                                   a.media_type, a.filename) for a in d.attachments)
        segs = d.segments
        principal = d.principal if any(s.provenance == PRINCIPAL_DIRECT for s in segs) else None
        if principal is None and any(s.provenance == PRINCIPAL_DIRECT for s in segs):
            raise RuntimeError("principal_direct without principal")
        return InboundEvent(event_id=event_id, channel=d.channel,
                            channel_message_id=d.message_id, received_at=now,
                            principal=principal, segments=segs, attachments=refs,
                            flags=injection_flags(segs))
