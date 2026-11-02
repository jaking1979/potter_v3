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
- **24 h maximum:** an approval is refused 24 h after issue, even if it was signed with a later `expires_at` (T37 S1). A narrower policy TTL still applies.
- **Stale state:** `BoundAction.preconditions`, such as the target's version or etag, is inside the hash, so an approval given against old state doesn't match (T37 S1).
- **Single use without stranding (mandatory C1/C2 gate):** `ApprovalLedger.consume(nonce, intent)` marks the nonce used *and* durably records the pending `EffectIntent` in one atomic step; in production, one C2 effect-journal transaction. After a crash, recovery finds the bound intent and reconciles it per T10, so it can neither replay nor strand the approval. `MemoryApprovalLedger` proves the semantics only; it is not durable, and production cross-process durability is unproven until C1/C2 show it.
- Clients never mint approvals. `APPROVAL_REQUESTED` events are display-only; the decision returns through E1.
- Browser and computer approvals carry a screenshot reference (T38).
- No runtime behavior is enabled by this package.

Merge evidence: `tests/test_approval_contract.py`. It shows a replayed approval refused, parameters swapped after approval refused, a forged or unsigned record refused, refusal at 24 h even with a signed longer expiry, stale state refused, the intent recorded atomically with consumption, and exactly one of 32 concurrent consumers succeeding. C0 needs approval from both agents (G7).
