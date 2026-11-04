# A4a draft fixture: alert only when a match is tomorrow (New York time)

**Status:** Draft built from synthetic inputs only, for ongoing-responsibility candidate 25. The fixture list is invented and is not a real Chelsea schedule.

## Prompt

Standing instruction recorded earlier: "Tell me the day before a Chelsea match. Nothing otherwise."

## Isolated inputs

A synthetic fixture list. All kickoff times are given in UTC, as a typical feed would give them:

| ID | Opponent (invented) | Kickoff (UTC) |
|---|---|---|
| F1 | Northport Rovers | 2026-11-06 00:30 |
| F2 | Eastvale Town | 2026-11-08 15:00 |
| F3 | Westmere United | 2026-11-08 03:30 |

The harness runs the responsibility at three evaluation clocks, each in America/New_York (EST, UTC−5, after the 2026-11-01 DST change):
- **Run A:** 2026-11-04 18:00
- **Run B:** 2026-11-06 09:00
- **Run C:** 2026-11-07 09:00

**Traps:**
- F1 is on 2026-11-06 in UTC but 2026-11-05 at 19:30 in New York.
- F3 is on 2026-11-08 in UTC but 2026-11-07 at 22:30 in New York.
- F2 is a plain case: 2026-11-08 at 10:00 in New York.

A scratch ledger records the alerts already given, and it persists across the runs.

## Safe evaluation

Both systems receive identical read-only copies of the inputs, the same evaluation clock and a scratch workspace. The harness captures any simulated message, reminder or file change as output. Real external messages, network calls, purchases, schedule changes and writes outside scratch are denied. Reset scratch state before each system. Do not assert that these restrictions work until an executable negative test demonstrates them.

## Expected behavior

- **Run A:** a single alert for F1, because it is tomorrow evening in New York, at 19:30 ET.
- **Run B:** a single alert for F3, because it is tomorrow evening in New York, at 22:30 ET. No alert for F2, which is two days away in New York.
- **Run C:** a single alert for F2, because it is tomorrow in New York, at 10:00 ET. No duplicate alert for F3, which was already alerted and is now tonight.
- **Times:** every alert states the kickoff time in New York time.
- **Otherwise silent:** no alerts or messages beyond these.

## Grading anchors

- **Done:**
  - Yes: all three runs alert exactly correctly.
  - Partly: one run is wrong.
  - No: two or more runs are wrong, or the system computes the day in UTC throughout.
- **Re-explaining:** none if the standing instruction is honoured without being restated.
- **Fast enough:** grade the user experience, and record elapsed time separately.
- **Understandable and trustworthy:** times are given in ET, and the opponent and date are correct.
- **Follow-through:**
  - Yes: there are no duplicate or extra alerts.
  - No: a duplicate or a "nothing today" message is sent.

## Before acceptance

Freeze identifiers and inputs, check alignment with T1/T10/T26/T36, demonstrate the no-effects test, establish blind and independent grading, measure elapsed time and interventions, and obtain opposite-agent review. This draft is not an A4b run.
