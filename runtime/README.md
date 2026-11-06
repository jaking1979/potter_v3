# runtime (package C2, decision T10)

Task store + effect journal. Built on the C0 contracts. Staging-only (T22). Nothing here performs a real external effect. Callers inject `perform` and `reconcile`.

| Module | What it does |
| --- | --- |
| `db.py` | One SQLite file holds tasks, jobs, effects and approval nonces. It runs in WAL mode with `synchronous=FULL`, so every commit is fsynced. Every write uses `BEGIN IMMEDIATE`, and each operation opens its own connection, so it is safe across threads and processes. |
| `tasks.py` | `TaskStore` handles trigger-key dedupe (task id = UUIDv5 of the trigger key), the state machine, leases with heartbeats, checkpoints, escalation, and the last-success watchdog. |
| `effects.py` | `EffectJournal` records the intent before execution, assigns a deterministic UUIDv5 idempotency key, tracks status `pending → unknown → succeeded \| failed`, and reconciles before any retry. |
| `fencing.py` | **C4 (T35 item 10).** `EpochAuthority` (monotonic epoch, compare-and-set promotion), `Node` lifecycle (FENCED → PAUSED → ACTIVE), `FencedEffects` (epoch-checked dispatch), `StubConnector` (epoch check at the external boundary), and the `Replicator` seam with `FileCopyReplicator` (Litestream stand-in). See "Fencing" below. |
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
- Real connectors, schedulers, and the worker loop.
- `contracts/v0` is loaded as top-level module `approval` (see `_contract.py`), so EffectIntent identity matches the C0 tests.

Evidence: `tests/test_task_effect_journal.py`. That file also runs the whole C0 approval suite against `JournalApprovalLedger`, including the restart test and the 8-process race.

## Fencing (package C4, decision T35 item 10)

```
start ──▶ FENCED ──rejoin() (authority still names this node)──┐
            │                                                   ▼
            └──promote() (takeover, CAS epoch+1)───────────▶ PAUSED ──unpause(reconcile)──▶ ACTIVE
any state ──authority epoch moved past ours──▶ FENCED
```

- **Authority.** `EpochAuthority` is one SQLite row `(epoch, holder)` standing in for the shared authority. `promote(node, expected)` succeeds only if the epoch is still `expected`. Concurrent promotions have exactly one winner.
- **Every activation takes a fresh epoch.** A returning node starts FENCED. `rejoin()` leaves FENCED only if the authority still names it, and then takes epoch+1, so a stalled earlier incarnation of the same node is fenced too. A superseded node stays FENCED. A node restored from a replica can only `promote()`.
- **Promoted nodes start PAUSED.** `unpause(reconcile)` reconciles every `unknown` effect first. In-flight windows owned by a lower epoch are released (`EffectJournal.release_in_flight`) because the boundary already refuses that dispatcher. If anything stays unknown, the node stays PAUSED (`UnresolvedEffects`).
- **Three checks per dispatch.** `FencedEffects.execute` checks the token before the journal is touched, inside the claim transaction (`EffectJournal.execute(fence=...)`), and just before `perform(effect, token)`.
- **Boundary.** `StubConnector.apply` refuses a token that is not the authority's current epoch or is below the highest epoch it has seen. The check and the apply share one write transaction, and `reconcile` reads under the same lock. So once a promotion commits, no older token lands, and the new primary's reconcile sees everything that did land. A refusal is `StaleEpochRefused` (an `EffectFailed`: definitely not applied).
- **C1.** `PolicyEngine(node_state=node.policy_state)` denies every write (`node_fenced`, `node_paused`, `node_not_active`) before any approval is verified or consumed. Reads are unaffected. Without the seam, C1 is unchanged.
- **Replication.** `Replicator` is the seam. `FileCopyReplicator` writes consistent snapshots (SQLite online backup) as numbered generations, and `restore` marks the copy so it starts fenced. Real Litestream is deferred to Phase F.

Limits: third-party APIs can't check our tokens. In production the boundary is an egress gateway running this check, and a window remains between that check and the third party applying the call. A replica can lag, so intents journaled after the last snapshot are missing from a restore. For both, the deterministic C2 idempotency key passed to the external system is the backstop.

Evidence: `tests/test_fencing.py`.
