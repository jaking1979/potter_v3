---
name: bench
description: PotterBench, the fixed benchmark (a fictional household, 30 tasks with answer keys, v3 against the flat-file design). Use for "run the benchmark", "how does v3 score", "benchmark results", "leaderboard", "set up the benchmark".
---
# PotterBench

Nothing real is involved: the household is fictional, both agents are sealed to their own folders.

- "Set up the benchmark" (once, and after a big update): `potter-bench setup`. It builds both agents, seeds v3's
  separate graph and runs v3's capture pipeline over the household's messages, then restarts the gateway. Report
  its last line verbatim.
- "Run the benchmark": tell the owner it takes about 30 to 60 minutes in the background, then `potter-bench run`,
  `potter-bench grade`, `potter-bench report`. Send the report verbatim, then at most three lines: where v3 wins,
  where it loses (name the task ids), and the single fix that would move the score most.
- "Leaderboard": `potter-bench leaderboard`, verbatim.
- One task's answers: `potter-bench report --json` and quote that task's two answers and grades.
Never edit answers or grades. If a run fails part-way, `potter-bench run --run <id>` resumes it.
