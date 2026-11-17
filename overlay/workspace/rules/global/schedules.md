# Schedules

These exist because jobs that silently did not run were the old system's biggest failure.

1. **Work that must happen is a command job**, not an agent turn that is asked to remember. Use an agent turn only
   when judgment is the point (a brief, a digest).
2. Every job has: an explicit time zone, exact firing, an explicit timeout, and for agent turns a named model and
   the tools it needs. Nothing relies on a session's defaults.
   An agent-turn job that needs a connected app (calendar, tasks, mail, Notion) must be created with its tools
   allowed explicitly and then run once to prove it can reach them; the old system's scheduled turns silently had
   no access to connected apps.
3. **Recurring needs get one recurring job.** Never create a new one-off job per day.
4. A job that must reach the owner fails if delivery fails. A job with nothing to say says nothing.
5. One-off jobs remove themselves after running. Period-bound jobs carry their own end date.
6. Create and change jobs only with OpenClaw's own commands, after reading its docs for that payload kind.
7. After creating or changing a job: list it, confirm the next run time, and run it once where that is safe.
8. The schedule watchdog reports every failed, missed or undelivered job. Do not silence it; fix the job.
