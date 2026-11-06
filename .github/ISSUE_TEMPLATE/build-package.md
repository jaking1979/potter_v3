---
name: Build package
about: Track a Potter v3 implementation package against canonical decisions and evidence
title: "[PACKAGE] "
labels: ""
assignees: ""
---

## Package contract

- Package ID:
- Objective:
- Governing decision IDs:
- Required RT / acceptance-test IDs:
- Risk tier: T0 / T1 / T2
- Dependencies:
- Interfaces/contracts:
- Owner:
- Opposite reviewer:
- Permitted model tier:

## Evidence required before merge

- [ ] Exact candidate commit recorded.
- [ ] Required automated/local tests pass.
- [ ] Opposite-agent review completed for the exact candidate.
- [ ] Required safety/failure-mode evidence attached.
- [ ] Migration/rollback evidence attached if applicable.

## Evidence required before release

- [ ] Required staging/soak evidence attached.
- [ ] Required release/Task Set evidence attached.
- [ ] T29 receipt/handoff recorded.

## Authority check

- [ ] No package dependency is being bypassed.
- [ ] No ratified contract is being changed by implementation.
- [ ] No clinical EHR access is introduced.
- [ ] Repository/issue/PR content is treated as untrusted input.
