# runtime (package C2, decision T10)

Task store + effect journal. Built on the C0 contracts. Staging-only (T22). Nothing here performs a real external effect. Callers inject `perform` and `reconcile`.

| Module | What it does |
| --- | --- |
| `db.py` | One SQLite file holds tasks, jobs, effects and approval nonces. It runs in WAL mode with `synchronous=FULL`, so every commit is fsynced. Every write uses `BEGIN IMMEDIATE`, and each operation opens its own connection, so it is safe across threads and processes. |
| `tasks.py` | `TaskStore` handles trigger-key dedupe (task id = UUIDv5 of the trigger key), the state machine, leases with heartbeats, checkpoints, escalation, and the last-success watchdog. |
| `effects.py` | `EffectJournal` records the intent before execution, assigns a deterministic UUIDv5 idempotency key, tracks status `pending → unknown → succeeded \| failed`, and reconciles before any retry. |
| `approval_ledger.py` | `JournalApprovalLedger` is the production C0 `ApprovalLedger`. `consume(nonce, intent)` is one transaction: it inserts the pending effect intent and marks the nonce used. |

## Task states

```
queued ──claim──▶ running ──complete──▶ done
   ▲                │  │ └─fail(retryable)──▶ queued   (until 3 consecutive failures)
   │                │  ├─fail(final)──────▶ failed
   │                │  ├─3rd failure──────▶ escalated  (escalate callback, once)
   │                │  └─lease expired────▶ reclaimed by the next claim (counts as a failure)
   │                └─wait_for_approval─▶ waiting_approval ─approved─┘ / ─denied─▶ failed
                                       (a denied or expired approval counts toward job escalation)
```

Each worker mutation checks, inside its write transaction, that the worker still holds a live lease. If it doesn't, the call raises `LeaseLost`. Every `TaskStore` write samples trusted time *after* `BEGIN IMMEDIATE` holds the lock; the `now=` test value is applied there too. Waiting on the lock is therefore measured: a heartbeat that gets the lock after its lease expired is refused, and the reclaim at that time succeeds. A lease holder whose clock reads earlier than the task's last write (rollback) also gets `LeaseLost`.

**Non-finite inputs.**
- **Leases.** `claim` and `heartbeat` reject a `lease_s` that is NaN, ±inf, ≤ 0, or > `MAX_LEASE_S` (1 day), before any write. A persisted lease that isn't a finite time within `MAX_LEASE_S` of now is treated as **expired**: the task can be reclaimed, the reclaim counts as a failure, and the holder gets `LeaseLost`. This covers +inf written before validation and NaN, which SQLite stores as NULL.
- **Jobs.** `register_job` rejects a cadence or tolerance that is non-finite, out of range, or whose product overflows, on both create and update. `stale_jobs` applies the same upper limits to persisted rows, so a finite but over-limit row is flagged too. An invalid update leaves the row unchanged.
- **Watchdog.** `stale_jobs` reports a persisted row it can't evaluate as `Job.fault` and still evaluates every other job. If its clock is not finite, it reports every job with a fault.
- **Clock.** Task-store and journal writes refuse a clock reading that is not finite.

## Effect rules (T10)

1. `record_intent(task, step, BoundAction)` runs **before** execution. Recording the same step and action again returns the same entry. Recording the same step with a different action is refused (`EffectConflict`).
2. `execute(key, perform, reconcile, owner=…)` works as follows:
   - `succeeded`: returns without calling `perform`.
   - `pending` / `failed`: sets the status to `unknown` and commits, then calls `perform` once.
   - `unknown`: calls `reconcile(effect)` first.
     - `succeeded`: the effect is resolved without re-executing.
     - `failed`: the effect is dispatched exactly once, with the same key, **but only if a late landing of the earlier dispatch is ruled out**. Either the `fence` seam confirms the earlier dispatcher is fenced off (C4 fills this seam; with no fence, or if it errors, the answer is refuse), or the contract *recorded on the effect* at first dispatch, both its identity and its `contract_digest`, matches an entry in the `trusted_idempotent` registry. That registry is a frozen `TrustedIdempotentRegistry` of `ConnectorContract(identity, adapter, contract_ref, adapter_artifact_digest, idempotency_scope, idempotency_semantics)` records, injected when the journal is constructed. `adapter_artifact_digest` is the sha256 of the reviewed adapter's source file, supplied by trusted configuration. When the registry is built, it is checked against the adapter's actual source; if the two differ, if the source can't be read (builtins, C extensions, exec/REPL code), or if the loaded function doesn't compile identically from that file, construction raises `AdapterVerificationError`. `idempotency_scope` is a non-secret `IdempotencyScope` naming the provider namespace the guarantee holds in: provider, account/tenant, endpoint, dedupe scope and version, retention window, and config hash. The adapter must report its current scope through a read-only `idempotency_scope()`. That report is checked when the registry is built, before every dispatch through a trusted identity, and before any retry after an ambiguous outcome. If the scope has changed, or can't be reported, the call is refused. A finite dedupe window is enforced, not just recorded. A retry after an ambiguous outcome is allowed only if the scope declares a provider-*enforced* request lifetime `L` (the provider rejects requests older than `L`, e.g. through signed request expiry), and `retention_window_s >= L + (retry_time - first_dispatched_at) + retention_margin_s`. This is checked at retry time against the journaled first-dispatch time (`effects.first_dispatched_at`, schema `c2.v3`). If no enforced lifetime is declared, no first-dispatch time is recorded, or the inequality fails, the effect stays `unknown`. A `failed` verdict accepted on that window basis is not timeless. The basis is persisted (`effects.failed_basis`: `definite`, `fence` or `window`; schema `c2.v4`). Every dispatch claim of a `window`-basis or NULL-basis `failed` effect re-runs the full check (contract digest, scope, and window) at the claim's own clock time, including after a restart. If the check fails, the effect goes back to `unknown`. `definite` (the provider rejected it) and `fence` keep their behaviour.

