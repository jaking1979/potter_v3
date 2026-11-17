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

## Not yet (v3.3+)
Model broker per turn (`before_model_resolve`), tool-surface narrowing (`toolsAllow`, needs the host's tool list),
memory staging, durable tasks and the Notion outbox.
