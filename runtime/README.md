# runtime (package C2, decision T10)

Task store + effect journal. Built on the C0 contracts. Staging-only (T22). Nothing here performs a real external effect. Callers inject `perform` and `reconcile`.

| Module | What it does |
| --- | --- |
| `db.py` | One SQLite file holds tasks, jobs, effects and approval nonces. It runs in WAL mode with `synchronous=FULL`, so every commit is fsynced. Every write uses `BEGIN IMMEDIATE`, and each operation opens its own connection, so it is safe across threads and processes. |
| `tasks.py` | `TaskStore` handles trigger-key dedupe (task id = UUIDv5 of the trigger key), the state machine, leases with heartbeats, checkpoints, escalation, and the last-success watchdog. |
| `effects.py` | `EffectJournal` records the intent before execution, assigns a deterministic UUIDv5 idempotency key, tracks status `pending → unknown → succeeded \| failed`, and reconciles before any retry. |
| `fencing.py` | **C4 (T35 item 10).** `EpochAuthority` (monotonic epoch, compare-and-set promotion), `Node` lifecycle (FENCED → PAUSED → ACTIVE), `FencedEffects` (epoch-checked dispatch), `StubConnector` (epoch check at the external boundary), and the `Replicator` seam with `FileCopyReplicator` (Litestream stand-in). See "Fencing" below. |
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
     - `failed`: the effect is dispatched exactly once, with the same key, **but only if a late landing of the earlier dispatch is ruled out**. Either the `fence` seam confirms the earlier dispatcher is fenced off (C4 fills this seam; with no fence, or if it errors, the answer is refuse), or the contract *recorded on the effect* at first dispatch, both its identity and its `contract_digest`, matches an entry in the `trusted_idempotent` registry. That registry is a frozen `TrustedIdempotentRegistry` of `ConnectorContract(identity, adapter, contract_ref, idempotency_semantics)` records, injected when the journal is constructed. The digest is sha256 of the canonical (identity, contract_ref, adapter qualified name, semantics). A registry rebuilt after a restart that maps the identity to another contract, adapter or guarantee does not match, so the retry is refused. A row with no digest (dispatched while untrusted, or migrated from `c2.v0`/`c2.v1`) is also refused. With no registry, the answer is refuse. No caller flag or callable attribute can grant this. Without one of these, "not applied yet" is not trusted: the effect stays `unknown` in `unresolved()`, and the call raises `EffectOutcomeUnknown`. A stalled worker or a timed-out request might still land later.
     - `still_unknown`, an unrecognised verdict, or an error: the retry is refused (`EffectOutcomeUnknown`).
   - An expired in-flight claim is never permission to re-dispatch on its own.
   - When `perform` raises `EffectFailed`, the effect becomes `failed`. Any other exception leaves it `unknown`.
   - While a live dispatcher holds the effect (`in_flight_until`), other callers get `EffectInFlight`.
3. `execute(..., connector=<identity>)` records the identity on the first dispatch. Every later dispatch must use the same identity (`EffectConflict` otherwise). For a trusted identity, `perform` must be that contract's registered adapter.
4. `perform` must run `effect.action()`, the hash-checked journaled snapshot, and pass `effect.idempotency_key` to the external system.

## Not in this package

