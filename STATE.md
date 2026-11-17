# STATE (150 lines max: package, head SHA, status, open blocking findings)

Branch: `v3-core`, created 2026-11-16 from `claude/integration-staging` @ f611f4b.
CI is the only test authority. Nothing merges to main. One audit per integrated SHA.
A finding blocks only if it breaks a ratified invariant on Linux / Python 3.12; everything else is one backlog line.

> Source of truth for scope: docs/SCOPE.md. docs/BUILD_PLAN.md is the long-range menu only.

## 2026-11-17 reset (Josh): v3 = everything v2 does, no lockdown
Unlocked overlay (full toolset, v2's rules as behavior), v2 export importer (install/import_v2.py), setup-form upload,
settings page for secrets (install/settings_page.py), handover skill, docs/V2_PARITY.md, docs/V2_EXPORT_PROMPT.md.
v2 export received (jaking1979/potter-v2-export @d5362bc, read-only): 76 crons (27 enabled; 45 are shell-command jobs at
real /home/josh paths), 5 MCP servers, 20 app folders, 3 agents (main, potter-local, merrick), 2 own plugins
(potter-guardrails, potter-kg), primary model local Ollama. Problems: a live Telegram token in history; apps/ regrouping
broke command paths (my prompt's fault). Follow-up for v2: docs/V2_EXPORT_FOLLOWUP.md (schema /2, mirrored tree).
Next: importer v2 reads inventory/cron.json + mirrored tree, recreates /home/josh paths, agents, plugins, services.

## Current direction (Josh, 2026-11-16) - supersedes the standalone-service plan
Build on a FRESH OpenClaw in its own new Proxmox container (never v2's infrastructure). Reuse
existing open-source pieces; do not reinvent. Our deliverable is configuration + overlay + installer:
- openclaw.json template: Telegram locked to Josh's chat, read-only tool profile (allow read; deny
  write/edit/apply_patch/exec/process/browser/gateway/cron/sessions_*), sandbox on, model key capped at $50.
- Workspace overlay: personality files, Morning Brief skill (adapted from v2 morning-brief-v1-skill, read-only
  parts) on an 8:30 ET weekday cron.
- Existing read-only connectors for Calendar, Home Assistant, Gmail, Notion; read-only enforced by provider
  scopes/tokens, not only tool policy.
- Installer: wraps OpenClaw's own install + onboard, applies overlay, prompts for each value, runs verify_host.
- Python shadow runtime (shadow/, policy, tracing, runtime) stays as the harness-independent core for later;
  it is NOT in the Nov 27 install path. No more custom transports.
Built and proven against a real OpenClaw 2026.11.4 in a sandbox profile (2026-11-16):
install/{install,bootstrap,verify_host,uninstall,lib}.sh, overlay/ (locked config, Potter workspace, morning-brief skill).
bootstrap -> verify_host: 14/14 PASS. Proxmox container creation is written but NOT yet run on a real Proxmox host.
Added (Josh: no terminal; Composio/AgentMail MCPs; OptiPlex on Tailscale with funnel): browser setup form
(install/setup_form.py), Composio + AgentMail as mcp.servers, Tailscale join + funnel, password auth, connect-apps skill,
approval-gated config changes from chat (docs/CHAT_SETUP.md). Tests: 10 form + 9 installer, full gate green.
Added 2026-11-17: skills why-and-cost, home-status, inbox-triage; Home Assistant (state-only tool) and voice (OpenAI
realtime) fields in the form; docs/RUNBOOK.md. Sandbox install with every option: verify_host 16/16, all 5 skills ready.
Next: Potter UI branding + Today
dashboard, voice (talk mode) config, connectors, phone, payments, memory. See docs/BUILD_PLAN.md.

## Gate on this branch
- `tools/ci_gate.py tests`: exact head, full history, Python 3.12; refuses skips, zero tests, hard exits, forged tails.
- `tools/nodeploy_guard.py .`: refuses deploy commands, write/secret workflows, secret shapes, private addresses and real-host/credential/Alpha Bot references outside docs.

## Packages (all in f611f4b unless noted; audit = Audit 1 recorded / Audit 2 on the frozen SHA)
| Package | Where | Status | Blocking findings |
| --- | --- | --- | --- |
| C0 contracts | contracts/ | present | none known |
| C1 PolicyEngine | policy/ | present, dispatch mark added | none known |
| C2 task store / effect journal | runtime/tasks.py, effects.py | present | none known |
| C3 outbox | runtime/outbox.py | present | none known |
| C4 fencing (single node) | runtime/fencing.py | present; two-node/Litestream deferred | none known |
| C5 identity grants | identity/ | present | none known |
| C6 trace | tracing/ (trace, emitter, policy adapter) | present, join fields added; no SQLite sink | trace-outage retry |
| E1 approvals / notifier | notifier/ | present | none known |
| E2 responsibility runner | responsibilities/ | present | none known |
| E3 ingress (Telegram only for MVP) | ingress/ | present; NOT yet wired to the turn loop | none known |
| No-effect gate (shadow/gate.py) | shadow/ | merged from claude/mvp-shadow-runtime @32cc58c | none known |
| ModelBroker + cost log (shadow/models.py, costlog.py, model_broker/) | model_broker/, shadow/ | merged from claude/mvp-shadow-runtime | none known |
| Turn loop + C1 policy wiring + durable trace | shadow/turn.py, policy.py, tracefile.py | wired; trace outage denies the turn | none known |
| Telegram front door (shadow/telegram.py: E3 ingress + E1 auth) | shadow/ | wired; only principal_direct drives a turn; ack after handle, recover() replays | real Telegram transport verifier/poller not built |
| Runner loop (shadow/runner.py) | shadow/ | built: recover on start, poll, front door, brief tick; per-step isolation, capped backoff, codes-only logs | Telegram HTTP poller/sender not built |
| Family Calendar read (shadow/calendar.py) | shadow/ | built: read-only tool bound to one calendar id and an 8-day window, cleaned/capped text, untrusted provenance; real Google transport + readonly OAuth scope not built | none known |
| Morning Brief (shadow/brief.py) | shadow/ | built: weekday tick, once/day, durable state, system_policy provenance, v2's 7 formats chosen in code (missing state = unpopulated, never quiet), no external content | no scheduler process yet; state/sources (home, inbox, Notion) come later; v2's Todoist, weather and Google Calendar are outside the allowlist |
| Context Governor v0 (shadow/governor.py) | shadow/ | built: persona + last exchanges, hard budget, no retrieval | none known |
| Install / verify / uninstall scripts | - | not started | - |

## Open blocking findings (fix Nov 17-18)
1. Trace outage with a consumed approval: the task is retried three times, escalates, and the approved action is stranded; there is no recovery.
2. ~~Trace rows lack join fields~~ FIXED in the head below: schema v1 rows carry action hash, effect id, capability and reason; the digest commits to them.
3. ~~Unaudited-allow dispatch~~ FIXED in the head below: schema c2.v4 adds a dispatch mark; an approval-bound effect cannot dispatch until its allow is durably traced, and reconciliation writes a catch-up allow row first.

## Backlog (one line each)
- Two-node fencing, Litestream, signed releases, updater: deferred until after install.
- E3 inbox migration and bounded recovery: present; revisit only if a blocking invariant is shown.

## Log
- 2026-11-16: v3-core created; CI gate and no-deploy guard added; MVP.md written.
- 2026-11-16: merged claude/mvp-shadow-runtime (32cc58c); wired C1 PolicyEngine + durable JSONL policy trace into the turn loop (12 tests, mutations killed). Context Governor v0 built (18 tests). Telegram front door wired (11 tests). Morning Brief built (9 tests). Finding 1 (trace-outage retry) concerns approval-bound effects; the shadow install has none (chat.turn needs no approval), so it is backlog, not blocking. Runner loop built. Next: Telegram HTTP transport (poller + reply tool), Google Calendar transport, then home status.
- 2026-11-16: finding 3 (dispatch mark) fixed; C2 schema c2.v4; 13 tests; all non-equivalent mutations killed.
- 2026-11-16: finding 2 (trace join fields) fixed; 12 tests, 15 of 15 mutations killed after one known-answer test was added.
