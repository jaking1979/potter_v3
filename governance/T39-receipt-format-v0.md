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
2. each claim signature verifies against the configured key for that role;
3. author and reviewer public-key fingerprints differ;
4. both verdicts are `approve`, and authoritative request-change state reports none open;
5. both attestations are within the consumer-supplied lifetime and neither nonce was previously consumed;
6. every required check is explicitly green; missing, stale, or unavailable status is invalid;
7. neither key is revoked for a new receipt.

The nonce check-and-consume operation must be one durable atomic transaction. A restart or verifier outage is invalid, not an implicit pass.

## Key and revocation rules

Hosts hold pinned public keys only. Initial public fingerprints require Josh's one-time Command Center confirmation before they become trusted. A key-set change requires the ratified one-tap approval. Store immutable historical key/revocation metadata with archived receipts so historical verification can explain past evidence while new receipts reject revoked keys.

## Consumer boundary

CI and the host gate independently verify the envelope. Release builder, updater, manual release, rollback, and alternate deployment paths must refuse absent, invalid, expired, revoked, or unavailable receipt state. The direct-`main` detector/reverter is a separate required consumer and must block release.

## Remaining implementation work

No signing platform, trusted public-key configuration, durable nonce store, required-check source, request-change source, release builder, updater, or direct-`main` responder is wired here. Those integrations require exact implementation and adversarial evidence before B1 can merge.
