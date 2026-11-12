# notifier (package E1: T19 PASS 2 approvals + notifier; T11/T12)

E1 is the only notifier and the only approval issuer. It builds on C0 (`contracts/v0`), C1 (`policy/`), C2/C3 (`runtime/`), C5 (`identity/`) and C6 (`tracing/`). It is staging-only (T22): channels are injected, the only implementations are offline fakes, and nothing here enables real delivery.

| Module | What it does |
| --- | --- |
| `service.py` | `Notifier`: `enqueue(tx, …)` writes to the C3 outbox in the caller's transaction. `run()` relays one claim at a time, re-checking the kill flag and primary health between messages. Also: outage fallback, `ack`, `stop`, `watchdog`, `JobSpec`/`JobHandle`, `LongWork`. |
| `issuer.py` | `ApprovalIssuer`: `request(tx, BoundAction, …)` stores the action ID with the exact canonical action and a 24 h deadline, then enqueues the prompt. `decide(…)` signs a C0 `ApprovalRecord` with the injected issuer key, or refuses. |
| `auth.py` | `ChannelAuth`: transport authentication, then principal checks. An inbound message counts only if it is an envelope from the trusted channel adapter (`TransportSigner`). The envelope carries an HMAC over channel, sender, text, reply target, update ID and time, made with that channel's transport key. It must also be fresh (±300 s) and not seen before (the durable `inbound_seen` table). After that, `ChannelAuth` maps senders to principals, enforces the allowlist, and requires email DKIM or a Potter Home session through injected verifiers. Vouchers are bound to the verified message: its text, reply target and update ID. They carry an unguessable token, `vouches()` accepts only the exact minted object unmodified, and `consume()` retires one after a single approval decision. |
| `router.py` | `CommandRouter`: handles `/stop`, `/ack`, `/approve`, `/deny`. Senders who aren't authenticated or allowlisted are ignored and logged to `inbound_log`; the log never stores message text. |
| `kill.py` | `KillFlag`: a durable global flag in the shared SQLite file. Only an explicit `clear()` resets it. |
| `channels.py` | The `Channel` interface (`send`, `reconcile`, `probe`), `InboundMessage` and `FakeChannel`. |
| `lint.py` | Structural check: no direct channel use and no reach-through into notifier internals outside `notifier/`. Run it with `python3 -m notifier.lint .`. |
| `traces.py` | Emits C6 `DecisionTrace` records for issue, decide and send. The records are redaction-safe. |
| `_schema.py` | E1's tables, pinned by `meta.notifier_schema=e1.v1`. `ensure()` migrates an `e1.v0` database inside one `BEGIN IMMEDIATE` transaction (see Schema migration below). It refuses an unknown or future version, or an `e1.v0` layout it doesn't recognise, before routing any traffic. |

## T19 PASS 2 mapping

1. **One notifier.** Every job declares `JobSpec(name, delivery="josh"|"silent")` and gets a `JobHandle`, which can only enqueue.
   - The handle, and any `LongWork` it returns, holds only an opaque handle ID, the job name and the silent flag. The notifier, outbox and channels are resolved from a private registry on each call, so no attribute chain on the handle's data leads to a channel. A test walks the handle's references to prove this, and Dolores's `handle._n._channels['telegram'].send(msg)` now raises `AttributeError`.
   - `lint.py` fails, outside `notifier/` and `runtime/`, on: imports of a channel, the outbox, the notifier service or a network client; `.relay()` and `.send()` calls; reach-through attributes (`_n`, `_hid`, `_channels`, `__dict__`, `__closure__`, `__globals__`, `cell_contents`, `get_referents` and similar, except on `self`/`cls`); `vars()`; `getattr`/`setattr`/`delattr` with a dynamic or `_`-prefixed name; and imports of `gc`, `inspect` or `ctypes`. Network clients are refused everywhere.
   - **Honest boundary:** in-process Python cannot be a hard isolation boundary. Code in the same interpreter can always reach the private registry or the channels through some route a lint doesn't anticipate. The handle design and the lint are defence in depth. Hard enforcement is the runtime/process boundary in C8: jobs run where the notifier's channels and credentials don't exist. So the T19 release test "a job sending directly fails" holds for jobs written against the handle API and checked by the lint; it is not a claim about hostile code inside the notifier's process.
