# t39 — receipt registry and consumer gates (B1, production path)

This package holds two of the production pieces of the ratified T39 receipt gate. Dolores's production verifier is **injected**, not implemented here. It has been revised after Dolores's review of `79e4c0e`.

## Lifecycle
1. **Merge admission (`admit_merge`).** The verifier checks the attestation pair, and the registry **issues** the receipt. Its two nonces are consumed in the same transaction. This is the only place nonces are consumed. If the issuer retries with byte-identical facts, the result is `ALREADY_ISSUED`. If either nonce was used before for anything else, the result is `REPLAY`.
2. **Every later consumer (`require_issued`):** the release builder, host updater, rollback and manual deploy. Each one:
   - re-verifies the signed receipt independently ("CI and the host gate both verify receipts");
   - requires the **exact issued record** to exist;
   - logs its use.

   Nothing is consumed, so the CI → builder → host → rollback sequence all runs on one receipt. A never-issued or mismatched receipt fails at every consumer, and on `main` that is flagged as an incident.
3. **Detector (`unreceipted_main_commits`):** reads issued receipts only, never bare nonces.

## Integrity (`receipt_registry.py`)
- **One hash chain:** genesis, every issuance and every use form a single hash chain. On open, the whole chain is re-verified, and the nonce and receipt indexes must match it exactly. An edited, forged, deleted or partial row makes the registry unavailable.
- **External anchor:** after every commit, the chain head (sequence number and hash) is advanced in an **external anchor**. The anchor only moves forward and is protected by a lock.
  - On open, the registry must contain the anchored head. A **restored older backup** or a **fork** is refused.
  - If the process crashed between a commit and the anchor write, a registry ahead of its anchor is accepted only when it extends the anchored head, and the anchor then catches up.
- **One-time bootstrap:** `initialize` is an explicit, one-time step. It is refused if the file exists **or the anchor is already set**, so losing state can never quietly restart with an empty registry. Opening never creates anything.
- **Durability:** WAL, `synchronous=FULL`, a schema-version check and `quick_check`.

## Tests (`tests/test_t39_store_gate.py`, 20)
- **Lifecycle:** the full CI → builder → host → rollback sequence, including across a restart; a never-issued receipt refused at every consumer; idempotent issuer retry; replay and partial-overlap replay (T39 test 3); a commit or tree changed after review (T39 test 4); verifier failures and incomplete facts, including identical keys for both roles; detector input; and 5 processes racing so that exactly one issues.
- **Integrity:** one-time bootstrap including after state loss; a restored older backup; crash recovery; a forked anchor; a forged index row; a deleted nonce; edited and deleted chain rows; a chain edit that leaves the indexes consistent; a garbage or missing anchor; a closed registry.
- **Mutation checks:** each of these makes at least one test fail when removed:
  - the anchor check
  - the chain-hash check
  - the nonce check at issuance
  - the issued-record check at consumers

## Not here
- **The verifier:** signatures, key policy and revocation (tests 2, 5 and 6), which is Dolores's package.
- **Where the anchor lives in production.** A file is used in the tests. In production it should be on a different host from the registry, for example the Brooklyn node under T35.
- **Wiring into the real consumers,** and the hourly detector job with its alerts and reverts.
