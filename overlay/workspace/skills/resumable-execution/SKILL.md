---
name: resumable-execution
description: Keep multi-step goals resumable across restarts, model changes and lost sessions with GOAL / PLAN / PROGRESS / DECISIONS files and checkpoints. Use for any build, migration, audit or project longer than one step.
---
# Resumable execution

Any goal with several stages, likely to outlive this session, or costly to repeat keeps its state in
`projects/<slug>/` so the next session (or another agent) continues instead of starting over.

**Files.**
- `GOAL.md`: the outcome asked for, scope, limits, authority, definition of done. Do not reinterpret it; record
  approved changes.
- `PLAN.md`: numbered, bounded work units, each with result, dependencies, how it is validated, and status.
- `PROGRESS.md`: the handoff: status, units done with evidence, unit in progress, the exact next action, files and
  settings changed, commands run, errors, blockers, decisions waiting on the owner.
- `DECISIONS.md` (when there are any): decision, why, alternatives, when to revisit. Not an activity log.

**Start or resume.** Find the project folder, read all four files, check the real system still matches them, then
continue from the recorded next action. Never redo finished work unless checking shows it is missing or broken.
New project: write the files before substantial work.

**After each unit:** validate it, record what changed and the result, mark it done in `PLAN.md`, write the next action
in `PROGRESS.md`. Do not wait for the end.

**Checkpoint before:** restarting anything (gateway, service, container), switching model or agent, anything that may
cut the connection, nearing a time or context limit, and stopping on a blocker. After a significant error, record
the error, what was tried, whether anything partly changed, and the safest next step; the same unit failing twice
becomes a documented blocker, not a loop.

**Done** only when the definition of done is met, the final state is validated, limits are written down, and
`PROGRESS.md` says complete. A session or job ending is not completion.
