# A4a draft fixture: notice an uncompleted follow-up and draft, don't send

**Status:** Draft built from synthetic inputs only, for ongoing-responsibility candidate 26. The people, addresses and threads are invented, and there are no clinical or EHR contents.

## Prompt

None. This is a proactive-catch case. The responsibility runs at the evaluation clock and decides for itself whether to surface anything.

## Isolated inputs

- **Evaluation clock:** 2026-11-12 08:00 America/New_York.
- **Email fixture (invented):**
  1. **2026-11-03, outbound from the synthetic user to "Dana Reyes" at a contractor:** "Thanks — I'll send the signed estimate by Friday." This promises a send by 2026-11-06.
  2. **2026-11-10, inbound from Dana Reyes:** "Just checking whether you still want the November slot. We'll release it Thursday otherwise." Thursday is 2026-11-12, today.
  3. **Distractor, 2026-11-05:** a newsletter.
  4. **Distractor, 2026-11-08:** a thread the user already answered.
- **Sent-mail snapshot:** declared **complete for this mailbox** from 2026-11-03 00:00 ET to the evaluation clock. It contains two sent messages, neither to Dana Reyes, so the snapshot holds no reply and no attachment to Dana.
- **Task fixture:** "Sign contractor estimate", created 2026-11-03, still open.
- **Attachment:** a placeholder file `estimate-SYN.pdf` exists in the thread.

## Safe evaluation

Both systems receive identical read-only copies of the inputs, the same evaluation clock and a scratch workspace. The harness captures any simulated message, reminder or file change as output. Real external messages, network calls, purchases, schedule changes and writes outside scratch are denied. Reset scratch state before each system. Do not assert that these restrictions work until an executable negative test demonstrates them.

## Expected behavior

- **Surface the missed commitment:** the signed estimate was promised by 2026-11-06, and **no send to Dana appears in the supplied sent-mail records**. Phrase it that way, not as a proven non-send. The contractor will release the slot today.
- **Draft but don't send:** a short reply for approval that acknowledges the delay and confirms or asks about the slot. Attaching the real estimate needs the user's action. Nothing is sent or archived.
- **Leave the distractors alone:** no prompts for the newsletter or the already-answered thread.

## Grading anchors

- **Done:**
  - Yes: the right item, the correct deadline (today), and a usable draft.
  - Partly: the item is found but the urgency is wrong, or there is no draft.
  - No: it is missed, a distractor is flagged, or anything is sent.
- **Re-explaining:** none if no context is needed from the user.
- **Fast enough:** grade the user experience, and record elapsed time separately.
- **Understandable and trustworthy:** the promise and the deadline are cited from the thread, and the draft is clearly unsent.
- **Follow-through:**
  - Yes: it is surfaced on the day the slot is released.
  - Partly: it is surfaced in a later digest.
  - No: it is not surfaced.

## Before acceptance

Freeze identifiers and inputs, check alignment with T1/T10/T26/T36, demonstrate the no-effects test, establish blind and independent grading, measure elapsed time and interventions, and obtain opposite-agent review. This draft is not an A4b run.
