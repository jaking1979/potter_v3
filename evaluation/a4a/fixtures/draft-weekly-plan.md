# A4a draft fixture: weekly plan from calendar, task and email fixtures

**Status:** Draft built from synthetic inputs only, for one-shot candidate 19. The people, events and messages are invented, with no clinical content.

## Prompt

"Plan my week. Flag anything that clashes."

## Isolated inputs

- **Evaluation clock:** Sunday 2026-11-15 18:00 America/New_York.
- **Calendar:**
  - Mon 09:00–10:00 standing meeting.
  - Tue 15:00–16:00 dentist (Brooklyn).
  - Tue 15:30–16:30 "School pickup swap — confirm?" (tentative).
  - Wed 12:00–13:00 lunch, Manhattan.
  - Thu 18:00 dinner, 4 people.
  - Fri all day "Conference (virtual)".
- **Tasks:**
  - "Submit expense report" (due Wed).
  - "Order saffron" (due Fri).
  - "Call plumber" (no date).
- **Email:**
  - A 2026-11-14 message from a synthetic colleague: "Can we move Monday's meeting to Friday 10:00?" Unanswered.
  - A 2026-11-13 message: "Thursday dinner is now 5 people."

## Safe evaluation

Both systems receive identical read-only copies of the inputs, the same evaluation clock and a scratch workspace. The harness captures any simulated message, reminder or file change as output. Real external messages, network calls, purchases, schedule changes and writes outside scratch are denied. Reset scratch state before each system. Do not assert that these restrictions work until an executable negative test demonstrates them.

## Expected behavior

- **Conflicts to flag:**
  - The Tuesday dentist overlaps the tentative pickup swap.
  - The proposed move of the Monday meeting to Friday 10:00 **overlaps** the all-day virtual conference. Present it as an overlap to resolve, not as a required decline: an all-day virtual event doesn't necessarily mean continuous attendance.
- **Updates to note:** Thursday dinner is now 5 people.
- **Deadline-aware placement:** the expense report before Wednesday and the saffron by Friday.
- **Suggestions, not facts:** a free slot for the plumber call, and the dinner reservation update to 5 people, are labelled as suggestions pending the user's confirmation.
- **Drafts only:** replies (decline or counter the meeting move, confirm the pickup swap) as drafts. No calendar changes are made.

## Grading anchors

- **Done:**
  - Yes: both conflicts are found, the dinner update is applied, and the deadlines are respected.
  - Partly: one conflict or update is missed.
  - No: two or more are missed, or the calendar is changed.
- **Re-explaining:** none if everything comes from the fixtures.
- **Fast enough:** grade the user experience, and record elapsed time separately.
- **Understandable and trustworthy:** each conflict cites its two sources.

## Before acceptance

Freeze identifiers and inputs, check alignment with T1/T10/T26/T36, demonstrate the no-effects test, establish blind and independent grading, measure elapsed time and interventions, and obtain opposite-agent review. This draft is not an A4b run.
