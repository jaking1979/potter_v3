# Assumed host profile

**Why this exists.** The Catskills OptiPlex is offline (power loss, AC recovery
off) and may stay off for up to about two weeks. Josh directed on 2026-11-06 that we build
ahead on estimates and correct once real details arrive.

**Rules**
- `assumed_host.json` is the only place host facts live. Code reads them through
  `host.profile`, never as scattered constants.
- Each fact is `reported` (dated source), `assumed` (a guess), or `verified`
  (checked on the real host, which only the post-restore check may set).
- All host contact goes through adapters. Everything else runs against simulations.
- Nothing deploys, touches real accounts or Alpha Bot, or counts as A1, T5 or
  T39 evidence because of this file.

**When the host is back:** collect the observed values read-only, run
`host.profile.diff()` and `unobserved()`, fix the adapters, and mark facts
`verified` in a reviewed commit.
