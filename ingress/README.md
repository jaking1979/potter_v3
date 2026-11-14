# ingress (package E3: untrusted-source boundary; T11/T12, T19 PASS 2 items 2+4)

Adapters that turn inbound Telegram updates, email (AgentMail/Gmail raw RFC 5322 bytes), signed webhooks and fetched web pages into normalized `InboundEvent`s for the runner. Builds on C1 (`policy/`, provenance taint), C5 (`identity/`), C6 (`tracing/`) and E1 (`notifier.auth.ChannelAuth`). Staging-only (T22): every transport, DKIM/SPF verifier and signature check is injected; only offline fakes exist. No connector, network client or secret is here.

| Module | What it does |
| --- | --- |
| `events.py` | Provenance kinds, `Segment`, `AttachmentRef`, `InboundEvent`. An import-time assert proves no untrusted kind is in C1's `TRUSTED_SOURCES` and ingress never emits `system_policy`. |
| `gateway.py` | `IngressGateway.ingest_telegram / ingest_email / ingest_webhook / ingest_web` → `IngressResult`. `vouches(event)` confirms an event was minted here, unchanged. |
| `content.py` | `IngressLimits`, forwarded/quoted splitting, injection-indicator flags. |
| `store.py` | `IngressStore` (SQLite dedupe + ingress log, `meta.ingress_schema=e3.v2`; an e3.v0 store is rebuilt atomically on open, e3.v1 gains the `inbox_blocked` table, any other version or layout is refused before service) and the `AttachmentStore` seam (`InMemoryAttachmentStore` fake). |
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

**Transport envelope (E1 `c8c8422`, `f14e564`).** Ingress is the trusted channel adapter. Once the platform's own proof passes, it signs the update with that channel's injected `TransportSigner` and hands the envelope to `ChannelAuth.authenticate`, which checks the MAC, freshness and replay. With no signer for a channel, or a mismatched one, nothing on that channel is authenticated. Ingress never builds an `InboundMessage` itself (`test_73`).

