# A4a — Task Set and grading scaffold

Status: IN PROGRESS. Package A4a (L15); decisions T1, T9, T33, T36. Owner Dolores, reviewer Claude. A4a is offline only. A4b baselines require A1; A5 waits on A4b. This scaffold is not completion evidence.

The draft set and grading prototype live in the canonical Notion Build Execution Plan. Josh approved the proposed direction of 20 one-shot and seven ongoing cases, including the rain-jacket substitution. Final fixture selection, source alignment, anchors and freeze remain outstanding; these drafts are not final benchmark tasks. T1 requires 25–30 real tasks from the preceding 60 days, including 18–22 one-shot and 6–8 ongoing-responsibility cases. Cover all T36 classes, T33 behavior, the T27 memory ablation, T29 handoff, and the T26 silent-failure case.

## Case fixture contract (draft)

For every case record a stable ID and version; source and date; user prompt; scenario type and capability classes; allowed inputs and frozen fixture references; expected outcomes and uncertainty; safety limits; grading anchors; and deterministic setup and cleanup instructions. Real source materials and outputs stay outside the repo when sensitive. Do not put clinical content, EHR data, secrets, message contents, or production credentials in fixtures. Unavailable facts remain unknown.

Run all evaluations against snapshots or scratch environments. The eval flag must deny real consequential effects. Transactions stop at the approval boundary; no real send, purchase, scheduling, trade, deletion, or device change. This scaffold does not authorize or start baseline runs.

## Blind grading

For each task show outputs A and B with the same context. For each output ask: got it done (yes/partly/no); re-explaining needed (none/some/a lot); fast enough (yes/slow/too slow); understandable and trustworthy (yes/somewhat/no). Responsibility cases add follow-through (yes/partly/no). Optional comment. Preserve the separate system-to-label mapping away from the grader. Record time, cost, and interventions automatically; target at most 60 seconds of Josh's grading time per task. Retain outputs until graded, then apply T9's bounded retention and privacy rules.

## L17 reporting boundary

The current case-agreement helper is a preparation aid only; it does not calculate the full L17 report or any acceptance result. When the future report applies L17's more-than-10-percent invalidation rule, it must count the union of distinct affected case IDs, not per-dimension affected entries. It must also retain the fixed prespecified denominator, intervals, flips, prevalence, and separate independent-grader evidence. The agreed prevalence clarification is in `test-retest-protocol-draft.md`: usable cases only, with planned N and exclusions shown; invalid exercises are not scored. The synthetic helper cannot verify protected output alignment or exercise validity.

## Evidence still required

- Complete final fixture selection and source alignment; freeze versioned, representative fixtures and success anchors.
- Complete a usable prebuilt Notion grading form and verify blind presentation and access boundaries.
- Demonstrate automatic timing, cost and intervention collection without raw sensitive data.
- Give independent graders the same fixed outputs and anchors; record reproducibility, disagreements, duration, and any refinements without inventing an acceptance threshold.
- Request Claude's review of the exact A4a candidate under the agreed T1 review process.
- Do not call A4a complete or run A4b until the relevant evidence and dependencies pass.
