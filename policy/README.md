# PolicyEngine v0 (package C1, T11/T12)

`PolicyEngine.decide(PolicyRequest) -> Decision` is the fail-closed decision core. It builds on the C0 contracts (`contracts/v0/approval.py`) and does not re-implement them.

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `allow` | Every gate passed. If an approval was required or presented, C0 `verify()` authenticated it and consumed its nonce atomically with the pending `EffectIntent`, which is returned as `decision.intent`. |
| `reconcile` | A retry of an already-consumed approval that carries the **same** `EffectIntent` the ledger recorded. The caller reconciles per T10 by reading external state first. This outcome never permits re-execution. |
| `deny` | Every other case, including any exception inside the engine. |

## Gates (the first failure denies)

1. **Request shape:** types, non-empty IDs, and the requested capability must equal `action.capability`.
2. **Clock:** it must return an `int`. If it raises or returns anything else, the request is denied.
3. **Registry:** an unknown capability is denied. Registration itself rejects unsafe declarations, such as a consequential capability that doesn't require approval.
4. **Forbidden:** always denied. A genuine approval cannot unlock a forbidden capability.
4b. **Node state (C4, optional `node_state` seam):** a write from a node that isn't `active` is denied (`node_fenced` / `node_paused` / `node_not_active`) before any approval is touched. A seam error is denied. Without the seam, nothing changes.
5. **Delegation:** root grants come from engine configuration and are never taken from the request. Each hop (`subagent` / `cron` / `retry`) can only narrow them, so the effective grants are the intersection. The maximum depth is 8.
6. **Live state (stateful capabilities):** preconditions must be non-empty, every declared key must be present, and the values must equal the live state the caller just observed.
7. **Provenance:** the engine decides trust from the source *kind*. Only `principal_direct` and `system_policy` are trusted. Unknown, missing or mixed sources taint the request. A tainted request for any write needs an approval.
8. **Approval:** C0 `verify` first runs against a probe ledger that refuses consumption. If the nonce turns out to be consumed already, only `reconcile` is possible. Otherwise a real `verify` consumes it. If an approval is presented but not required, it must still verify.

## Risk matrix (reference: `RiskMatrix` in `tests/test_policy_engine.py`)

| Risk class | No approval, trusted | No approval, untrusted | Valid approval |
| --- | --- | --- | --- |
| read | allow | allow | allow (consumed) |
| reversible_write (no approval needed) | allow | **deny** | allow |
| reversible_write (approval needed) | deny | deny | allow |
| consequential | deny | deny | allow |
| forbidden | deny | deny | deny |

## Redaction and tracing

Reasons are fixed strings from the closed `REASONS` table. C0 refusal messages are mapped to codes and never echoed. `Decision.intent` holds the action parameters and is excluded from `repr` and from `trace_record()`. `trace_hook(record)` is the seam for C6 and is a no-op by default. If the trace hook raises, the decision becomes a deny. Any nonce already consumed stays recorded as a pending intent, so C2/T10 recovery reconciles it.

## Not in this package

- Harness wiring (C8).
- Real connectors.
- A production ledger (C2's effect journal).
- Key custody.

Nothing here enables runtime behavior (T22).
