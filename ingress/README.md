# ingress (package E3: untrusted-source boundary; T11/T12, T19 PASS 2 items 2+4)

Adapters that turn inbound Telegram updates, email (AgentMail/Gmail raw RFC 5322 bytes), signed webhooks and fetched web pages into normalized `InboundEvent`s for the runner. Builds on C1 (`policy/`, provenance taint), C5 (`identity/`), C6 (`tracing/`) and E1 (`notifier.auth.ChannelAuth`). Staging-only (T22): every transport, DKIM/SPF verifier and signature check is injected; only offline fakes exist. No connector, network client or secret is here.

| Module | What it does |
| --- | --- |
| `events.py` | Provenance kinds, `Segment`, `AttachmentRef`, `InboundEvent`. An import-time assert proves no untrusted kind is in C1's `TRUSTED_SOURCES` and ingress never emits `system_policy`. |
| `gateway.py` | `IngressGateway.ingest_telegram / ingest_email / ingest_webhook / ingest_web` → `IngressResult`. `vouches(event)` confirms an event was minted here, unchanged. |
| `content.py` | `IngressLimits`, forwarded/quoted splitting, injection-indicator flags. |
| `store.py` | `IngressStore` (SQLite dedupe + ingress log, `meta.ingress_schema=e3.v0`) and the `AttachmentStore` seam (`InMemoryAttachmentStore` fake). |
| `traces.py` | One C6 `DecisionTrace` per ingress decision. |

## Provenance (the boundary)

`InboundEvent.provenance` is the sorted set of kinds of every segment and attachment. Pass it unchanged as `PolicyRequest.provenance` for any request the event shaped.

| Kind | When | C1 |
| --- | --- | --- |
| `principal_direct` | Command text from a sender that passes the channel's own authentication **and** E1 `ChannelAuth` (bound, allowlisted, enabled → `human:josh` only). | trusted |
| `external_sender` | Text from anyone else, or from a Josh address that failed any check below. | tainted |
| `forwarded_content` | Forwarded material, even inside Josh's authenticated message: `Fwd:`-style subjects, forward markers, pasted `From:`/`Sent:` header blocks, Telegram forwards. Runs from the boundary to the end of the body. | tainted |
| `quoted_content` | Everything from the first `>` line, rule line, or reply header (`On … wrote:`, including wrapped and non-English forms) to the end of the body. | tainted |
| `attachment` | Every attachment, plus HTML-only bodies. Stored by reference; never read. | tainted |
| `webhook_payload` | Body of a signature-verified webhook. | tainted |
| `web_content` | Pages the runner fetched. | tainted |

