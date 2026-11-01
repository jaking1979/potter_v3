# A1 — Phase A safety/containment evidence baseline

Status: ACTIVE

This directory records exact-version evidence for the ratified Phase A / T5 Step-0 safety prerequisites. Evidence is not considered passing merely because a checklist item exists.

## Required evidence

- [ ] secrets scan and approved vault-path readiness
- [ ] Alpha Bot containment
- [ ] fenced scratch restore respecting the live kill state
- [ ] Home Assistant/watchdog checks
- [ ] page-scoped Notion negative-access test against forbidden roots
- [ ] seven healthy briefs

## Safety boundary

No A1 activity enables v3 production behavior. Until the T22 readiness token, all v3 runtime remains staging-only with no durable production writes or real external effects.

## Recording rule

For each item record: component/version or commit, timestamp, command/test ID, result, evidence artifact/link, and any failure or exception. Failures remain visible; do not convert an unverified item into PASS.
