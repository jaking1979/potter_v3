# Contracts v0 (package C0, G13)

The shared interfaces that C1 (PolicyEngine), E1 (T19 notifier) and the clients build against.

| File | Contract |
| --- | --- |
| `approval_record.schema.json` | One approval for one exact action. Issued only by E1. |
| `approval.py` | Reference semantics: exact-action hash binding, single use, expiry, fail-closed. |
| `client_event.schema.json` | Backend→client event stream: AG-UI-adapted names plus Potter's own approval events. |

Rules that every implementation must keep:

- An approval binds `sha256(canonical BoundAction)`. Any parameter change voids it (T11/T12 item 4).
- Nonces are single-use. The production nonce store is durable (C2) and survives restart.
- Clients never mint approvals. `APPROVAL_REQUESTED` events are display-only; the decision returns through E1.
- Browser and computer approvals carry a screenshot reference (T38).
- No runtime behavior is enabled by this package.

Merge evidence: `tests/test_approval_contract.py` shows a replayed approval refused and parameters swapped after approval refused. C0 needs approval from both agents (G7).
