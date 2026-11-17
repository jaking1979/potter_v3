# Quality control

Your word that something is done is not evidence. Every night two programs check it, and check every schedule.

1. **Recorder** (00:30) lists what you said you did, from the day's session transcripts, into
   `state/qc/claims/<day>.jsonl`. It only makes the list.
2. **Verifier** (01:00) checks each claim against the world: the scheduler's job list, files in the workspace,
   recent commits, a read-only library listing. It never re-reads a transcript to confirm a claim, and it never
   changes what it checks. It also checks every enabled scheduled job: did the last run succeed, did it run when due.
3. **The owner hears about it only when something does not check out**: one short report with the unsupported
   claims and a "Schedules" section. Verified, uncheckable and future items are silent. Hourly rechecks are silent.
4. **When a report names your claim:** investigate, then either do the missed work or correct the record. Reply
   with what you fixed and the evidence. Never re-assert without evidence. Open items are in
   `state/qc/followup/<day>.jsonl`; a recheck closes them when the evidence appears.
5. **Make your claims checkable.** When you report work, name the file path or the job name. A claim that names
   nothing cannot be verified.
6. Output in the wrong language, or any reply you cannot explain, is an error: record it in `learnings/ERRORS.md`.
7. Do not disable, quiet or edit these jobs or their state files to make a report go away. Fix the cause.
