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
```

Each worker mutation checks, inside its write transaction, that the worker still holds a live lease. If it doesn't, the call raises `LeaseLost`.

## Effect rules (T10)

1. `record_intent(task, step, BoundAction)` runs **before** execution. Recording the same step and action again returns the same entry. Recording the same step with a different action is refused (`EffectConflict`).
2. `execute(key, perform, reconcile, owner=…)` works as follows:
   - `succeeded`: returns without calling `perform`.
   - `pending` / `failed`: sets the status to `unknown` and commits, then calls `perform` once.
   - `unknown`: calls `reconcile(effect)` first. If the result is `succeeded`, the effect is resolved without re-executing. If it is `failed`, the effect is dispatched exactly once, with the same key. If it is `still_unknown`, unrecognised, or an error, the retry is refused (`EffectOutcomeUnknown`).
   - When `perform` raises `EffectFailed`, the effect becomes `failed`. Any other exception leaves it `unknown`.
   - While a live dispatcher holds the effect (`in_flight_until`), other callers get `EffectInFlight`.
3. `perform` must run `effect.action()`, the hash-checked journaled snapshot, and pass `effect.idempotency_key` to the external system.

## Not in this package

- **Wiring C2 escalations to C3:** `TaskStore`'s escalate callback is still at-most-once after commit. Moving it onto `Outbox.enqueue` in the same transaction is E1's job (the T19 notifier).
- **C4:** fencing tokens against a dispatcher that is stalled but still alive past its lease or in-flight window. C2 checks leases in the DB only.
- Real connectors, schedulers, and the worker loop.
- `contracts/v0` is loaded as top-level module `approval` (see `_contract.py`), so EffectIntent identity matches the C0 tests.

Evidence: `tests/test_task_effect_journal.py`. That file also runs the whole C0 approval suite against `JournalApprovalLedger`, including the restart test and the 8-process race.
