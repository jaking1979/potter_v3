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
space and memory checks, no edits to host container config, Tailscale userspace. Potter Local lives on the MacBook M1 (Josh): Ollama there over Tailscale (form field OLLAMA_URL); Mac = desktop
workspace, to be paired as an OpenClaw node. Mac is always on and Ollama listens on the LAN (address given by Josh in chat; entered on the form, not stored here).
Not built: the Mac node pairing step.
Step 1b DONE: machinery rules rewritten generically (overlay/workspace: AGENTS.md pointer-only, rules/INDEX.md,
rules/global/{permissions,communication,memory,retrieval,verification,schedules}.md, learnings/, state/compiling-doc.md).
Personal rules are built at install by install/profile_from_v2.py from a schema /2 export (26 active files, 133
reference files on Josh's real export; wall id found, redacted, and added to guardrails). Full sandbox install with the
real export: health check passes. Step 2 DONE (email): AgentMail channel for owner + co-owner conversations (standby until handover); quarantined
mail-intake skill + job (every 10m, isolated, no delivery, standby); overlay/tools/urgent_deliver.py (model-free,
quiet hours, marks delivered only after a successful send; 8 tests); rules/global/email-pipeline.md. Sandbox: jobs
created as designed, failed send leaves item queued and exits 1. Not proven: real mail, real Telegram send.
Step 3 DONE: morning-brief skill with v2's real inputs (family calendar, task reconciliation, weather, compiling doc,
archive+reset that KEEPS state signals; unknown state is never Quiet); evening-checkin skill (shutdown ritual + silent
capture); overlay/tools/evening_nudge.py = ONE recurring command job (*/10 15-21 weekdays), last work session + 20 min
via Composio CLI if present, else 18:00; 7 tests; sandbox install passes. Not proven: a real brief or check-in with a model.
Step 4 DONE: Composio by API-key header, Notion pre-registered (OAuth from chat), job tool rule.
Steps 5-9 CODE DONE, NOT WIRED (built by parallel builders, tests pass, not yet reviewed line by line by the main session):
- Merrick (overlay/merrick, overlay/workspace-merrick). Data-driven away hold and confirmed departure added (owner,
  2026-11-17). WIRED: bootstrap writes ha.env (600), the merrick wrapper, the merrick agent (Potter may delegate to it), the
  house file from the v2 export (install/house_from_v2.py), cold-watch + collect (on) and tick (standby). Sandbox install
  with the real export passes the health check; merrick ask runs end to end (no real Home Assistant in the sandbox).
- KG plugin (overlay/plugins/potter-kg, 51 node tests) + Jev layer (overlay/jev, 24 node tests; policy has placeholders).
- Monitors (overlay/tools/monitor_lib.py + monitors/, 45 tests) + 10 skills. QC (overlay/tools/qc, 39 tests).
- Trading pre-checks (overlay/tools/trading_precheck.py, 29 tests) + trading-run skill. No trading logic ported.
- notifier/lint.py now skips overlay/ (owner's decision, 2026-11-17).
Wiring notes exist only for merrick and qc-finance (docs/wiring/). Three builders stopped at the usage limit.
WIRED (2026-11-17): install/jobs.json + install/add_jobs.py create 19 jobs (KG curator, QC x3 on; monitors, digests,
trading, monthly scoring in standby); potter-kg plugin installed and loaded; model policy, monitor config and a blank
mandate placed. Sandbox install with the real export: 25 Potter jobs (10 on, 15 standby), health check passes; KG
curator, QC recorder/recheck run clean; trading gate refuses the blank mandate as designed.
MONITORS (fe6227b): install/monitors_from_v2.py builds state/monitors/config.json from the export (7 city, 17 local,
1 wine, 9 deal feeds on the real export); bootstrap takes a quiet first reading, then the 4 fetch jobs run; digests
stay standby. Gaps: 3 businesses list no website, 2 deal blogs have no known feed; city pages report changes from run 2.
REVIEW (51cdd1b + this commit): monitors had one shared state folder (fixed: one per skill); seen page lines aged
out (fixed); QC verified claims on one shared word (now two); 'lock up' refused (now starts the departure check).
MODELS: policy.json from ratified T28 + v2's production models; installer replaced openrouter/auto; KG curator now
calls Jev through a root-only key file. Known trading-gate limits: per_order approval is the agent's flag, and the
capital limit is per run, not cumulative (trading stays standby).
LIVE (2026-11-17): installed on the real host (CT 211); 20/20 health checks; tailnet-only; DeepSeek V4.1 Flash.
v3 CORE (docs/V3_CORE.md): potter-core plugin = v3.1 Control (classify/decide/approve, hash-chained receipts,
/receipts) + v3.2 Brain (Context Governor plan, Merrick fast path without the model, Merrick+Jev). Sandbox: plugin
loads with 4 hooks + command; house request answered by the fast path in 1.9 s with no model; governor plan logged.
LIVE v3.1/v3.2 on CT 211 (2d2b68b): fast path, governor + Jev, receipts, board 3/3, memory search 168/168 (ZDR model).
v3.3-3.5 (docs/V3_CORE.md): broker + spend ledger, staged memory + weekly review + responsibilities, once-only
effects + in-doubt list + Notion outbox. Sandbox: 6 hooks, 8 tools, 2 new jobs, no duplicates, fast path 2 s.
v3.6 Brain v1 (docs/V3_CORE.md): working context assembled per request (local recall + domain rules + state,
7000-char budget). 7 node tests; recall checked against the real v2 export (wine, events, family, house, trading).
KG + Jev (docs/V3_CORE.md v3.7): eval run live on v2's graph (copy at /root/v2-kg on CT 211, 1,254 entities,
3,815 relations); graph read on every request; importer. v3.8 (KG write path, capability map, mail-observe) and v3.9 (staleness) built. NEXT: deploy; check kg_queued/sender_known; read-path eval;
staleness; house dry-run then cutover after Thanksgiving; chief-of-staff loop; T1 eval vs v2.

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