The handoff has a bounded lifetime and holds no text:
- **A digest of the whole update, not the body.** The envelope's text is `bind_update(text, content_digest)` = `e3-sha256:<sha256 of the text plus the update's full content digest>`. The content digest covers every trust- and event-relevant field:
  - **Telegram:** chat, chat type, sender, the bot, forwarded and via-bot flags, text, and each attachment's bytes, media type and filename.
  - **Email:** the raw message bytes, so all headers and attachments.

  The MAC therefore binds the exact update, but E1's in-memory voucher and its durable `inbound_seen` row only ever hold the digest. `e3-sha256:` is not an E1 command word, so an E3 voucher can't drive `/stop`, `/ack` or `/approve` (`test_83`).
- **Conflicting retries refused.** E1's pending recovery can only resume an *identical* update. A different update reusing a rolled-back update's ID, sender and text (different flags, chat type, attachments or headers) is answered by E1 (`f37086f`) as `pending_conflict`. E3 refuses it as `transport_conflict`: not accepted and not recorded, and the stored `sent_at` is kept. So it can't pre-empt the genuine update, which still resumes (`test_109`–`test_113`, `test_116`). A conflict with an update E3 has already *committed* is E3's own `replay_conflict`.
- **Processed in E1 but never committed by E3.** On a first handoff, E1 can answer `replayed`, meaning it holds a *processed* record that E3 never committed. Typical causes: E3's store was lost or restored, or the row came from another path. This is a durable, explicit safe failure, `needs_reconciliation`:
  - **Recorded:** later re-deliveries are plain duplicates, so there is no retry loop.
  - **Never accepted:** no event and no effect.
  - **Listed for an operator** by `gateway.needs_reconciliation()` (`test_114`, `test_115`).
- **Limit:** E3 relies on the adapter or provider delivering each message ID immutably. This interface can detect a changed update, but not prevent a provider from issuing one.
- **Vouchers are retired immediately.** Each voucher is checked, then retired with `consume()` on every path (accepted, untrusted, rejected or exception), so nothing stays in `ChannelAuth`'s minted table (`test_79`, `test_82`).
- **One replay row per platform message.** `update_id` is `e3:<key digest>`, so retries add no rows (`test_80`, `test_89`). Once E3's decision commits, `finish()` marks it `processed`.
- **Identical envelope on every retry.** E1 (`225e7df`) resumes a `pending` row only for the byte-identical envelope, and `sent_at` is inside the MAC. So E3 records the `sent_at` it signs, in `transport_handoff` (keyed by hash; no body and no sender reference), *before* presenting the envelope. Every retry or re-delivery re-presents exactly that envelope.
  - **Fresh `sent_at`:** taken only when E1 answers `stale_transport`, which it does only when it holds **no** row (`test_98`).
  - **Dropped:** once E1's row is settled.
- **Settled exactly once on re-delivery.** If the process dies after E3 commits but before `finish()`, the row stays `pending`. A re-delivery answered by E3's dedupe re-presents the identical envelope; E1 resumes it and E3 finishes it. A processed row answers `replayed` (`test_85`, `test_90`). The same holds for a rolled-back attempt retried long after the freshness window (`test_97`).
- **Retention is E1's.** E1 (`2e01315`) prunes its own `processed` rows by its durable `sent_at` watermark (never wall time), using its own configured skew, and never prunes `pending` ones. E3 never deletes from E1's table, so no E3 setting can reopen replay. The earlier skew-mismatch path is gone (`test_88`).
  - A pending row survives E1's prune and still settles (`test_86`).
  - A captured envelope replayed after its row is pruned is `stale_transport`. Nothing is recorded and it never reaches recovery (`test_87`).
  - Moving the clock backwards produces no second event (`test_99`).
- **E1 refusals that say nothing about the sender** are not accepted and not recorded, and so stay retryable (`E1_RETRYABLE`):
  - `clock_regression`: E1's clock went backwards; the transport is paused until the clock catches up or an operator reconciles it.
  - `needs_operator`: a migrated record is awaiting an operator.
  - `stale_transport`: still stale after one re-sign, meaning E3's and E1's clocks disagree.

  None of these ever becomes an accepted event (not even an untrusted one), nor a recorded rejection that would turn the sender's retry into a duplicate. E3 keeps the stored `sent_at`, and drops it only after E1 reports a settled outcome (`test_104`–`test_108`).

**Vouchers.** For both Telegram and email, a principal counts only if `ChannelAuth.vouches(sender)` confirms that this `ChannelAuth` minted that exact `AuthenticatedSender` for this channel **and this exact message text**. A hand-built, copied, relabelled or other-text voucher yields no principal: the Telegram message is rejected, and the email is `external_sender` with `unvouched_sender`.

## Content handling

- Provenance is computed from authentication results only. No text is parsed for provenance, approval or policy fields. `InboundEvent` has no approval field, and ingress exposes no policy API.
- Injection indicators (`claims_approval`, `addresses_policy`, `instruction_like`, `payment_request`) are computed over untrusted segments and set as `flags`. They are informational and can never raise trust.
- MIME boundaries are decided before descending: any `message/*` part (an attached or forwarded email, inline or not), and any part with `Content-Disposition: attachment` or a filename (multipart included), is kept whole as one opaque attachment and never parsed. An attached email's body can therefore never become the outer sender's text (`test_74`–`test_78`).
- Attachments go to the injected `AttachmentStore`. That store is content-addressed (`put(data)` must return `att://sha256/<hex>`, or ingest fails closed) and `delete(ref)` is idempotent. It is not transactional with E3's database, so E3 owns the lifecycle:
  1. A `staged` marker is committed **before** any `put`.
  2. The event's `attachment_refs` are written in the same transaction as the event, and that transaction clears the marker.
  3. `gc_attachments(grace_s)`, phase 1: deletes blobs whose every marker is older than the grace period and that no committed event references. It deletes markers and blobs together under `BEGIN IMMEDIATE`.
  4. **Stage ownership:** in the same transaction as the event commit, the ingest checks that all its own markers are still present and every blob exists. If not, because GC collected a paused or slow writer, the event is refused with `attachment_reclaimed` (retryable, nothing recorded) rather than committing a dangling reference (`test_100`).
  5. `gc_attachments`, phase 2 (orphan sweep): blobs in the store with no reference and no marker get an `orphan:` marker, so phase 1 reclaims them after the grace period. This catches a writer's late `put` that landed after its stage was collected, and a crash at any step (`test_101`, `test_102`). Committed and in-flight blobs are never touched (`test_103`).

  A trace or database failure after `put`, or a crash at any step, leaves a reclaimable marker. A blob shared with a committed event is never deleted. `expire_pending` releases an event's references; `ack` keeps them, since keeping them is runner (C8) retention policy (`test_91`–`test_96`). The event carries `AttachmentRef(ref, sha256, size, media_type, filename)`; filename and media type are sender-supplied.
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
- **Integrity and quarantine.** A restored event must match the digest minted at acceptance. A row that can't be restored (`malformed`, or `digest_mismatch`) is never returned, accepted, acked or retried. In one transaction it is **quarantined**:
  - durably moved out of `pending`, with its untrustworthy text dropped;
  - traced as `corrupt_pending`;
  - listed by `gateway.quarantined()` as (event_id, reason), with no content.

  `pending(limit, max_scan)` scans with a rowid cursor and examines at most `max_scan` rows per call (default `MAX_SCAN` = 1000). Every call therefore makes forward progress: corrupt rows can't starve later valid ones, across calls and after a restart, and quarantined rows are never re-scanned. The duplicate re-delivery path quarantines in the same way. `expire_pending` is unchanged for valid rows (`test_117`–`test_124`).
- **Quarantine failure.** If the quarantine write itself fails, `pending()` does not skip it silently:
  - the row is recorded as **recovery blocked** (`inbox_blocked`: event_id, reason, attempts, timestamps; no content), traced as `quarantine_failed`, and listed by `gateway.recovery_blocked()`;
  - blocked rows are excluded from later scans, so the next call (also after a restart) reaches later valid rows; if even the block record cannot be written, the row is held in process memory instead, with the same effect;
  - a blocked row is never returned or acked, and keeps its attachment refs until it is actually quarantined (then released) or expired;
  - each `pending()` call first retries quarantine for at most `RETRY_BLOCKED` = 16 blocked rows (least recently tried first); success clears the block in the same transaction (`test_131`–`test_138`).
  - every row read counts toward `max_scan`, including rows held in memory. A call that stops at the cap leaves a process-local resume point; the next call continues after it and wraps to the oldest row once, and returns its events oldest-first. A call that fills `limit` or reaches the end resets the resume point. After a restart the memory holds and resume point are gone: those rows are re-examined, fail again and are re-held within each call's cap, and are never treated as resolved (`test_139`–`test_142`).
  - retry selection is bounded too: it reads only the `RETRY_BLOCKED` prefix of the memory holds (never the whole backlog), keeps their rotation, and splits the budget evenly with durable holds when both exist (`test_143`–`test_146`). `recovery_blocked()` is the operator listing and returns everything.
- **Retention.** `ack` drops the stored text. `expire_pending(max_age_s)` drops the text of events left pending too long. Those events are lost on purpose and traced as `expired`. Quarantine is terminal and keeps only (event_id, reason, timestamps): its text is dropped when it is quarantined, and its attachment refs are released then, as on expiry, so `gc_attachments` can reclaim blobs no live event shares (`test_129`). The id/reason row stays as the operator's audit record, like `handed_off`/`expired` rows.
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
