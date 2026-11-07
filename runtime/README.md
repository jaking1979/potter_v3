# runtime (package C2, decision T10)

Task store + effect journal. Built on the C0 contracts. Staging-only (T22). Nothing here performs a real external effect. Callers inject `perform` and `reconcile`.

| Module | What it does |
| --- | --- |
| `db.py` | One SQLite file holds tasks, jobs, effects and approval nonces. It runs in WAL mode with `synchronous=FULL`, so every commit is fsynced. Every write uses `BEGIN IMMEDIATE`, and each operation opens its own connection, so it is safe across threads and processes. |
| `tasks.py` | `TaskStore` handles trigger-key dedupe (task id = UUIDv5 of the trigger key), the state machine, leases with heartbeats, checkpoints, escalation, and the last-success watchdog. |
| `effects.py` | `EffectJournal` records the intent before execution, assigns a deterministic UUIDv5 idempotency key, tracks status `pending → unknown → succeeded \| failed`, and reconciles before any retry. |
| `outbox.py` | **C3 (T4/T17, T19 items 1/3/7).** `Outbox.enqueue(tx, message)` writes the notification in the caller's business transaction. `relay(send, reconcile, owner)` records the dispatch (`unknown`) before `send`. An unknown outcome is reconciled first; if it stays unknown, a routine message becomes `abandoned_unknown` and is not resent, and an urgent one is resent once with the same key. `ack()` suppresses sibling channels. `stale_backlog()` feeds the watchdog. The table and `meta.outbox_schema=c3.v0` are created by `Outbox`, so `db.py`'s C2 schema is unchanged. |
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

Each worker mutation checks, inside its write transaction, that the worker still holds a live lease. If it doesn't, the call raises `LeaseLost`.

## Effect rules (T10)

1. `record_intent(task, step, BoundAction)` runs **before** execution. Recording the same step and action again returns the same entry. Recording the same step with a different action is refused (`EffectConflict`).
2. `execute(key, perform, reconcile, owner=…)` works as follows:
   - `succeeded`: returns without calling `perform`.
   - `pending` / `failed`: sets the status to `unknown` and commits, then calls `perform` once.
   - `unknown`: calls `reconcile(effect)` first.
     - `succeeded`: the effect is resolved without re-executing.
     - `failed`: the effect is dispatched exactly once, with the same key, **but only if a late landing of the earlier dispatch is ruled out**. Either the `fence` seam confirms the earlier dispatcher is fenced off (C4 fills this seam; with no fence, or if it errors, the answer is refuse), or the contract *recorded on the effect* at first dispatch, both its identity and its `contract_digest`, matches an entry in the `trusted_idempotent` registry. That registry is a frozen `TrustedIdempotentRegistry` of `ConnectorContract(identity, adapter, contract_ref, adapter_artifact_digest, idempotency_scope, idempotency_semantics)` records, injected when the journal is constructed. `adapter_artifact_digest` is the sha256 of the reviewed adapter's source file, supplied by trusted configuration. When the registry is built, it is checked against the adapter's actual source; if the two differ, if the source can't be read (builtins, C extensions, exec/REPL code), or if the loaded function doesn't compile identically from that file, construction raises `AdapterVerificationError`. `idempotency_scope` is a non-secret `IdempotencyScope` naming the provider namespace the guarantee holds in: provider, account/tenant, endpoint, dedupe scope and version, retention window, and config hash. The adapter must report its current scope through a read-only `idempotency_scope()`. That report is checked when the registry is built, before every dispatch through a trusted identity, and before any retry after an ambiguous outcome. If the scope has changed, or can't be reported, the call is refused. The contract digest is sha256 of the canonical (identity, contract_ref, adapter qualified name, adapter artifact digest, idempotency scope, semantics). A body changed under the same name, or an adapter pointed at a different namespace, is therefore a different contract. A registry rebuilt after a restart that maps the identity to another contract, adapter or guarantee does not match, so the retry is refused. A row with no digest (dispatched while untrusted, or migrated from `c2.v0`/`c2.v1`) is also refused. With no registry, the answer is refuse. No caller flag or callable attribute can grant this. Without one of these, "not applied yet" is not trusted: the effect stays `unknown` in `unresolved()`, and the call raises `EffectOutcomeUnknown`. A stalled worker or a timed-out request might still land later.
     - `still_unknown`, an unrecognised verdict, or an error: the retry is refused (`EffectOutcomeUnknown`).
   - An expired in-flight claim is never permission to re-dispatch on its own.
   - When `perform` raises `EffectFailed`, the effect becomes `failed`. Any other exception leaves it `unknown`.
   - While a live dispatcher holds the effect (`in_flight_until`), other callers get `EffectInFlight`.
3. `execute(..., connector=<identity>)` records the identity on the first dispatch. Every later dispatch must use the same identity (`EffectConflict` otherwise). For a trusted identity, `perform` must be that contract's registered adapter.
4. `perform` must run `effect.action()`, the hash-checked journaled snapshot, and pass `effect.idempotency_key` to the external system.

## Not in this package

- **C3:** the outbox and durable delivery of escalation notifications. The callback is at-most-once after commit.
- **C4:** the fencing implementation. C2 provides the `fence` seam and refuses unfenced re-dispatch. Leases are checked in the DB only.
- **Wiring C2 escalations to C3:** `TaskStore`'s escalate callback is still at-most-once after commit. Moving it onto `Outbox.enqueue` in the same transaction is E1's job (the T19 notifier).
- **C4:** fencing tokens against a dispatcher that is stalled but still alive past its lease or in-flight window. C2 checks leases in the DB only.
- Real connectors, schedulers, and the worker loop.
- `contracts/v0` is loaded as top-level module `approval` (see `_contract.py`), so EffectIntent identity matches the C0 tests.

Evidence: `tests/test_task_effect_journal.py`. That file also runs the whole C0 approval suite against `JournalApprovalLedger`, including the restart test and the 8-process race.
