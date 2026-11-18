---
name: house-insights
description: Merrick's analysis of the house data: what it supports (efficiency, comfort, maintenance), and automation ideas, but only once there is enough data. Weekly on Saturday, and on request ("what has the house data shown", "any house automation ideas", "make that automation").
---
# House insights

The rule: no idea without enough evidence. Every detector says what data it needs (for example 5 heating runs
with the outdoors below 25 F) and stays silent until it has it.

## 1. What the data supports
Run `merrick insights --json` (the evidence pack) and `merrick insights` (its plain text).
- `ready`: findings with their numbers. `waiting`: what is still missing and how far along it is.

## 2. Ideas no detector covers (only if `days_of_data` is at least 14)
Think as an energy and home-automation analyst. From the evidence pack ONLY, propose up to 5 ideas: an
automation, a schedule change, a maintenance check, or a question worth measuring. Each:
`{"title": "...", "what": "<what would change, concretely>", "cites": "<the exact numbers from the pack>"}`.
No idea may rest on conditions the data has not seen (if the coldest reading is 31 F, nothing about colder
weather). Never anything with locks, alarms, garage doors or water valves.
Then read `state/house-ideas.jsonl` (earlier ideas with their status) and the existing Home Assistant
automations (home-assistant tools, read-only), and pipe
`{"evidence": <pack>, "existing": [<titles of those>], "ideas": [<yours>]}` into `potter-house-ideas`.
Only its `kept` ideas go further.

## 3. Report (at most 3 items, each with its numbers)
- The strongest `ready` findings and `kept` ideas, each: what, the evidence, the expected effect (in heating
  hours or degrees; dollars only if an energy sensor exists).
- One line of progress: what is still waiting for data ("preheat timing below 25 F: 1 of 5 runs").
Append each item you report to `state/house-ideas.jsonl` as
`{"date": ..., "title": ..., "status": "proposed", "cites": ...}`.

## 4. "Do it" (the owner adopts one)
Draft the Home Assistant automation (YAML) and show it to the owner with what it will do and when. Only after
the owner approves: create it with the home-assistant tools. Mark it `adopted` in `state/house-ideas.jsonl`, and
four weeks later compare the same numbers before and after and tell the owner whether it worked. "No" marks it
`rejected` (never proposed again). During the dry-run (before the house switch-over) nothing is created: the
owner gets the YAML to apply through the old assistant or by hand.
