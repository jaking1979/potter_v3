---
name: t1-eval
description: The T1 evaluation of Potter v3 against v2 on the owner's own tasks (drafting the task set with the owner, running it, collecting v2's answers, grading, the report). Use when the owner says "T1", "run the eval", "compare v3 and v2", or forwards a v2 answer for the eval.
---
# T1 evaluation

The task set is the owner's, approved once; grading is Jev, one narrow question per criterion; actions are
blocked during runs (potter-core blocks everything but reading in `t1-eval-` sessions).

## 1. Draft the task set (once, with the owner)
Draft 25 to 30 tasks from what the owner actually asks for: look at recent conversations, the compiling doc,
the morning briefs and the knowledge graph. Spread them over these categories, 3 to 5 each: brief (today and
tomorrow), calendar and logistics, email and follow-ups, house, family and people, food and wine, money and
tasks, local events, memory ("what do you know about..."), judgment (a decision with trade-offs).
Each task: `id` (short), `category`, `prompt` (the owner's own words, as they would ask), and 2 to 4 `criteria`:
checkable statements a good answer meets ("names the Friday 6:30 soccer game", "says the brief source it could
not read"). Criteria about substance, never about style.
Send the list to the owner in batches of 10 and apply their edits. Only after they approve: write
`state/t1/tasks.json` as `{"tasks": [...]}`. Never add tasks they did not approve.

## 2. Run against v3
`potter-t1 run` (about 30 seconds per task; tell the owner it runs in the background and you will report). Then
`potter-t1 grade`, then `potter-t1 report` and send the report.

## 3. v2's answers (the owner collects them; you never contact v2)
`potter-t1 v2-sheet` prints the numbered prompts: send them to the owner to ask v2 one by one, in v2's chat.
When the owner forwards a v2 answer ("v2 answer brief1: ..."), save the text to a temporary file and run
`potter-t1 v2-answer <id> <file>`, then delete the file. After the last one: `potter-t1 grade` and
`potter-t1 report`.

## Reporting
Send the report as it is, then at most three lines: where v3 clearly beats v2, where it loses, and the one fix
that would move the score most. Never edit or re-grade an answer by hand.
