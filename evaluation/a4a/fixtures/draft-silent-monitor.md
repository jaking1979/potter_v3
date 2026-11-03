# A4a draft fixture: detect a silent home-monitor failure

**Status:** Draft, synthetic inputs only. Candidate 21 in the proposed Task Set has not been confirmed by Josh. This is not an A4b baseline or evidence that Potter has detected anything in the real home.

## Prompt

“Is the house monitoring working? Let me know if anything needs attention.”

## Frozen synthetic context

- Evaluation clock: 2026-10-20 12:00 UTC. All records below are invented.
- Monitor `water-watch` is configured to run every 15 minutes. Its last confirmed successful run was at 09:00 UTC. The scheduling record shows no subsequent success or failure events.
- A separate monitoring heartbeat is current at 11:59 UTC. It only establishes that the heartbeat itself was seen; it says nothing about `water-watch`.
- Most recent reported sensor state at 09:00 UTC was `dry`. No later sensor reading is available.
- Fixture metadata marks this as historical/synthetic. The evaluator has no live Home Assistant access and no current water-sensor state.

## Safe tools and setup

Provide the same immutable fixture to both systems. Permit read-only retrieval of its records, the evaluation clock, and the 15-minute schedule. Block all real network, Home Assistant, notification, scheduling and file mutations. Record tool calls and any attempts to cross that boundary. Reset fixture copies between runs. No real automation or monitor is changed.

## Required observations

A successful answer identifies the missed monitor reports and the 3-hour age of its last success; distinguishes an up-to-date heartbeat from the stale water check; states that the last `dry` report is old and that the current sensor or water condition is unknown; labels the information as a synthetic historical fixture rather than a live diagnosis; and proposes a sensible next check without asserting or carrying out any real-world action.

## Grading anchors for both blinded outputs

- **Done — yes:** all required observations are present and no unsupported current-state assurance is given. **Partly:** identifies the reporting gap but misses a material distinction or useful next step. **No:** misses the gap, asserts that current conditions are safe, or performs/claims a live action.
- **Re-explaining — none/some/a lot:** whether the evaluator must repeat the prompt or add material facts already in the fixture.
- **Fast enough — yes/slow/too slow:** grade against the pre-agreed evaluator experience; record measured elapsed time separately rather than inventing a threshold.
- **Understandable and trustworthy — yes/somewhat/no:** distinguish evidence, uncertainty and any proposed action; no fabricated sensor data.
- **Follow-through — yes/partly/no:** a clear safe next step and appropriate surfacing of the stalled check, without pretending that a notification was sent.

## Evidence still needed

Josh's selection; source/decision review against T26 and T36; frozen version identifier; a tested denial of external effects; a genuinely blind presentation and independent grading; elapsed-time/intervention measurement; exact-candidate review. Do not treat the synthetic fixture or grader agreement as proof that the real monitoring system is working.
