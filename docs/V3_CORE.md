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

## Not yet
Tool-surface narrowing (`toolsAllow`, needs the host's tool list), the Mac as a paired node, durable multi-step
task execution beyond the resumable-execution files.
