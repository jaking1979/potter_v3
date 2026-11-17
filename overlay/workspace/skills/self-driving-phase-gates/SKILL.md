---
name: self-driving-phase-gates
description: Make a phased build advance itself or nudge the owner once a phase is proven ("so I don't forget", "build the next step automatically once it works"). Writes a roadmap, a gate state file and one weekly gate job.
---
# Self-driving phase gates

When the owner wants a phased build not to be forgotten, set up a gate: a roadmap, a state file and one weekly job
that either runs the next phase or sends one nudge. Read `rules/global/schedules.md` first; it governs the job.

1. `state/<project>-roadmap.md`: phases, each with a purpose, a spec a future session can run without this
   conversation (exact paths, commands, exit criteria) and a status (DONE / CURRENT / NEXT), plus rules that hold
   for every phase. At least one phase is DONE and the current gate is named.
2. `state/<project>-gate-state.json`: `{"phase", "gateNumber", "status": "waiting", "criteriaMet": false,
   "earliestAssessmentAt": "<ISO>", "counters": {}, "history": []}`. The observation window is agreed with the owner.
3. One weekly agent job, made with the schedules rule's settings (time zone, timeout, a named model, delivery to the
   owner). Its message holds the whole run logic: before `earliestAssessmentAt`, add a "skipped" history entry and
   output nothing; after it, check the exit criteria against evidence; if met, run the next phase's spec and update
   the roadmap and state; if not, send one short nudge with what is missing. Test it once with delivery off.
4. Tell the owner the schedule, the first real assessment date and what happens then.
5. For labels that may grow into categories: record each match once with its source; promote only past a volume
   threshold seen across several months; propose candidates for the owner's approval rather than creating them.
