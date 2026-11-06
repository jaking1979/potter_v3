# B1 — T39 receipt gate implementation work

Status: DESIGN IN PROGRESS. This document is not a verifier, a receipt, a passing test, a deployment authorization, or a change to the ratified contract. Source: Notion Design Council T39 Decision & Reconciliation Record, ratified 2026-11-02. Owner Dolores; independent reviewer Claude.

## Required boundaries

- Author and opposite reviewer sign exact commit SHA, tree hash, decision-manifest hash, evidence hash, role, verdict, nonce and expiry with separate platform-held private keys, SSH namespace `potter-review`. Hosts receive pinned public keys only.
- A valid receipt requires distinct author and reviewer keys, required checks green, no open request-changes verdict, and an unexpired, unconsumed nonce. CI and host each verify it. State must survive restarts and concurrent attempts must not both consume the same nonce.
- Release builder and updater fail closed on absent, invalid, stale, revoked or untrusted receipts. Check every entry point, including manual, rollback, and alternate deployment paths; merely adding a CI job does not enforce consumption.
- An unreceipted main commit is a security incident: alert, revert, and block release; direct pushes must be detected within one hour. Main itself is not protected from direct pushes by GitHub.
- Key-set changes require Josh's one-tap approval. Preserve historical key and revocation metadata so archived receipts remain verifiable without accepting revoked keys for new receipts. Initial trusted key setup and independent custody require explicit evidence.

## Acceptance evidence, bound to exact component versions

1. Direct main push is detected within one hour and cannot be deployed.
2. Same-key self-review is rejected.
3. Replayed and expired attestations are rejected.
4. A commit added after review voids the receipt.
5. Host-held key material cannot forge an agent signature.
6. Revoked keys fail for new receipts while archived receipts still verify.

Also exercise altered tree, decision manifest and evidence; wrong namespace, roles and verdict; missing checks, open change requests, unavailable verifier/state, concurrent nonce use, restart, and alternate release/update paths. Failing or unavailable checks must not become green through default values. Evidence must identify candidate SHA, test versions, time, result, and artifacts, without private keys or secrets.

## Work remaining

Define canonical signed bytes and a versioned receipt format with cross-platform test vectors; implement independent verification and atomic replay state; establish protected key custody and approved trust anchors; integrate CI, host, release builder, updater, and monitoring/revert; run adversarial integration tests; request Claude's independent exact-head T2 review. The two signing platforms, their secure signing interfaces, existing host access, and the consumer deployment paths have not yet been verified in this session. Do not invent those interfaces or claim T39 is enforced before end-to-end evidence exists. B1 remains unmergeable until all required evidence and review exist.
