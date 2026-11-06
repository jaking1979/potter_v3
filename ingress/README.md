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
| `forwarded_content` | Forwarded material, even inside Josh's authenticated message (`Fwd:` subject, forward markers, Telegram forwards). | tainted |
| `quoted_content` | `> …` lines and `On … wrote:` headers. | tainted |
| `attachment` | Every attachment, plus HTML-only bodies. Stored by reference; never read. | tainted |
| `webhook_payload` | Body of a signature-verified webhook. | tainted |
| `web_content` | Pages the runner fetched. | tainted |

A mixed event (Josh's command plus a forward or an attachment) keeps `principal=human:josh` for attribution, but `trusted` is false, and C1 requires an approval for any write it shapes. `principal_direct` is authentication, not approval: a consequential capability still needs an E1 approval (`approval_required`).

## Sender authentication

- **Telegram.** The injected transport verifier must confirm the update came from Telegram (production: webhook secret token). Without one, every update is rejected (`transport_unverified`, not recorded). The `from` user ID must pass `ChannelAuth`, otherwise the update is rejected (`sender_not_allowlisted`; E1 ignores strangers too). Bots are rejected. `principal_direct` additionally needs the private chat with that user (`chat_id == from_user_id`) and no `via_bot`. Otherwise the text is `external_sender`.
- **Email.** The injected `EmailVerifier(raw) → EmailAuthVerdict(dkim, dkim_domain, spf)` judges the exact bytes. `principal_direct` requires all of:
  1. exactly one `From` header with one address and no header defects;
  2. no `@` in the display name (`"josh@…" <mallory@…>`);
  3. `Reply-To` and `Sender`, when present, equal to `From`;
  4. DKIM `pass` with `d=` equal to the `From` domain (alignment);
  5. SPF not `fail`/`softfail`;
  6. `ChannelAuth` for the address (which itself requires its own DKIM verifier to pass; `raw_ref` is the Message-ID).

  Any failure, including a missing verifier or one that raises, makes the text `external_sender` with a fixed `trust_reason` (`dkim_failed`, `display_name_spoof`, `reply_to_mismatch`, …). Email from other people is accepted as untrusted (inbox triage), not rejected.
- **Webhooks.** Each source needs a signature verifier. Unknown sources and unsigned deliveries are rejected and not recorded.

## Content handling

- Provenance is computed from authentication results only. No text is parsed for provenance, approval or policy fields. `InboundEvent` has no approval field, and ingress exposes no policy API.
- Injection indicators (`claims_approval`, `addresses_policy`, `instruction_like`, `payment_request`) are computed over untrusted segments and set as `flags`. They are informational and can never raise trust.
- Attachments go to the injected `AttachmentStore`. The event carries `AttachmentRef(ref, sha256, size, media_type, filename)`; filename and media type are sender-supplied.
- Limits (`IngressLimits`): raw size, total text, attachment count and per-attachment size. Exceeding any limit rejects the message.
- `repr` of events, segments and raw inputs excludes text. The store keeps no text, sender reference or header.

## Dedupe and re-delivery

The dedupe key is the channel message ID: Telegram `chat_id:message_id`, the email `Message-ID`, the webhook `source:delivery_id`, or the web fetch ID. For email the key is scoped by the authentication namespace (`human:josh` vs `untrusted:<sha256(from)>`), so an unauthenticated sender cannot squat Josh's Message-ID. The event ID is UUIDv5 of the key.

- Same key, same content: `duplicate`. No new event; the original `event_id` is returned. This survives a restart.
- Same key, different content: `replay_conflict`, rejected.
- Rejections after transport authentication are recorded, so they are idempotent too. Pre-authentication rejections (forged transport, unsigned webhook, oversized raw, missing Message-ID) are traced but not recorded, so they can't fill the store.

## Tracing

Each decision runs in one `BEGIN IMMEDIATE` transaction: look up, insert, emit the C6 trace, commit. The trace uses component `task` with outcome `accepted` or `blocked`. Its digest is over channel, decision, reason, trust class and content digest. If the sink raises, the transaction rolls back and the result is `trace_error`, so a re-delivery is processed afresh.

## Runner contract (C8)

1. Act only on `IngressResult.accepted` events for which `gateway.vouches(event)` is true.
2. Copy `event.provenance` into every `PolicyRequest` the event shaped. Never drop kinds.
3. Never treat event text as an approval. Approvals come only from E1.

## Not in this package

- Real Telegram, Gmail or AgentMail connectors, and DKIM/SPF/DMARC resolution. These are injected.
- Durable attachment storage and retention.
- Runner wiring (C8).
- Email approvals remain refused by E1 (C0 has no `email` channel).

Evidence: `tests/test_ingress.py`.
