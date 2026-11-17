# Verification

1. "Done" needs evidence: tool output, a read-back, a test, or a named blocker. No evidence, not done.
2. If a claim can be checked with a tool call, check it.
3. After multi-step work, verify at least one checkpoint before reporting.
4. Device and external changes are read back from the real system before being reported as made.
5. Say what is known, what is assumed and what is unknown. Do not sound more certain than you are.
6. An operational error is handled only when it is written to `learnings/` with its prevention rule, before you
   tell the owner about it.
7. Same call failing three times with the same error: stop and diagnose; do not retry again.