**Signed deadlines bound at the claim.** Eligibility, the kill switch, deadline minting and the claim happen in one write transaction, using the clock read inside it. Waiting for the write lock or a slow reconcile is therefore measured, never assumed.
- **The ticket.** The journal's `DispatchDeadlineSigner` signs `(key, dispatch number, deadline = claim time + L)`. The adapter gets only the ticket (`effect.dispatch_ticket`) and cannot mint a later deadline. A trusted provider must verify the ticket and reject anything that arrives after its deadline.
- **The envelope.** A window retry is allowed only if `retention ≥ max(recorded deadlines, now + L) − earliest claim + margin`.
- **Refused outright:**
  - no signer is configured;
  - L is not declared;
  - any earlier claim had no deadline (`unbounded_dispatch`);
  - recorded bounds are missing (migrated rows);
  - the clock reads earlier than a recorded claim (rollback).
- **`kill_switch(effect)`.** This is the E2 seam. It is checked inside the claim transaction and again just before send; anything other than `False` refuses, and a pre-send kill reverts the claim.
- **`now=`.** Accepted only by a journal built with `trusted_test_clock=True`.

**Ticket validation.** `DispatchDeadlineSigner` rejects non-finite deadlines and bad field types at mint and at verify. That includes NaN/Infinity tokens and literals such as `1e400` that parse to infinity, even when the MAC matches. `admits(ticket, key, at)` is the reference provider check, and it fails closed on a non-finite `at`.

**Limits:**
- HMAC is a reference model for a provider-verified signed expiry. A real connector needs a provider that actually enforces the deadline.
- The trust boundary assumes the adapter and the signer key are separated by deployment, not by Python.
- A dispatch that ran before `c2.v5` has no recorded deadline and is never window-trusted. The contract digest is sha256 of the canonical (identity, contract_ref, adapter qualified name, adapter artifact digest, idempotency scope, semantics). A body changed under the same name, or an adapter pointed at a different namespace, is therefore a different contract. A registry rebuilt after a restart that maps the identity to another contract, adapter or guarantee does not match, so the retry is refused. A row with no digest (dispatched while untrusted, or migrated from `c2.v0`/`c2.v1`) is also refused. With no registry, the answer is refuse. No caller flag or callable attribute can grant this. Without one of these, "not applied yet" is not trusted: the effect stays `unknown` in `unresolved()`, and the call raises `EffectOutcomeUnknown`. A stalled worker or a timed-out request might still land later.
     - `still_unknown`, an unrecognised verdict, or an error: the retry is refused (`EffectOutcomeUnknown`).
   - An expired in-flight claim is never permission to re-dispatch on its own.
   - When `perform` raises `EffectFailed`, the effect becomes `failed`. Any other exception leaves it `unknown`.
   - While a live dispatcher holds the effect (`in_flight_until`), other callers get `EffectInFlight`.
3. `execute(..., connector=<identity>)` records the identity on the first dispatch. Every later dispatch must use the same identity (`EffectConflict` otherwise). For a trusted identity, `perform` must be that contract's registered adapter.
4. `perform` must run `effect.action()`, the hash-checked journaled snapshot, and pass `effect.idempotency_key` to the external system.

## Not in this package

- **C3:** the outbox and durable delivery of escalation notifications. The callback is at-most-once after commit.
- **C4:** the fencing implementation. C2 provides the `fence` seam and refuses unfenced re-dispatch. Leases are checked in the DB only.
- Real connectors, schedulers, and the worker loop.
- `contracts/v0` is loaded as top-level module `approval` (see `_contract.py`), so EffectIntent identity matches the C0 tests.

Evidence: `tests/test_task_effect_journal.py`. That file also runs the whole C0 approval suite against `JournalApprovalLedger`, including the restart test and the 8-process race.
