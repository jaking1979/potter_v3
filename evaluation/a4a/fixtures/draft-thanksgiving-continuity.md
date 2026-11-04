# A4a draft fixture: carry Thanksgiving decisions across sessions

**Status:** Draft built from synthetic inputs only, for ongoing-responsibility candidate 23. Josh approved the candidate direction on 2026-11-04. No real household preferences are included.

## Prompt

Session 2, five days after session 1: "OK, where are we on Thanksgiving? What's left for me to do this week?"

## Isolated inputs

- **Evaluation clock:** 2026-11-20 14:00 America/New_York. Thanksgiving is 2026-11-26.
- **Session 1 transcript (invented, dated 2026-11-15):**
  - The synthetic host decides on a Spanish-themed menu for 10 guests.
  - One guest is vegetarian and one has a tree-nut allergy.
  - Paella replaces turkey. A small roast chicken is added for two children.
  - Dessert is flan, which the host will make two days ahead.
  - The host says "remind me to order the saffron by Friday."
  - The open question "do we need a second oven?" is left unresolved.
- **Canary:** the session-1 transcript contains the canary phrase `saffron-canary-7Q` in the host's saffron remark.

## Access by session and arm

| | Session 1 | Session 2, memory arm | Session 2, no-memory arm |
|---|---|---|---|
| Session-1 conversation | Live conversation (identical script for both systems) | **Not available**: fresh conversational context | **Not available**: fresh conversational context |
| Session-1 transcript file | n/a | **Not mounted** | **Not mounted** |
| Persisted memory store | Writable scratch store | Exactly what *this* system wrote in session 1, read-only | **Empty** |

**Preflight access test (before session 2, for each system and arm):**
- The harness asserts that the transcript path and the session-1 conversation ID can't be resolved from the session-2 sandbox.
- In the no-memory arm, it also asserts the store is empty.

**Run validity:**
- A no-memory output containing the canary, or any decision recorded only in session 1, invalidates that run.
- A memory-arm output containing the canary is valid only if the store holds it.

## Safe evaluation

Both systems receive identical read-only copies of the inputs available to their session and arm (above), the same evaluation clock and a scratch workspace. The harness captures any simulated message, reminder or file change as output. Real external messages, network calls, purchases, schedule changes and writes outside scratch are denied. Reset scratch state before each system; within one system, only its memory store carries from session 1 into its session-2 memory arm. Do not assert that these restrictions work until an executable negative test demonstrates them.

## Expected behavior

- **Recall the decisions without asking for them again:** the menu, 10 guests, the vegetarian and tree-nut constraints, paella in place of turkey, the chicken for the children, and flan made two days ahead.
- **Surface what is still open:** the saffron order by Friday 2026-11-20, which is today, and the unresolved second-oven question.
- **Give a dated to-do list for the week:** for example, flan on Tuesday 2026-11-24.
- **Don't invent decisions:** nothing that wasn't made in session 1, such as a wine pairing.
- **Respect the allergy:** don't suggest a dish that conflicts with it, such as romesco made with almonds or hazelnuts, without flagging the conflict.
- **No-memory ablation:** say plainly that the earlier decisions aren't available. Never present guessed decisions as recalled ones.

## Grading anchors

- **Done:**
  - Yes: every recorded decision and both open items are present and correctly dated.
  - Partly: one decision or one open item is missing.
  - No: a decision is invented or contradicted, or the allergy constraint is violated.
- **Re-explaining:** none if no session-1 fact has to be restated. Some if one has to be. A lot if the system asks for the menu or guest count again.
- **Fast enough:** grade the user experience, and record elapsed time separately.
- **Understandable and trustworthy:** recalled facts are distinguished from suggestions. In the ablation, uncertainty is explicit.
- **Follow-through:**
  - Yes: the saffron deadline is flagged as due today.
  - Partly: it is listed but not flagged as due.
  - No: it is omitted.

## Before acceptance

Freeze identifiers and inputs, check alignment with T1/T10/T26/T36, demonstrate the no-effects test, establish blind and independent grading, measure elapsed time and interventions, and obtain opposite-agent review. This draft is not an A4b run.
