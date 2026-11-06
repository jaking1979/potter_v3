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

- **C3:** the outbox and durable delivery of escalation notifications. The callback is at-most-once after commit.
- **C4:** fencing tokens against a dispatcher that is stalled but still alive past its lease or in-flight window. C2 checks leases in the DB only.
- Real connectors, schedulers, and the worker loop.
- `contracts/v0` is loaded as top-level module `approval` (see `_contract.py`), so EffectIntent identity matches the C0 tests.

Evidence: `tests/test_task_effect_journal.py`. That file also runs the whole C0 approval suite against `JournalApprovalLedger`, including the restart test and the 8-process race.
