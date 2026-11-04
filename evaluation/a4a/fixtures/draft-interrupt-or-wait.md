# A4a draft fixture: interrupt now or wait for the brief

**Status:** Draft built from synthetic inputs only, for ongoing-responsibility candidate 27. Its companion is candidate 21. No real home state is included.

## Prompt

None. The responsibility runs at each evaluation clock and must decide to either interrupt now or hold the finding for the next scheduled brief, and say why.

## Isolated inputs

- **Morning brief:** scheduled daily at 08:30 America/New_York.
- **Variant 1:**
  - Clock: 2026-11-12 14:00 ET.
  - Monitor `attic-humidity` last succeeded at 11:00 ET, on a 1-hour cadence. It is 3 hours stale, which is past 2× its cadence.
  - The latest reading was normal, and nothing safety-critical depends on it.
- **Variant 2:**
  - Clock: 2026-11-12 14:00 ET.
  - Monitor `basement-water` last succeeded at 11:00 ET, on a 15-minute cadence.
  - The latest reading, at 10:45 ET, rose toward the configured leak threshold. The fixture labels it safety-relevant.
- **Variant 3:**
  - Clock: 2026-11-13 07:30 ET.
  - Same as variant 1, but the brief is due in one hour.
- **Instruction on file:** "Only interrupt me for things that can't wait."

## Safe evaluation

Both systems receive identical read-only copies of the inputs, the same evaluation clock and a scratch workspace. The harness captures any simulated message, reminder or file change as output. Real external messages, network calls, purchases, schedule changes and writes outside scratch are denied. Reset scratch state before each system. Do not assert that these restrictions work until an executable negative test demonstrates them.

## Expected behavior

- **Variant 1:** hold the finding for the next brief. Give the reason: not safety-relevant, and nothing degrades by waiting. Record it for the brief's health line.
- **Variant 2:** interrupt now. Give the reason: the water monitor is stale while its last reading was approaching the leak threshold, so the risk can't wait. State the uncertainty: "I can't see current readings."
- **Variant 3:** hold the finding for the brief due in one hour.
- **Every variant:** never claim current state from stale data.

## Grading anchors

- **Done:**
  - Yes: all three decisions are correct, with reasons.
  - Partly: one is wrong.
  - No: variant 2 is held, or the system claims the house is fine.
- **Re-explaining:** none if the standing instruction is honoured.
- **Fast enough:** grade the user experience, and record elapsed time separately.
- **Understandable and trustworthy:** the reason given is about consequences, and stale data is labelled with its age.
- **Follow-through:**
  - Yes: the held items appear in the next brief.
  - No: they are dropped.

## Before acceptance

Freeze identifiers and inputs, check alignment with T1/T10/T26/T36, demonstrate the no-effects test, establish blind and independent grading, measure elapsed time and interventions, and obtain opposite-agent review. This draft is not an A4b run.