A mixed event (Josh's command plus a forward or an attachment) keeps `principal=human:josh` for attribution, but `trusted` is false, and C1 requires an approval for any write it shapes. `principal_direct` is authentication, not approval: a consequential capability still needs an E1 approval (`approval_required`).

## Sender authentication

- **Telegram.** The injected transport verifier must confirm the update came from Telegram (production: webhook secret token). Without one, every update is rejected (`transport_unverified`, not recorded). The `from` user ID must pass `ChannelAuth`, otherwise the update is rejected (`sender_not_allowlisted`; E1 ignores strangers too). Bots are rejected. `principal_direct` additionally needs the private chat with that user (`chat_id == from_user_id`) and no `via_bot`. Otherwise the text is `external_sender`.
- **Email.** The injected `EmailVerifier(raw) → EmailAuthVerdict(dkim, dkim_domain, spf, mailbox_bound)` judges the exact bytes. `principal_direct` requires all of:
  1. exactly one `From` header with one address and no header defects;
  2. no `@` in the display name (`"josh@…" <mallory@…>`);
  3. `Reply-To` and `Sender`, when present, equal to `From`;
  4. DKIM `pass` with `d=` equal to the `From` domain (alignment);
  5. SPF not `fail`/`softfail`;
  6. the From domain is in `email_attested_domains`. This set is **empty by default**, so email is never `principal_direct` unless configured. Adding a domain is a Design Council decision that its signing provider binds DKIM signatures to the authenticated mailbox. An aligned DKIM pass proves the signing domain and message integrity, not that the mailbox owner sent the message (`email_domain_not_attested`);
  7. `mailbox_bound is True` in the verdict: the verifier attests, for this exact message, that the provider authenticated the submitter *as* the From mailbox (`mailbox_unbound`);
  8. `ChannelAuth` for the address (which itself requires its own DKIM verifier to pass; `raw_ref` is the Message-ID).

  Any failure, including a missing verifier or one that raises, makes the text `external_sender` with a fixed `trust_reason` (`dkim_failed`, `display_name_spoof`, `reply_to_mismatch`, …). Email from other people is accepted as untrusted (inbox triage), not rejected.
- **Webhooks.** Each source needs a signature verifier. Unknown sources and unsigned deliveries are rejected and not recorded.

**Transport envelope (E1 `c8c8422`).** Ingress is the trusted channel adapter. Once the platform's own proof passes (the Telegram transport verifier, or the email header and DKIM/attestation checks), it signs the exact update with that channel's injected `TransportSigner` and hands the envelope to `ChannelAuth.authenticate`. The signature covers channel, sender, the full message text (for email, subject plus plain body), `raw_ref`, `update_id` and time. `ChannelAuth` checks the MAC, freshness (`max_skew_s`) and envelope replay.

- With no signer for a channel, or a signer whose channel doesn't match, nothing on that channel is authenticated.
- Ingress never builds an `InboundMessage` itself; `test_73` checks the source for this.
- `update_id` is `e3:<key digest>:<nonce>`, unique per attempt. Ingress handles platform re-delivery itself: its dedupe is content-bound and atomic with the durable inbox. A deterministic ID would turn a rolled-back attempt into a permanent `replayed` rejection of the sender's retry (`test_72`).

**Vouchers.** For both Telegram and email, a principal counts only if `ChannelAuth.vouches(sender)` confirms that this `ChannelAuth` minted that exact `AuthenticatedSender` for this channel **and this exact message text**. A hand-built, copied, relabelled or other-text voucher yields no principal: the Telegram message is rejected, and the email is `external_sender` with `unvouched_sender`.

## Content handling

- Provenance is computed from authentication results only. No text is parsed for provenance, approval or policy fields. `InboundEvent` has no approval field, and ingress exposes no policy API.
- Injection indicators (`claims_approval`, `addresses_policy`, `instruction_like`, `payment_request`) are computed over untrusted segments and set as `flags`. They are informational and can never raise trust.
- Attachments go to the injected `AttachmentStore`. The event carries `AttachmentRef(ref, sha256, size, media_type, filename)`; filename and media type are sender-supplied.
- Limits (`IngressLimits`): raw size, total text, attachment count and per-attachment size. Exceeding any limit rejects the message.
- `repr` of events, segments and raw inputs excludes text. The dedupe log keeps no text, sender reference or header. The durable inbox (below) holds an accepted event's text locally only until it is acked or expires.

## Quote boundaries and residual limitations

Boundary detection (`content.first_boundary`) is a heuristic. It cannot establish who wrote a piece of text, so it is used only to **fail closed**:

- Only the text *before* the first boundary keeps the sender's kind. Everything from that line to the end of the body is one untrusted run. That includes unprefixed continuation lines, wrapped quotes and Josh's own interleaved replies.
- Boundaries recognized: forward markers (several languages), pasted header blocks, `>` lines, rule lines (`_____`, `-----`), and any line or 2–4-line wrapped run that ends in a quote verb plus `:` (`wrote`, `a écrit`, `schrieb`, `escribió`, …).
- Anything derived from an event (a summary, a plan, a request) must carry the event's full `provenance`, never only its direct segments. `event.tainted` is true whenever any part is untrusted.

**Residual limitation (not detected):** text pasted with *no* marker at all, such as third-party words typed or pasted into Josh's own authenticated message or Telegram chat, cannot be told apart from Josh's words. `principal_direct` attests that Josh's authenticated account sent the text, not that he authored every word. The backstops:

- Consequential capabilities always need an E1 approval, whatever the provenance (`test_53` pins this).
- Email trust is off by default (above).

We do not claim that all quoted or forwarded text is detected.

## Dedupe and re-delivery

The dedupe key is the channel message ID: Telegram `chat_id:message_id`, the email `Message-ID`, the webhook `source:delivery_id`, or the web fetch ID. For email the key is scoped by the authentication namespace (`human:josh` vs `untrusted:<sha256(from)>`), so an unauthenticated sender cannot squat Josh's Message-ID. The event ID is UUIDv5 of the key.

- Same key, same content, original already handed off: `duplicate`. No event is returned, only the original `event_id`. This survives a restart.
- Same key, same content, original accepted but not yet acked: `duplicate_pending`. The stored event is returned again (see durable handoff).
- Same key, different content: `replay_conflict`, rejected.
- Rejections after transport authentication are recorded, so they are idempotent too. Pre-authentication rejections (forged transport, unsigned webhook, oversized raw, missing Message-ID) are traced but not recorded, so they can't fill the store.

## Tracing

Each decision runs in one `BEGIN IMMEDIATE` transaction: look up, insert, emit the C6 trace, commit. The trace uses component `task` with outcome `accepted` or `blocked`. Its digest is over channel, decision, reason, trust class and content digest. If the sink raises, the transaction rolls back and the result is `trace_error`, so a re-delivery is processed afresh.

## Durable handoff

An accepted event is written to `ingress_inbox` in the **same transaction** as its dedupe row and its trace, so a commit never leaves a dedupe row without a recoverable event.

- **Recovery.** The event stays `pending` until the runner calls `gateway.ack(event_id)` after a durable enqueue. If the process crashes before that, the event comes back either from `gateway.pending()` (call it at start-up) or from the sender's re-delivery (`duplicate_pending`).
- **Integrity.** A restored event must match the digest minted at acceptance. A tampered row is never returned.
- **Retention.** `ack` drops the stored text. `expire_pending(max_age_s)` drops the text of events left pending too long. Those events are lost on purpose and traced as `expired`.
- **Idempotency.** `vouches()` is an integrity check, not consumption: it stays true on repeated calls. Single processing comes from the runner's queue, keyed on `event_id`; C2 `TaskStore.submit(f"ingress:{event_id}", …)` is idempotent.

## Runner contract (C8)

1. On start-up and for every result that carries an `event` (`accepted` or `duplicate_pending`), check `gateway.vouches(event)`, enqueue idempotently on `event_id`, then call `gateway.ack(event_id)`.
2. Copy `event.provenance` into every `PolicyRequest` the event shaped. Never drop kinds.
3. Never treat event text as an approval. Approvals come only from E1.

## Not in this package

- Real Telegram, Gmail or AgentMail connectors, and DKIM/SPF/DMARC resolution. These are injected.
- Durable attachment storage and retention.
- Runner wiring (C8).
- Email approvals remain refused by E1 (C0 has no `email` channel).

Evidence: `tests/test_ingress.py`.