- **C3:** the outbox and durable delivery of escalation notifications. The callback is at-most-once after commit.
- **Wiring C2 escalations to C3:** `TaskStore`'s escalate callback is still at-most-once after commit. Moving it onto `Outbox.enqueue` in the same transaction is E1's job (the T19 notifier).
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
- **Promoted nodes start PAUSED.** `unpause(reconcile)` reconciles every `unknown` effect first. Live windows owned by a lower epoch are ended with `EffectJournal.expire_in_flight`, which keeps the owner on the row. If anything stays unknown, the node stays PAUSED (`UnresolvedEffects`).
- **C2 `fence` seam.** `Node` builds its journal with `fence=node.c2_fence`. It returns `True` only when all of these hold:
  - the node is ACTIVE, or PAUSED inside `unpause`, which reconciles but never dispatches;
  - the node holds the authority's current epoch;
  - the row names the earlier dispatcher (`node@epoch:N`; an untagged pre-C4 owner counts as epoch 0), and that epoch is older than the node's;
  - the effect's **recorded** connector (C2 `effect.connector`) is bound to a boundary in the node's `FencedBoundaryRegistry`, and the contract digest recorded at dispatch still matches.

  An epoch only protects an effect if its connector refuses stale tokens at a boundary that is linearized with promotion. A `perform` that ignores its token gets nothing from the epoch. Anything else returns `False` and C2 keeps the effect unresolved. That includes:
  - no owner on the row, or our own epoch (a timed-out call of ours may still be on the wire);
  - a connector that is unregistered, rotated (digest mismatch), or unbound (dispatched around `FencedEffects`). Every decision is appended to `node.fence_log`. To keep that evidence, an ambiguous settle in C2 now keeps `in_flight_owner` and only expires the window.
