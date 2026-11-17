## 2026-11-17 (later): Josh - do NOT blindly copy v2; rebuild each capability the v3 way
Design of record: docs/V3_DESIGN_FROM_V2.md (capability map, v2 failures designed out, build order). Composio key in the
export is fine per Josh (no re-export needed for it). Importer is a reading tool only. Built: schedule watchdog (overlay/tools/schedule_watch.py, 7 tests, proven on a live sandbox gateway), wired into
bootstrap + verify. Merrick diagnosis and design in the design doc. Step 1a done: potter-guardrails v3 plugin (overlay/plugins/potter-guardrails: hard wall with configurable ids,
docs-consult gate, config write guard; 6 node tests; loads in OpenClaw 2026.11 with before_tool_call registered),
installed by bootstrap, checked by verify_host (19 checks pass in sandbox). Plugin installs are now verified, not assumed.
Not proven: a live model turn being blocked (sandbox has no model key).
Host pre-flight (Josh's host session, read-only, 2026-11-17): PVE 9.2.3; vmbr0; local-lvm 18.9 GB free; ~10 GB RAM
available; IDs 100 (HAOS VM), 200, 210 (v2) in use; Debian 13 template present; v2 runs Tailscale in userspace mode.
Installer defaults now: first free ID from 211, hostname potter-v3, 12 GB disk, 4 GB RAM, local Debian 13 template,
space and memory checks, no edits to host container config, Tailscale userspace. No local Ollama on this host (RAM/disk).
Next: step 1b - rewrite Potter's rules tree (deduplicated) from v2's rules/ and AGENTS.md.

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
Done: cron_commands() translates inventory/cron.json (real export: 29 kept = 5 run at install, 16 standby, 8 off in v2;
41 expired one-shots and 6 built-ins skipped). Schema /2 export received @3951253 (mirrored tree, 3 agents, 2 plugins, 9 dependency dirs, units). It leaked a Composio
API key (tree/home/josh/.composio/user_data.json): docs/V2_EXPORT_FOLLOWUP2.md asks v2 to recreate the repo and add
inventory/models.json. Model decision (Josh): mirror v2's model setup incl. the Jev decider (lib/jev in the tree).
Next (after the /2 export): importer v2 reads the mirrored tree, recreates /home/josh paths, agents, plugins, services.

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
