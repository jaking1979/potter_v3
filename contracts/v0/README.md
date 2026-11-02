# Contracts v0 (package C0, G13)

The shared interfaces that C1 (PolicyEngine), E1 (T19 notifier) and the clients build against.

| File | Contract |
| --- | --- |
| `approval_record.schema.json` | One approval for one exact action. Issued only by E1. |
| `approval.py` | Reference semantics: exact-action hash binding, single use, expiry, fail-closed. |
| `client_event.schema.json` | Backend→client event stream: AG-UI-adapted names plus Potter's own approval events. |

Rules that every implementation must keep:

- **Provenance:** a record counts only if its `issuer_signature` verifies against a pinned E1 issuer key. `decided_by` and `channel` are signed claims, not proof. With no authenticator configured, the verifier refuses.
- An approval binds `sha256(canonical BoundAction)`. Any parameter change voids it (T11/T12 item 4).
- Nonces are single-use through `NonceStore.consume_once()`, which must be atomic. In production, C1/C2 consume the nonce in the same durable transaction that writes the effect-journal intent, so concurrent consumers or a crash cannot reuse an approval. The in-memory store is reference only.
- Clients never mint approvals. `APPROVAL_REQUESTED` events are display-only; the decision returns through E1.
- Browser and computer approvals carry a screenshot reference (T38).
- No runtime behavior is enabled by this package.

Merge evidence: `tests/test_approval_contract.py`. It shows a replayed approval refused, parameters swapped after approval refused, a forged or unsigned record refused, and exactly one of 32 concurrent consumers succeeding. C0 needs approval from both agents (G7).
