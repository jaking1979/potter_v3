---
name: kg-eval
description: Measure how well the decision model (Jev) recognises the links in a knowledge graph. Use when the owner asks to run the KG eval, the Jev eval, or to test the graph's connection checks.
---
# KG eval

1. Run (one command; it reads the graph read-only and makes about 520 decision calls, under $1):
   `potter-kg-eval --db <graph file>`
   The graph file is the one the owner names. If they name none: `/root/v2-kg/potter-kg.sqlite` when it exists
   (the copy of v2's graph), else the live graph (`potter-kg` plugin config `dbPath`).
   Optional: `--pairs 200 --negatives 60 --max-usd 1`.
2. Reply with the printed summary exactly, then one line comparing to v2's published result
   (recall 68%, precision 100%, predicate top-1 68%, top-3 86%), saying which arm did better.
3. If it exits 3 (no decider) or 4 (every call failed), say so in one line and include the error. Do not retry
   more than once.

The full per-pair results file is root-only; do not paste it into chat. It names people in the graph.
