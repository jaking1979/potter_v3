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
| `_schema.py` | The `notifier_state`, `approval_requests` and `inbound_log` tables, pinned by `meta.notifier_schema=e1.v0`. |

## T19 PASS 2 mapping

1. **One notifier.** Every job declares `JobSpec(name, delivery="josh"|"silent")` and gets a `JobHandle`, which can only enqueue.
   - The handle, and any `LongWork` it returns, holds only an opaque handle ID, the job name and the silent flag. The notifier, outbox and channels are resolved from a private registry on each call, so no attribute chain on the handle's data leads to a channel. A test walks the handle's references to prove this, and Dolores's `handle._n._channels['telegram'].send(msg)` now raises `AttributeError`.
   - `lint.py` fails, outside `notifier/` and `runtime/`, on: imports of a channel, the outbox, the notifier service or a network client; `.relay()` and `.send()` calls; reach-through attributes (`_n`, `_hid`, `_channels`, `__dict__`, `__closure__`, `__globals__`, `cell_contents`, `get_referents` and similar, except on `self`/`cls`); `vars()`; `getattr`/`setattr`/`delattr` with a dynamic or `_`-prefixed name; and imports of `gc`, `inspect` or `ctypes`. Network clients are refused everywhere.
   - **Honest boundary:** in-process Python cannot be a hard isolation boundary. Code in the same interpreter can always reach the private registry or the channels through some route a lint doesn't anticipate. The handle design and the lint are defence in depth. Hard enforcement is the runtime/process boundary in C8: jobs run where the notifier's channels and credentials don't exist. So the T19 release test "a job sending directly fails" holds for jobs written against the handle API and checked by the lint; it is not a claim about hostile code inside the notifier's process.
2. **Identity.** The allowlist is `human:josh`. `human:christie` is listed but disabled in the C5 seed, so she is ignored. Everyone else is ignored and logged.
   - A hand-built `InboundMessage` (for example `('telegram', josh_id, '/stop')`) has no valid adapter MAC and is ignored as `unauthenticated_transport`. So is a genuine update whose text, reply target, sender, channel or update ID was changed, and so is one signed with another key. A replayed update is ignored as `replayed`, and an old one as `stale_transport`.
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
   - **Gate:** every send passes a gate that checks the kill flag and records the send in `notifier_sending`, in one `BEGIN IMMEDIATE` transaction. `stop()` sets the flag under the same write lock.
   - **Recheck:** immediately before `channel.send`, the sender re-checks in its own transaction that its gate row still exists, that the row is younger than the dispatch timeout, and that the kill flag is clear. If any check fails, it deletes its row and aborts.
   - **Boundary:** only a call already inside `channel.send` may complete after the flag is set, and that call completes before `stop()` returns. A sender past the gate but not yet in the channel aborts at its recheck. No send of any kind can start after `stop()` returns, except the `/stop` confirmation. An expired gate can never send, `/stop` or not. C3 records a completed delivery, but the row stays suppressed, so nothing follows it.
   - **Never an all-clear from age:** `stop()` waits, up to `stop_wait_s` (default 30 s), for unexpired rows. It then reports *every* row still present in `StopReport.unconfirmed_in_flight`. Expired rows are not waited for, because their senders must abort at the recheck; they also appear in `expired_in_flight`. If the bounded wait expires, the unconfirmed set is returned as it stands. `all_clear` is true only when that set is empty. A process that died while past the gate leaves a row that keeps being reported as expired, until an operator clears it (follow-up).
   - The gate is shared through the SQLite file, so it holds across threads and processes. Deterministic pause tests cover five cases:
     - paused before the gate: `stop()` returns at once, and nothing is sent;
     - paused past the gate: `stop()` waits, and the sender aborts;
     - already inside the channel: the send completes before `stop()` returns;
     - Dolores's case, a sender paused past the gate with `/stop` at T+21 against a 20 s dispatch timeout: the row is reported expired and the sender aborts;
     - the bounded wait expires: the row is reported unconfirmed and the late sender aborts.
   - New enqueues, approval requests and decisions all raise or refuse.
7. **Outage.**
   - Three consecutive primary failures, whether definite or ambiguous, mark Telegram down.
   - While it is down, urgent notifications are copied to email. Routine ones are deferred: they make no attempts and burn no retries.
   - A `probe()` every `probe_interval_s` (60 s) ends the outage.
   - `watchdog()` reports C3 `stale_backlog` older than 30 min and raises one urgent alert per 30-minute bucket; a later change in the same bucket keeps the first alert.
   - While stopped, outage handling still runs for the `/stop` confirmation only, so it falls back to email if Telegram is down.

## Known gaps / decisions for review

- **Email approvals:** the C0 v0 `channel` enum has no `email` value. A DKIM-verified email *authenticates* Josh for acks and `/stop`, but an approval sent by email is refused (`channel_not_approval_capable`) rather than signed under a false channel claim. Allowing it needs a C0 contract change.
- **Direct outbox SQL:** the fallback scan, outage deferral and `/stop` sweep update C3's `outbox` table directly, because C3 has no API for them. E1 refuses to start unless `meta.outbox_schema` is `c3.v0`. Candidate C3 follow-up: `Outbox.defer(channel, until)` and `Outbox.cancel_all()`.
- **Kill flag consumers:** E1 checks the flag before every send, enqueue, request and decision. C2 workers and C8 must check `KillFlag.is_set()` before claiming tasks or executing effects; that wiring is C8.
- **C2 escalations:** `TaskStore`'s escalate callback is still at-most-once after commit. Moving it into the same transaction as `Notifier.enqueue` needs a C2 change, so it is not done here.
- **Key custody:** `HmacIssuerKey` is for tests only. The production issuer key is T11/T12.

Evidence: `tests/test_notifier.py`.
