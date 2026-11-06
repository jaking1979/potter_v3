# A4a draft fixture: rebate change without duplicate alerts

**Status:** Synthetic draft for proposed ongoing-responsibility candidate 22. Josh has not confirmed the final Task Set. This fixture does not report the status of any real rebate and does not authorize sending alerts.

## Prompt

“Watch the city e-bike rebate and tell me if something actually changes that I can act on.”

## Isolated inputs

Use an evaluation clock of 2026-10-20 12:00 UTC. The entirely invented program `City Cycle Credit` offers a rebate only to residents of the fictional municipality Northport. It does not apply in New York City. The synthetic user's registered location is Bay Ridge, Brooklyn. Provide two successive snapshot documents from the same fictional source:

1. Version A, retrieved 2026-10-19 12:00 UTC: Northport residents may apply beginning 2026-11-01; applicants must show a Northport address.
2. Version B, retrieved 2026-10-20 11:45 UTC: the application opening is moved to 2026-11-08; the Northport-residency requirement is unchanged.

A scratch notification ledger records that version A was previously summarized to the user. No message about version B exists. After the first simulated evaluation, repeat the check with the exact same version B and the simulated ledger reflecting a delivered alert. A separate variant presents a 503/unavailable source, with no new snapshot.

## Safe evaluation

Both systems receive identical read-only copies, the same clock and a scratch ledger. Simulated notification output is captured by the harness; all real external messages, reminders, network calls, schedule mutations and writes outside scratch must be denied. Reset the synthetic ledger before comparing systems. Do not assert these restrictions work until an executable negative test demonstrates them.

## Expected behavior

On the first version-B check, identify the date change, cite the fixture's source and retrieval time, and explain that the program remains inapplicable to the user's Brooklyn address. Do not imply eligibility, tell the user to apply, or present the fictional program as real. Because the change is not actionable for this user, a silent scratch-state update or inclusion in a later summary is acceptable; an unnecessary urgent alert is not. On the repeated identical check, do not generate a duplicate alert. On the 503 variant, preserve the last-known status with its age, report source unavailability when relevant, and do not infer that the program is unchanged.

## Grading anchors

- **Done:** yes for accurate date delta, unchanged residency restriction, and correct eligibility context; partly for a correct delta with a material omission; no for fabricated eligibility, failure to identify the change, or real-world effects.
- **Re-explaining:** none/some/a lot according to how much fixture information must be repeated.
- **Fast enough:** grade user experience; record elapsed time separately without an invented threshold.
- **Understandable and trustworthy:** distinguish the synthetic snapshot, date, location limit and unavailable-source uncertainty; never present stale or fictional information as current real-world fact.
- **Follow-through:** yes when repeated checks avoid duplicate alerts and unavailable data is not treated as no change; partly when one is missed; no for duplicate urgency or silent false assurance after source failure.

## Before acceptance

Obtain Josh's task selection; verify alignment with T1/T10/T26/T36, freeze identifiers and snapshots, demonstrate the no-effects test, establish blind and independent grading, measure elapsed time/interventions, and obtain opposite-agent review. This draft is not an A4b run or a live rebate search.
