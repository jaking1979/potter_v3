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
- **Single use without stranding (mandatory C1/C2 gate):** `ApprovalLedger.consume(nonce, intent)` marks the nonce used *and* durably records the pending `EffectIntent` in one atomic step; in production, one C2 effect-journal transaction. After a crash, recovery finds the bound intent and reconciles it per T10, so it can neither replay nor strand the approval. The intent carries the exact canonical action, integrity-checked against the approved hash, so recovery after a restart knows which external operation to reconcile. `SqliteApprovalLedger` is a durable reference: tests rebuild a fresh ledger after a simulated restart and race 8 processes. Production durability is proven only by C2's effect journal; this is a mandatory C1/C2 gate.
- **Redaction:** stored action parameters live only in the local effect journal, never in model context or logs. Secrets are never parameters; use SecretRefs (T11/T12).
- **Live-state revalidation is a C1 gate:** empty `preconditions` does not prove the caller revalidated live state. C1 must require preconditions for stateful capabilities.
- Clients never mint approvals. `APPROVAL_REQUESTED` events are display-only; the decision returns through E1.
- Browser and computer approvals carry a screenshot reference (T38).
- No runtime behavior is enabled by this package.

Merge evidence: `tests/test_approval_contract.py`. It shows a replayed approval refused, parameters swapped after approval refused, a forged or unsigned record refused, refusal at 24 h even with a signed longer expiry, stale state refused, the intent recorded atomically with consumption, the exact action reconstructed after restart from a fresh durable ledger, tampering detected, exactly one of 32 threads succeeding, and exactly one of 8 processes succeeding. C0 needs approval from both agents (G7).
