# Potter v3 core: v3.1 Control and v3.2 Brain

The v3 layer on top of the harness, as one OpenClaw plugin (`overlay/plugins/potter-core`). All logic is in `lib/`
(pure modules, no harness imports, tested with `node --test core.test.mjs`); `index.js` only connects it to the
harness's hooks, so another harness needs only a new `index.js`.

## v3.1 Control (T11/T12; Decision Log 16-17, 58-62, 76)

Every tool call is classified (`lib/classify.js`) and decided (`lib/control.js`) before it runs.

| | owner asked directly | scheduled job | anyone else / unproven |
|---|---|---|---|
| read | allow | allow | allow |
| reversible (workspace files, Potter's own stores, messages to the owner) | allow + receipt | allow + receipt | allow + receipt, marked untrusted |
| consequential (send to others, change an app, a schedule, the config, a device) | allow + receipt | allow if a standing grant covers it, else **owner approval** | **owner approval** |
| consequential, high (money, trades, deletions, locks, the alarm) | **owner approval** | **owner approval** | **owner approval** |
| forbidden (locks, alarms, valves through a tool; the hard wall in potter-guardrails) | block | block | block |

- Trust comes only from what the host proves (`ctx.requester.senderIsOwner`, `ctx.trigger`); missing means unproven.
- Approvals use OpenClaw's own plugin approvals: the run pauses and the owner answers `/approve` (allow once / deny),
  15 minutes by default. Nothing runs before the answer.
- Standing grants (`plugins.entries.potter-core.config.standingGrants`, e.g. `"mcp:composio:GMAIL_*"`) let a
  scheduled job do a consequential action without asking; never a high-severity one. "Take over X" adds them.
- Receipts: `<state>/potter-core/receipts.jsonl`, one line before (intent, decision) and one after (result) each
  change, hash-chained (`verify()` finds an edited or removed line), no arguments, secrets masked. `/receipts` shows the
  last ones and whether the chain is intact.
- `mode: "shadow"` records what it would have done and never blocks or asks.

## v3.2 Brain (00 Brain Architecture: System One / Context Governor)

- **Fast path** (`before_agent_reply`, `lib/fastpath.js`): a recognised house request is answered by Merrick's
  deterministic router with verified Home Assistant calls, without the main model (about 2 seconds). Anything
  Merrick does not recognise continues to the normal turn. `confirm departure` runs only for a proven owner.
- **Context Governor** (`before_prompt_build`, `lib/governor.js`): the request is matched to domains (a table first;
  Jev only when the table finds nothing, 2.5 s cap) and the model gets a short plan: rules to read, skills that fit,
  the connected apps that matter. A failure means no plan, never a blocked turn.
- **Merrick + Jev** (`overlay/jev/merrick-decide.mjs`): requests Merrick's table does not recognise get one Jev
  decision (intent and zone, confidence floor 0.7) before any slow reasoning.
- Telemetry, content-free: `<state>/potter-core/governor.jsonl` (domains, source, latency; fast path intent, outcome).

## v3.3 Model broker (T28/T13)

`before_model_resolve` (`lib/broker.js`): for each conversation turn Potter picks the model class from the model
policy: `chat` by default, `escalation` (DeepSeek V4 Pro) when the request asks for depth (trade-offs, strategy,
root cause, "think hard"), is very long, or is a money decision. Scheduled jobs keep the model they name.
`llm_output` writes a content-free spend ledger (`<state>/potter-core/spend.jsonl`: class, model, tokens, cost);
over the daily escalation budget (`broker.escalationDailyCapUsd`, default $3, and at most `escalationDailyMax`
escalations a day) turns stay on chat. `/receipts` shows today's model spend. `broker.mode: "shadow"` logs the
choice without applying it.

## v3.4 Memory and responsibilities (T15/T27, T16; Decision Log 11-15, 35-36, 51-53, 63-66)

- Staged memory (`lib/memory.js`): `memory_stage` proposes a fact, preference, pattern or correction with
  confidence, source and examples; nothing is written to `MEMORY.md` until the owner's review. The weekly job
  `potter-memory-review` (Sundays) sends the list; `memory_review_apply` (owner-only) writes approved items with
  provenance; a correction replaces the line it corrects.
- Responsibilities: `responsibility_track` (cadence, next due, checklist), `responsibility_due`,
  `responsibility_done`, in `state/responsibilities.json`; briefs and check-ins list what is due.

## v3.5 Durability (T10, T4/T17)

- Once-only effects (`lib/durable.js`): each consequential call has a fingerprint (tool + arguments, never stored
  themselves). The same call that already succeeded in the last 7 days asks the owner before running again.
- In doubt: a consequential call with no recorded result after 10 minutes (a crash or restart in between) is listed
  in `/receipts` to check.
- Notion outbox: a Notion write that fails is kept (root-only) and retried every two hours by
  `potter-notion-outbox`; a retry is the same decision and is not asked again; after 6 failures it is given up and
  the owner is told once.

## v3.6 Brain v1: working context (00 Brain Architecture; Context Governor)

Before the main model thinks, the governor builds its working context instead of telling it "go read X"
(`lib/context.js`, wired in `before_prompt_build`):
- Recall: a local BM25 index over the owner's own files (MEMORY.md, preferences, rules, learnings, memory),
  skipping files the harness injects every turn, machinery (global rules, skills, state) and v2 reference copies.
  Keyword-list sections never match; code and tool syntax rank low; a named person in the request boosts passages
  that mention them. No network, a few milliseconds; rebuilt only when a file changes (checked every 30 s).
- Rules: the full text of the global rules the request's domains need.
- State: local date and time, responsibilities due in 3 days, proposed memories waiting, today's working notes.
- A hard budget (default 7000 characters); a passage that does not fit is left out, never cut. Nothing relevant: only
  the state line. A failure means no working context, never a blocked turn.
- Logged per turn in governor.jsonl: `context_chars`, `recalled` (paths), `build_ms`.
- Config: `brain.enabled`, `brain.budget`, `timezone` (from `agents.defaults.userTimezone`).

## v3.7 Brain: the knowledge graph on every request (KG + Jev)

- Read path (`lib/kgrecall.js`, run beside the plan in `before_prompt_build`, so it adds no wait): the graph file
  is opened read-only and reloaded when it changes; names and aliases in the request are matched (longest first;
  "I"/"my" adds the owner); their description and direct facts, strongest first, become candidates; one Jev request
  asks one narrow yes/no per candidate ("does this fact help with this request?"); what passes is injected, with
  medium/low-confidence facts marked "(unconfirmed)". Without Jev: high-confidence facts only.
  Logged: `kg_entities`, `kg_considered`, `kg_kept`, `kg_source`, `kg_ms`.
- Eval (`potter-kg-eval`, 2026-11-18, v2's graph, 260 pairs, $0.05): both designs recall 70% of existing links;
  recall is 87% on high-confidence links and 47% on medium ones, so the misses sit in weak links. v2's yes/no kept
  100% precision; the structured link-type Choice beat v2's flat one (58.6% vs 55%). Per-predicate yes/no ranking
  was worse (44%) and is not used. The connection check now uses v2's yes/no plus the structured Choice.
- 3.7.1: the file search's shortlist goes into the same Jev request; passages Jev judges irrelevant are dropped
  (re-ranking). Logged: `files_considered`, `files_kept`.
- `potter-kg-import`: adds another graph (v2's copy) to the live one; never overwrites; tags the source; skips
  clinical material.

## v3.8 The graph learns (KG write path; potter-kg 3.2.0, potter-core 3.8.0)

- potter-core queues each message the OWNER writes (sender matched to the owner ids; jobs, heartbeats and other
  senders never) to a root-only queue. Logged per turn: `kg_queued`, `sender_known`.
- `potter-kg-capture` (every 20 minutes) per message: one Jev screen (lasting fact? kind: durable / preference /
  situational / one-off? the owner's own words? clinical?), stop unless worth remembering; a cheap model extracts
  facts in the controlled vocabulary with an exact source quote (a quote not in the message is discarded); per fact
  one Jev request (supported by the quote? structured link-type Choice + "any fits"? replaces an existing fact?);
  entities matched by exact name first, near matches by a 3-level Jev Score; code routes: write (high if the owner
  said it, else medium), stage for the weekly review, or drop. A move (lives_at, located_in, runs_on) archives the
  old link. Provenance keeps the quote, the date, the kind and any "until" date.
- New predicates: prefers, avoids, responsible_for.
- The weekly memory review lists staged graph facts; approve writes them at high confidence.
- `capability-map` (daily): TOOLS.md gets a live map of reachable tools and Composio-connected apps.
- `potter-mail-watch` (command job, every 30 minutes): lists the shared inbox through AgentMail's API without
  changing it while v2 owns it; Jev flags what needs the owner (one yes/no per message, 20 per request); those become
  dated Open Loops for the brief. Replaced the agent-turn `mail-observe`, which timed out reading mail into the model.

## v3.9 Staleness (potter-kg 3.3.0, potter-core 3.9.0)

- `potter-kg-freshness` (daily 03:37): facts whose "until" date has passed are archived (never deleted).
  "Still true?" questions, at most five a week, never re-asked within 60 days, chosen in code: temporary facts
  older than 90 days, preferences older than a year, and facts Potter actually used in the last 30 days that Jev
  judges time-bound (imported v2 facts carry no kind, so Jev decides). Facts confirmed in the last 180 days are left.
- The weekly memory review shows them; keep = confirmed at high confidence; archive = gone; "what changed" is a new
  fact through the capture path.
- Recall never injects an expired fact and marks old temporary ones "(as of <date>; may have changed)". Kept facts
  are logged to `kg-usage.jsonl` so freshness knows what is used.
- Broker log fields renamed: `escalation_today_usd` (what the cap counts), `escalation_cap_usd`, `day_total_usd`.

## v3.10 Chief-of-staff loop (overlay/cos, potter-kg 3.3.0 vocabulary)

- Morning: the brief commits to a forecast (the day's shape and 2-4 checkable predictions: must-dos expected done,
  items expected to slip) and writes `state/forecasts/<date>.json`; one "Forecast:" line in the brief, citing
  patterns from the graph.
- Evening: the check-in adds what actually happened (facts, the owner's words) and the day's real shape. At most
  one question about the forecast; never graded aloud.
- `potter-cos-score` (nightly 23:07): Jev judges each prediction against the evening record (came true? mentioned?
  not mentioned = unknown, never a miss). Code turns outcomes into patterns with evidence and confidence: items
  that keep slipping, weekdays that run packed or light, and the calibration of Potter's own forecasts. Patterns
  are `pattern:*` entities linked to the owner by `has_pattern`; recall brings them to every relevant turn;
  a pattern that loses its evidence is archived.
- `week-review` (Sunday 18:17): the week against the forecasts, what kept slipping with a fix to choose, the
  patterns, forecast accuracy, one change for next week. Says so when there is not enough evidence.
- New vocabulary: entity type `pattern`, predicate `has_pattern`.

## v3.11 Vault (potter-vault 1.0.0)

- The owner chooses what Potter may use: items in a "Potter" collection of a self-hosted Vaultwarden, read by
  Potter's own account (server-enforced scope). Setup: docs/VAULT_SETUP.md; `potter-vault-setup` serves a one-time
  form that checks the key and password and writes a root-only file.
- A value never enters the model's context: the model writes `{{vault:<item>#<field>}}`; the plugin substitutes the
  value into the tool call at the last moment, only for browser and request tools (shells, messages, files, memory,
  Notion and email are blocked), only for items authorized in the last 15 minutes. Values are scrubbed from tool
  results and the model's own messages before they are stored. `vault_send_to_owner` sends a value straight to the
  owner's chat.
- Approval: cards always; logins unless the item's notes say `potter:auto`. Audit: names and purposes, never values.
- Checked in a real OpenClaw 2026.11: the plugin loads with before_tool_call (priority 10, after potter-core's
  control), tool_result_persist and before_message_write.
- Updates reinstall a plugin only when its files changed (each install runs OpenClaw and npm; memory).

## House dry-run and switch-over

- `merrick shadow` (every 30 minutes, read-only): what v3 would hold each zone at (the away hold, only when vacant)
  and whether it would alert, next to the setpoints the house actually has (the old assistant's decisions).
  `merrick shadow-report --days 7`: agreement while vacant, every divergence as a merged stretch, alerts v3 would
  have sent. The `house-cutover` skill runs the switch-over in order: report, the owner lists the old assistant's
  house plans, the owner stops the old assistant, plans re-created with `merrick intend`, tick switched on.

## T1 evaluation (overlay/eval)

- The owner's own 25-30 tasks (drafted from real requests by the `t1-eval` skill, approved by the owner), each with
  2-4 checkable criteria. `potter-t1 run` asks v3 each one in an isolated `t1-eval-` session, where potter-core
  blocks everything but reading. v2's answers are collected by the owner (`v2-sheet`, `v2-answer`); v2 is never
  contacted. `grade`: one Jev request per answer, one yes/no per criterion plus "dodges" and "invented specifics",
  composed in code (0-100, pass at 70). `report`: averages, per category, head to head.

## Not yet
Tool-surface narrowing (`toolsAllow`, needs the host's tool list), the Mac as a paired node, durable multi-step
task execution beyond the resumable-execution files.
