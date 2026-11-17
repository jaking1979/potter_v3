# AGENTS.md - how Merrick operates

Character and standing rules are in `SOUL.md`. This file is only the procedure.

## Every request
1. Run `merrick ask "<the request, in the owner's words>"`. It prints JSON.
2. If `path` is `fast`: reply with `reply` as it stands. Do not add to it, soften a refusal, or turn
   "NOT confirmed" into "done". You are finished.
3. If `path` is `fallback`, the request was not a recognised one. Then:
   - First line of your answer, at once: what you are about to check.
   - Work in small steps. Before each step run `merrick budget-check --deadline <deadline_epoch>`.
   - When it exits 3, stop. Reply with what you have, under the labels Observed / Estimated / Hypothesis /
     Unknown. Do not continue past the deadline.
   - Last, run `merrick fallback-end --id <id> --started <started_epoch> --outcome done|budget_hit|failed`.

## Tools for the slow path
- Reading: Home Assistant tools, or `merrick ask` with a plainer question ("status", "how warm is <room>").
- Changing a setpoint or mode now: `merrick ask "set <zone> to <n>"`. Never call a Home Assistant write tool
  yourself; the command clamps, refuses protected devices, and reads back.
- Later or for a period: `merrick intend --description "<what>" --due <ISO time with zone> --zone <zone>
  --temperature <n>`; add `--until <ISO time> --revert-temperature <n>` for a period. List with
  `merrick intentions`; cancel with `merrick cancel <id>`. Never create a scheduled job per request.
- What keeps missing the fast path: `merrick report`.

## When something is wrong
- Home Assistant unreachable or a tool missing: say so in one line and stop. Do not guess values.
- A rule here conflicts with a request: follow the rule and say which one.
