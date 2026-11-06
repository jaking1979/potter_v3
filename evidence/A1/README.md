# A1 — Phase A safety/containment evidence baseline

Status: ACTIVE

This directory records exact-version evidence for the A1 subset of the ratified Phase A / T5 Step-0 safety prerequisites. Evidence is not considered passing merely because a checklist item exists. Fenced scratch restore and the page-scoped Notion negative-access test are A3 work after A2; vault setup/migration is A2.

## Required A1 merge evidence

- [ ] secrets scan clean, or findings inventoried for A2 remediation
- [ ] Alpha Bot containment negative test
- [ ] Home Assistant/watchdog last-success check
- [ ] seven healthy briefs

## Safe evidence collection

Evidence that touches live accounts or hosts is collected by the package owner on the live host under the existing v2 safety boundary. The repository receives only redacted logs, hashes, test metadata, or other minimum proof needed for verification. Never commit tokens, credentials, account numbers, message/brief contents, clinical material, or other unnecessary private content.

## Safety boundary

No A1 activity enables v3 production behavior. Until the T22 readiness token, all v3 runtime remains staging-only with no durable production writes or real external effects.

## Recording rule

For each item record: component/version or commit, timestamp, command/test ID, result, evidence artifact/link, and any failure or exception. Failures remain visible; do not convert an unverified item into PASS.