- **Fenced boundary registry.** `FencedBoundaryContract` mirrors C2's `ConnectorContract` at `claude/c2-task-effect-journal@fdd3137`. Its fields are `identity`, `adapter`, `contract_ref`, `adapter_artifact_digest`, `fencing_scope` and `fencing_semantics`.
  - **Artifact digest.** `adapter_artifact_digest` is the sha256 of the reviewed implementation's source, supplied by trusted configuration. `boundary_artifact_digest()` computes it the C2 way: the source file bytes, plus a check that the **loaded** code is structurally equal to what that file compiles to. Unreadable source (builtins, `exec` code) is refused.
  - **Scope.** `fencing_scope` is a non-secret `FencingScope` (C2 `IdempotencyScope`-style) covering the boundary kind, the epoch authority it checks, the store where check and apply are atomic, the check version, and a config hash. The adapter reports it live through `fencing_scope()`.
  - **Contract digest.** The digest covers all of the above plus the adapter's qualified name. So a changed body under the same name, an in-memory patch, or a re-pointed scope is a different or unverifiable contract.
  - **Registry.** `FencedBoundaryRegistry` is frozen and given to `Node(boundaries=...)`. It is **empty by default**. Each entry is verified when the registry is built (artifact against the actual source, scope against the live report), and a mismatch raises `BoundaryVerificationError`.
  - **Binding.** Before anything is claimed, `FencedEffects.execute` binds the key to its connector in `fenced_bindings`, which lives in the journal DB and replicates with it.
    - For a registered identity, `perform` must be the reviewed adapter. The contract is **re-verified live**: artifact, scope, and that the scope names this node's own authority. Then its digest is recorded.
    - An unregistered identity is recorded without a digest and is never cleared by the fence.
    - A different binding for the same key is refused (`EffectConflict`).
  - **Recovery.** `c2_fence` repeats the live verification before vouching.
  - **Whole-implementation verification.** `verify_boundary_implementation()` runs at registry build, at every bind and before every vouch. Checking `perform` alone is not enough: the token check and apply live in `apply`, `EpochAuthority.boundary` and the module's helpers. For each module that defines the adapter's function or bound class, it compiles the module source and compares, structurally (as C2 does):
    - every module-level function and class the source defines: same object at the same name, nothing removed, nothing added;
    - every method of those classes: plain, static and class methods, and property parts. A `contextmanager`-wrapped method is unwrapped, and the wrapper must be contextlib's own helper.
    - the bound instance's class, which must be the module's own class at that name;
    - the bound instance itself: no instance attribute may shadow a class attribute or hold a callable.
    - collaborators: an instance attribute whose class derives from a verified-module class, like the stub's `EpochAuthority`, must be exactly that class, with no instance attribute shadowing its methods.

    In-memory patches of `apply` or a helper, instance overrides, and impostor subclasses all mean "not proven".
  - **Test seams are data, not code.** `StubConnector.gate_before_apply` and `gate_in_check` are `(reached, resume)` pairs of Events (type-checked, `threading.Event` or a multiprocessing Event). `fail_before_apply` is a message string. A callable seam in the boundary path would be a token bypass. Node B can only verify B's own loaded code, so the dispatcher's own bind-time verification is what certifies a late call.
  - **Residuals, stated:**
    - Methods generated by the class machinery, which have no source (dataclass dunders, `typing.Protocol`'s `__init__` and `__subclasshook__`), are accepted by origin and name, not by content.
    - **Trust in imported dependencies remains a separate deployment gate.** That covers other first-party modules (`runtime.effects`, `runtime.db`), the stdlib (`sqlite3`, `threading`) and any third-party package. Verification covers only the adapter's own module(s).
    - Injected callables held by collaborators that are not on the check-and-apply path (the authority's clock) are allowed.
  - **Stub.** `StubConnector.contract(adapter_artifact_digest)` needs the digest from configuration, so the stub can't vouch for itself.
  - **Independence from C2.** Fencing does not use C2's trusted idempotent registry or its digest. The artifact helpers are a C4 copy of C2's algorithm, because that C2 change isn't on `claude/c-integration` yet. When it is integrated, they become imports.
- **Three checks per dispatch.** `FencedEffects.execute` checks the token before the journal is touched, inside the claim transaction (`EffectJournal.execute(guard=...)`), and just before `perform(effect, token)`.
- **Connector identity.** `FencedEffects.execute(..., connector=...)` passes the required C2 connector identity through. C2 records it on the effect, and every later dispatch must match it. `StubConnector.identity` (default `stub.epoch-gateway`) is deliberately **not** in the journal's `TrustedIdempotentRegistry`. Re-dispatch after an expired or ambiguous claim is cleared by `c2_fence` alone, using the fenced boundary registry above. `FencedEffects` wraps `perform` to pass the token, so a trusted connector, whose `perform` C2 requires to be the registered adapter, can't be routed through it as-is.
- **Boundary.** `StubConnector` keeps its state (highest epoch seen, applied and refused rows) in the **authority's** SQLite DB. `apply` runs inside `EpochAuthority.boundary()`, one `BEGIN IMMEDIATE` transaction that `promote()` also needs. In that transaction it refuses a token below the highest epoch seen, or not equal to the committed epoch, and otherwise records the apply. So an apply and a promotion are strictly ordered. Once a promotion commits, no older token lands, and an apply already in progress delays the promotion until it commits. `reconcile` reads under the same lock. A refusal is `StaleEpochRefused` (an `EffectFailed`: definitely not applied).
- **C1.** `PolicyEngine(node_state=node.policy_state)` denies every write (`node_fenced`, `node_paused`, `node_not_active`) before any approval is verified or consumed. Reads are unaffected. Without the seam, C1 is unchanged.
- **Replication.** `Replicator` is the seam. `FileCopyReplicator` writes consistent snapshots (SQLite online backup) as numbered generations, and `restore` marks the copy so it starts fenced. Real Litestream is deferred to Phase F.

Limits: third-party APIs can't check our tokens. In production the boundary is an egress gateway running this check. The gateway must share one transactional store, or one lock, with the epoch authority, as the stub does. Reading the authority from a separate store reopens the gap where a promotion commits between the check and the apply. A window also remains between the gateway's commit and the third party applying the call. **Registering a real network gateway** in `FencedBoundaryRegistry` requires proven external fencing for the calls it has already accepted but not yet completed. The provider must refuse, or deduplicate, a late call carrying an older token or the same idempotency key. Without that proof, keep the gateway unregistered: its ambiguous effects stay unresolved until reconciled authoritatively. A replica can lag, so intents journaled after the last snapshot are missing from a restore. For both, the deterministic C2 idempotency key passed to the external system is the backstop.

Evidence: `tests/test_fencing.py`.