2. **Identity.** The allowlist is `human:josh`. `human:christie` is listed but disabled in the C5 seed, so she is ignored. Everyone else is ignored and logged.
   - A hand-built `InboundMessage` (for example `('telegram', josh_id, '/stop')`) has no valid adapter MAC and is ignored as `unauthenticated_transport`. So is a genuine update whose text, reply target, sender, channel or update ID was changed, and so is one signed with another key. A replayed update is ignored as `replayed`, and an old one as `stale_transport`.
   - **No lost commands after a crash or a failure.** An authentic update is recorded in `inbound_seen` as `pending`. The record holds a sha256 digest of the exact signed envelope, never the message body. The command's effect marks it `processed` in the same transaction: the `/stop` transaction, or the approval-decision transaction.
     - **Known outcomes only.** The router marks a refusal, an unknown command or the idempotent ack `processed` only after that outcome completes. If anything raises first (a signing key, the trace sink, the database), the transaction rolls back and the update stays `pending`. A failure *after* the effect commits leaves it `processed`, so the effect is never reapplied.
     - **Recovery path.** Redelivery of a `pending` update resumes it, even after an outage longer than the 300 s freshness window. The redelivered envelope must match the stored digest byte for byte, and authorization is rechecked: binding, allowlist, enabled status, DKIM or session. A different envelope under the same update ID is refused as `replayed`. An update never seen before must still be fresh (`stale_transport` otherwise), and nothing is recorded for it.
     - Redelivery of a `processed` update is ignored as `replayed`. If two deliveries race, one wins the compare-and-set and the other's effect rolls back.
   - **Clock rollback fails closed.** Each transport keeps a durable high-water mark in the shared DB (`notifier_state['hw:<channel>']`). It holds the highest accepted signed `sent_at` and the highest clock reading seen; both only move forward.
     - If the clock reads more than 5 s behind the highest clock of *any* transport, every command is refused as `clock_regression`. Refusal continues until an operator calls `ChannelAuth.reconcile_clock()`, which lowers only the clock mark, never the `sent_at` mark.
     - A never-seen envelope must be fresh against the clock *and* have `sent_at ≥ max_sent_at − 300 s`. So an envelope whose row was pruned can't be accepted again, whatever the wall clock says.
     - Pruning removes processed rows with `sent_at < max_sent_at − (2 × 300 s + 60 s)`, never by wall time.
     - E3 reads `max_skew_s`, `prune_horizon_s`, `clock_tolerance_s` and `high_water(channel)` as read-only values.
     - Tests:
       - Dolores's jump-forward-then-roll-back;
       - a restart from the DB between the jump and the rollback;
       - a rollback seen on another transport;
       - operator reconcile;
       - restoring a DB copy taken after acceptance (`replayed`) and after the jump (`clock_regression`);
       - a small step back within tolerance;
       - unchanged forward operation.
     - **Limit:** restoring a DB copy taken *before* an envelope was first accepted is indistinguishable from first delivery, while the clock is still within 300 s of that envelope. Detecting it needs an external monotonic anchor, such as a signed counter held outside the DB.
   - **Refusal reasons (a contract E3 depends on).** For an update ID that is already journaled:
     - `replayed` means the update was processed: it is done and will never run again, so stop redelivering it.
     - `pending_conflict` means a *different* envelope arrived under an update ID that is still pending. It is not the journaled command and is never executed; the original envelope can still resume.
     - `needs_operator` means a migrated pending command, which is never resumed.

     The input-validation refusals cover non-finite values. Each fails closed with no writes, and none raises:
     - `invalid_sent_at`: a NaN, ±inf, bool or non-numeric signed `sent_at`, rejected before the MAC check, the freshness arithmetic or any transaction, for new and pending envelopes alike.
     - `clock_invalid`: a clock reading that is NaN, ±inf, bool or non-numeric, or a clock that raises. The reading is checked before authentication, any watermark update, marking a command processed (an invalid reading rolls the command's effect back, so it stays pending), operator reconcile or repair, and the migration seed.
     - `watermark_corrupt`: a stored high-water value that is not finite. This persists across instances until an operator runs `ChannelAuth.repair_watermark()`, which replaces only the non-finite fields with the current finite clock and never lowers a finite one. `reconcile_clock()` is not a repair path.
   - **`max_skew_s` bounds.** Construction rejects any value that isn't a finite number in (0, 3600] s; bool, NaN and ±inf are rejected. Watermarks are written with `allow_nan=False`.
   - **Retention.** Vouchers, which carry the verified text for command binding, are released after dispatch and capped at 1024 in memory. Journal rows keep only the digest. Processed rows are pruned after `2 × 300 s + 60 s`, once any replay of them would be rejected as stale anyway. Pending rows are never pruned.
   - A voucher can drive only the command it was minted for. `stop()` needs `/stop`, `ack(nid)` needs `/ack nid`, and `decide(id, approve)` needs `/approve id`. A voucher for `/ack` therefore cannot stop or approve.
   - A boundary test wires a fake Telegram webhook adapter to the router. The adapter checks the webhook secret and signs the envelope. A forged webhook never reaches the router.
   - Transport keys are test constants; real adapter key custody is T11/T12.
3. **Delivery IDs, unknown outcomes and acks.**
   - The outbox row (`<notification_id>:<channel>`) is committed before any send.
   - Unknown outcomes use C3 unchanged: a routine message is not resent, and an urgent one is resent once with the same key.
   - Every channel copy of a notification shares its `notification_id`, so an ack on any channel suppresses the others.
4. **Approvals.**
   - Each approval has a single-use action ID; the status flips in one `BEGIN IMMEDIATE` update.
   - An approval is refused 24 h after the request. The signed `expires_at` is that deadline, so the C1 window never extends past it.
   - Issuance and every decision require an authoritative C2 task with `id == action.task_id` that is in `waiting_approval`. If the task is missing, the request or decision is refused as `task_not_waiting`; a pending request is voided.
   - An approval is voided when the live action's hash differs (a parameter or precondition changed), when the C2 task has left `waiting_approval`, or on `state_changed(task_id)`.
   - A reply counts only if its channel authenticates Josh. See the note on email below.
5. **Long work.** One acknowledgement once work runs past 30 s, an update only when the state changes, and a final message with the receipt link. Progress is read back from the outbox on every tick, so update numbering and the last-told state survive a restart.
6. **`/stop`.** One transaction sets the kill flag, marks every `pending`/`unknown` outbox row `suppressed`, cancels pending approvals, and enqueues a reply that lists what was cancelled.
   - **T19 item 6 ("an in-flight call completes and nothing follows") is UNMET for channels that can't be fenced at the provider, which is Telegram and email.** This is an open gap against the ratified decision, not an amendment to it. A send that has passed its final check and then stalls can still land after `stop()` has returned, and no provider-side fence exists to stop it. What E1 does provide, and tests, is below.
   - **Durable in-flight record.** Each record lives in `notifier_sending`, keyed by (message, dispatch number).
     - It is written as `gated`, in the same `BEGIN IMMEDIATE` transaction as the gate's kill check, before the final check.
     - The final check is a second transaction serialized with the committed kill flag. It confirms the record is still `gated`, is younger than the dispatch timeout, and that the kill flag is clear (only `stop_ack` is exempt). It then flips the record to `dispatching` before calling the channel.
     - After the call, a success or a definite failure removes the record. Any other outcome keeps it as `unknown`: a timeout, an ambiguous error, or a crash after dispatch. A process killed mid-dispatch leaves it as it was.
     - A record is removed only when the outbox has resolved its message (delivered, acked, or proven undelivered) and it is not a live dispatch. Age, an expired lease or a dead process never removes one.
     - An urgent resend gets its own record, so a possible duplicate is visible.
   - **What `stop()` reports.** `stop()` waits, up to `stop_wait_s` (default 30 s), for live unexpired dispatches. It then reports *every* record still present, in three fields:
     - `unconfirmed_in_flight`: all of them;
     - `expired_in_flight`: those past the dispatch timeout;
     - `unknown_outcome`: those whose provider outcome is unknown.

     If it can't read the in-flight state, it reports `state_unknown`. `all_clear` is true only with no records and readable state.
   - **What holds:**
     - no send starts its final check after `stop()` commits;
     - a sender past its final check may still deliver once, and is reported unconfirmed (expired if aged);
     - a sender that reaches its final check after the flag is set, or after its gate aged out, aborts.

     C3 records any completed delivery, but the row stays suppressed, so no retry follows it.
   - **Tests** (deterministic pauses):
     - before the gate, past the gate, and inside the channel;
     - Dolores's paused-past-gate case with `/stop` at T+21, and a sender paused after its final check (it is reported unconfirmed and delivers once);
     - the bounded wait expiring;
     - a crash before dispatch (no record, no send) and a crash after dispatch (reported `unknown` across a restart and 30 days of age);
     - a dead-process `dispatching` record that never ages out;
     - a provider timeout (reported unknown until resolved);
     - an urgent retry and duplicate send (one record per dispatch);
     - a reconciled success (the record clears);
     - concurrent stop vs dispatch, 15 trials: any send after `stop()` returned was reported;
     - unreadable state (never an all-clear).
   - The gate and the records are shared through the SQLite file, so they hold across threads and processes. Clearing a dead process's record without outbox resolution is an operator step (follow-up).
   - New enqueues, approval requests and decisions all raise or refuse.
7. **Outage.**
   - Three consecutive primary failures, whether definite or ambiguous, mark Telegram down.
   - While it is down, urgent notifications are copied to email. Routine ones are deferred: they make no attempts and burn no retries.
   - A `probe()` every `probe_interval_s` (60 s) ends the outage.
   - `watchdog()` reports C3 `stale_backlog` older than 30 min and raises one urgent alert per 30-minute bucket; a later change in the same bucket keeps the first alert.
   - While stopped, outage handling still runs for the `/stop` confirmation only, so it falls back to email if Telegram is down.

## Schema migration (`e1.v0` → `e1.v1`)

- **Trigger.** `e1.v0` is the exact schema as of `225e7df`. That text is stored verbatim as `_schema.V0_SCHEMA`, and a test checks it against `git show 225e7df` when the commit is available. Before migrating, *every* non-`sqlite_%` object in the database file (tables, indexes, triggers and views, whatever table they hang off) must equal the expected catalog with byte-identical `sqlite_master` SQL. There is no normalization. The expected catalog is built by running, in `:memory:`, exactly the statements that created a `225e7df` database: `V0_SCHEMA`, plus an explicit allowlist of objects owned by other packages, namely C2's `runtime.db.SCHEMA` and C3's `runtime.outbox.OUTBOX_SCHEMA`. `inbound_seen` gained a required `sent_at` column. The migration runs in `_schema.ensure()`, which every E1 constructor calls, inside **one** `BEGIN IMMEDIATE` transaction.
- **Failure.** A crash or error rolls everything back. The database stays `e1.v0` with its rows intact, and the next start migrates it cleanly. Startup fails closed with `JournalUnavailable`, so no traffic is routed.
- **Old processed rows** stay as burned replay records. Their `sent_at` is set to the migration time, which becomes their floor, and they are flagged `migration_flag='sent_at_unknown'`. No envelope time is invented, and they can never become replayable.
- **Old pending commands (decision: not auto-recovered).** They become `needs_operator`, have no effect, and are listed by `ChannelAuth.needs_operator()`. A redelivery is ignored as `needs_operator`. An operator can only burn one with `burn_needs_operator(channel, update_id)`; it is never executed. I chose this over refusing the migration until they're drained, because a refusal would block `/stop` on the upgraded system.
- **High-water seed.** The mark is seeded from max(now, latest `processed_at`) under `hw:__migration__`, which applies to every transport's floor and clock check. A pre-migration envelope whose row v0 had already pruned is therefore below the floor (`stale_transport`).
- **Refused.** An unknown or future version (for example `e1.v9`), and any `e1.v0` schema that differs from `V0_SCHEMA`. That includes an altered CHECK or a changed literal inside one (for example `'IF NOT EXISTS pending'`, or added whitespace), a dropped NOT NULL, a changed primary key, a changed type, an extra or missing index, and an extra trigger, view or table anywhere. Dolores's case is covered: a trigger on C3's `outbox` that touches `inbound_seen`. In every case nothing is changed: the version, the schema text and the rows are all untouched, which the tests verify.
- **Refusal is atomic.** `ensure()` reads and classifies `notifier_schema` and any existing `outbox_schema`, then runs the `e1.v0` catalog check, all before writing anything. Only on an accepted path does it create C3's objects, using C3's own `runtime.outbox.OUTBOX_SCHEMA` statements and its `meta.outbox_schema` row. That happens in the *same* `BEGIN IMMEDIATE` transaction as the migration and E1's tables. `Notifier` runs `ensure()` before it builds its `Outbox`.
  - A refused database is left byte-identical: the whole `sqlite_master` and every `meta` row.
  - Tests cover a C2-only database tagged with a future version, an `e1.v0` mismatch on a C2-only database, and an unsupported outbox schema. Each is checked through every entry point: `ensure`, `ChannelAuth`, `KillFlag` and `Notifier`.
- **Limit (fails closed).** The C2/C3 allowlist is their current creation SQL. If C2 had `ALTER`-upgraded its own `effects` table from `c2.v0` or `c2.v1`, that table's stored SQL would differ, and E1 would refuse rather than migrate. Every `e1.v0` database was created on the `c2.v2` base, so no real database is affected.
- **Fidelity.** The test fixture is generated from `V0_SCHEMA` itself, so it can't drift. A database created by the real `225e7df` code was also checked: it matches and migrates normally.

## Known gaps / decisions for review

- **Email approvals:** the C0 v0 `channel` enum has no `email` value. A DKIM-verified email *authenticates* Josh for acks and `/stop`, but an approval sent by email is refused (`channel_not_approval_capable`) rather than signed under a false channel claim. Allowing it needs a C0 contract change.
- **Direct outbox SQL:** the fallback scan, outage deferral and `/stop` sweep update C3's `outbox` table directly, because C3 has no API for them. E1 refuses to start unless `meta.outbox_schema` is `c3.v0`. Candidate C3 follow-up: `Outbox.defer(channel, until)` and `Outbox.cancel_all()`.
- **Kill flag consumers:** E1 checks the flag before every send, enqueue, request and decision. C2 workers and C8 must check `KillFlag.is_set()` before claiming tasks or executing effects; that wiring is C8.
- **C2 escalations:** `TaskStore`'s escalate callback is still at-most-once after commit. Moving it into the same transaction as `Notifier.enqueue` needs a C2 change, so it is not done here.
- **T19 item 6, UNMET for non-fenced channels:** see `/stop` above. Closing it needs either provider-side fencing (for example idempotency keys that the provider enforces) or a channel that E1 can fence. Neither exists for Telegram or email.
- **Key custody:** `HmacIssuerKey` is for tests only. The production issuer key is T11/T12.

Evidence: `tests/test_notifier.py`.
