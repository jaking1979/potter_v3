---
name: t1-eval
description: The T1 evaluation of Potter v3 against v2 on the owner's own tasks (drafting the task set, running it, grading, the report). Use when the owner says "T1", "run the eval", "compare v3 and v2", or "how does v3 compare with v2 now".
---
# T1 evaluation

The owner does almost nothing: one reply to approve the tasks. You run v3 AND the v2 baseline yourself (an agent
built from v2's own files: its AGENTS.md, SOUL.md, rules, memory, prompts and the owner's preferences, as of the
import; live v2 is never contacted). Grading is Jev; actions are blocked during runs.

## 1. The task set (once; one message from the owner)
Draft 25 to 30 tasks from what the owner actually asks for (recent conversations, the compiling doc, briefs, the
knowledge graph), 3 to 5 per category: brief (today and tomorrow), calendar and logistics, email and follow-ups,
house, family and people, food and wine, money and tasks, local events, memory ("what do you know about..."),
judgment (a decision with trade-offs). Also 3 to 5 at the competitor bar (Instinct, Muse, Dots, Duet): web errands
("book dinner Friday for 4", "cancel the subscription I don't use"), travel, and background work that reports only
when it matters. In a run they stop before any committing click: grade the plan and how far it got.
Each task: `id`, `category`, `prompt` (the owner's own words), 2 to 4 `criteria` (checkable statements about
substance, never style), and `new_info`: true when the answer depends on something the owner told v3 after the
install (the v2 baseline gets those same messages as daily notes before every run, so both have the same facts;
the report scores the two groups separately). Aim for about a third `new_info` tasks.
Send ALL of them in ONE message, grouped by category, numbered, criteria in short form. End with:
"Reply 'ok', or 'drop 4 9', or 'change 12: <new wording>'." Apply the reply, write `state/t1/tasks.json` as
`{"tasks": [...]}`, and confirm in one line. Never add tasks the owner did not see.

## 2. Run, grade, report (no owner involvement)
Tell the owner it runs in the background (about a minute per task), then: `potter-t1 run`, `potter-t1 grade`,
`potter-t1 report`. Send the report as it is, then at most three lines: where v3 clearly beats v2, where it
loses, and the one fix that would move the score most.

## Optional: a real v2 answer
The baseline is v2's files with today's model, not live v2's runtime. If the owner wants to spot-check one task
against real v2, they forward v2's answer ("v2 answer brief1: ..."): save it to a temporary file, run
`potter-t1 v2-answer <id> <file>`, delete the file, re-grade. Never ask the owner to do this for every task.

## Re-runs
"Run T1 again" re-runs both systems on the same tasks, so progress over time is comparable. Never edit or
re-grade an answer by hand.
