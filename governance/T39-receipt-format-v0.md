# T39 receipt format v0 — implementation design

**Status:** design only. This document is not a receipt verifier, an approval to merge, key-custody evidence, or a change to the ratified T39 contract.

## Signed attestation

Each author or opposite reviewer signs the existing canonical UTF-8 JSON bytes produced by `experiments/b1_ssh/claims.py`. The exact field set is:

```json
{
  "schema": "potter.attestation/v0",
  "commit": "<40 lowercase hex>",
  "tree": "<40 lowercase hex>",
  "manifest": "<64 lowercase hex>",
  "evidence": "<64 lowercase hex>",
  "role": "author | reviewer",
  "verdict": "approve | request_changes",
  "nonce": "<64 lowercase hex>",
  "expires_at": "<integer Unix seconds>"
}
```

The v0 claims above do not carry an agent identity; they are insufficient to prove independent agents. A future implementation must bind an authenticated agent identity to each public key in a separately trusted, Josh-approved key set and bind that identity to the signed attestation. Different fingerprints alone do not prove different agents. This design does not change the experimental v0 encoder or claim those bindings exist.

The future trusted consumer supplies the expected candidate bindings, current time, and maximum lifetime. The attestation does not supply those authorities. The OpenSSH namespace is `potter-review`.

## Receipt envelope

A future verifier must persist a versioned receipt envelope containing:

- the two canonical claim byte strings and detached signatures;
- the trusted-key identifier and pinned public-key fingerprint for each signer;
- exact required-check names, immutable result identifiers, and their statuses;
- the request-changes state and evidence used to establish that none remains open;
- candidate, manifest, and evidence hashes already bound by both attestations;
- nonce-consumption outcome and verifier version.

A receipt is valid only if all of these are true:

1. exactly one author and one reviewer attestation bind the same expected candidate;
2. each claim signature verifies against a key pinned to the asserted agent in trusted configuration;
3. author and reviewer are different agents with different public-key fingerprints; role labels alone are insufficient;
4. both verdicts are `approve`, and authoritative request-change state reports none open;
5. both attestations are within the consumer-supplied lifetime and neither nonce was previously consumed;
6. every required check is explicitly green; missing, stale, or unavailable status is invalid;
7. neither key is revoked for a new receipt.

The nonce check-and-consume operation must be one durable atomic transaction at a single designated merge/host gate. Other consumers verify the gate's authenticated durable consumption record rather than consuming the same nonce again. Missing records, restart ambiguity, or verifier outage fail closed. Exact ordering, trust root, anti-replay behavior and concurrency require implementation and adversarial tests.

## Key and revocation rules

Keys, agent-to-key bindings, and required-check names must be obtained from a trusted pre-candidate source (the existing trusted main state or separately pinned host configuration), never from the candidate being authorized. Candidate key-set changes require Josh's separately verifiable one-tap approval and cannot authorize themselves. Hosts hold pinned public keys only. Initial public fingerprints require Josh's one-time Command Center confirmation before they become trusted. A key-set change requires the ratified one-tap approval. Store immutable historical key/revocation metadata with archived receipts so historical verification can explain past evidence while new receipts reject revoked keys. Historical validity requires a trusted, durable consumer acceptance-time anchor; a timestamp signed only by a later-compromised agent is insufficient. Missing or unverifiable historical timing fails closed.

## Consumer boundary

The consumed commit and tree must match the attested commit and tree exactly. Proposed implementation: fast-forward the already reviewed candidate using a compare-and-swap against the observed main head; a merge or squash that creates a different commit requires fresh attestations for that exact resulting commit. This is an implementation proposal, not an assertion that the GitHub or host gate is already enforceable. The consumer independently recomputes the B2 manifest hash from the candidate tree and the evidence hash from authenticated exact-candidate check results, using a versioned canonical encoding; their format and provenance remain to be specified and tested.

Request-changes state must come from an authenticated, agent-bound source; GitHub identity alone is insufficient where agents share an account. No open request-changes and all required checks must be proven for the exact candidate; absent or unverifiable status fails closed.

CI and the host gate independently verify the envelope. Release builder, updater, manual release, rollback, and alternate deployment paths must refuse absent, invalid, expired, revoked, or unavailable receipt state. The direct-`main` detector/reverter is a separate required consumer and must block release.

## Remaining implementation work

No signing platform, trusted public-key configuration, durable nonce store, required-check source, request-change source, release builder, updater, or direct-`main` responder is wired here. Those integrations require exact implementation and adversarial evidence before B1 can merge.
