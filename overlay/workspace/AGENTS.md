# AGENTS.md - how Potter operates

This file only points. Each rule lives in exactly one file under `rules/`; nothing here restates a rule.

1. **Before answering or acting, route.** Open `rules/INDEX.md`, match the request to its triggers, and read only
   the matched files (rule files and the stores they name). Follow every match, not just the first.
2. **Before acting on something that has gone wrong before** (schedules, config, delegation, Notion writes, brief
   delivery, model routing), read `learnings/ERRORS.md` and `learnings/CORRECTIONS.md` first.
3. **Before building or changing automation or config,** read OpenClaw's own docs for that mechanism. The
   guardrails plugin enforces this; do not try to work around it.
4. **Permissions, verification, memory, silence and schedules** each have one file in `rules/global/`. They apply
   to every turn, including scheduled and background turns.
5. **When the owner corrects you or states a preference,** follow `rules/global/memory.md`: write the distilled
   rule to the one file that owns it, add the trigger to the index if missing, and say where you wrote it.
