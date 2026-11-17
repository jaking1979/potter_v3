# Potter v3, designed from what v2 does (2026-11-17)

Josh's rule: the v2 export is the picture of what Potter should DO. It is not copied over. Each capability is
rebuilt the v3 way (OpenClaw-native first, existing plugins second, our code last), and v2's known failures are
designed out. Source: `jaking1979/potter-v2-export` (schema 2), read-only.

## Design rules for v3
1. **Native before custom.** If current OpenClaw or an official plugin does it, use that and delete v2's script.
2. **Deterministic things are not prompts.** Schedules that must happen are command jobs or hooks, not agent turns
   that "should remember to". Guardrails are plugin hooks, not prose.
3. **One source per fact.** v2's propagation map exists because the same fact lived in many files. v3 keeps one
   file per fact and generates the rest.
4. **No secret in any file.** v2 had literals in scripts. v3 uses the settings page and OpenClaw secret references.
5. **Config changes only through validated patches.** Hand-edits took v2's gateway down twice.
6. **Silence is a result.** A check that finds nothing or fails says nothing in chat; failures go to the log and QC.
7. **Every job proves it ran.** A job that silently died for weeks (the evening nudge) must be impossible: QC checks
   last-run status of every schedule daily.

## Capability map
| What Potter does (from v2) | How v2 did it | What went wrong in v2 (its own logs) | v3 design |
|---|---|---|---|
| Chief of Staff chat (Telegram, web, voice) | OpenClaw 2026.8, main session | non-English output once; answered from memory instead of its own docs | OpenClaw 2026.11, Telegram + Potter web app + talk mode; rules index kept as the routing layer |
| Operating rules, routing by trigger | AGENTS.md + rules/INDEX + global/domains/projects/people | rules not reliably followed; mirrors drifted | keep the rule tree (rewritten, deduplicated); hard rules move to hooks (row below) |
| Hard guardrails (EHR wall, Gmail-send lock, no-send-unseen, listener silence, docs-consult gate) | `potter-guardrails` plugin | plugin API is from 2026.8 | port to the current plugin hook API; test each gate; this is the one place we block |
| Morning Brief 8:30 ET, state-adaptive | cron -> main session -> prompt + compiling doc | ran before state was captured ("quiet" mislabel); cron turns lacked MCP tools | brief skill (built) + compiling doc; "no state" is its own format; job bundles its MCP tools explicitly |
| Evening check-in + state capture | scheduler script creating one-shot crons from calendar free/busy | 41 dead one-shots; nudge silently dead for weeks; leaked NO_REPLY | ONE recurring job that computes the time itself; QC watches it |
| Email: own inbox, real-time processing, urgent surfacing | custom WebSocket listener service + urgent-mail-deliver cron | repeated leaks of plumbing into chat | official AgentMail plugin channel (durable queue, allowlist); urgent rule as a skill; both scripts retired |
| Personal Gmail, calendar, Todoist, Notion | Composio MCP, Notion MCP | cron turns could not reach them; Notion CLI writes hung | Composio + Notion MCP at gateway level, bundled into jobs; Notion writes stay ask-first |
| Capture pipeline (links -> Reference Library + wiki), newsletters/events scoring | rules/domains/capture, preferences, wiki | - | capture skill + bundled `memory-wiki`; preferences files kept |
| Knowledge graph + Jev curator (6-hourly) | `potter-kg` plugin + command cron | plugin API age | port plugin; curator stays a command job |
| Jev decider / model routing | lib/jev + router; three conflicting defaults on record | drift between config, memory and session override | one model policy file; Jev decides within it; needs v2's live settings (`inventory/models.json`, asked) |
| Home: Merrick (thermal learning, cold-weather watcher, HA control) | separate agent + command crons + HA MCP | HA YAML mistakes; funnel confusion | Merrick agent kept; collectors as command jobs; HA via MCP with its token in settings |
| Finance: Alpha Bot daily run, Robinhood MCP, financial monitor + Tuesday digest, LLM monthly scoring | agent-turn crons + scripts | - | same jobs, standby until handover; mandate file is the authority |
| Monitors and digests (Brooklyn events, Margaretville, weekend plans, wine price, Eames reply, CMC:Coach weekly) | crons + scripts | timeouts; some dead | one "monitor" pattern: command job fetches, skill judges, silent unless actionable |
| Quality control (recorder, verifier, hourly recheck) | python + crons | - | kept; extended to check every schedule's last run |
| Local/offline tier (`potter-local`, Ollama) | second agent on a local model | - | kept if the new container gets Ollama (needs RAM; decide at install) |
| Docs-consult gate, OpenClaw docs index | generated index + rule | skipped twice | regenerate the index for 2026.11 at install; gate as a hook |
| Guest Potter, NotebookLM, board meeting, meal plan skills | skills/state | partly planned only | carried as skills; not wired to schedules until asked |

## What is NOT carried
41 expired one-shot schedules, heartbeat/skill-review built-ins (OpenClaw makes its own), SwarmVault and Hermes
leftovers, `.bak` files, the retired claw-auto-router, hard-coded tokens and paths.

## Build order
1. Rules + identity (rewritten) and the guardrails plugin port.  2. Email on the AgentMail plugin.
3. Brief + evening check-in + compiling doc.  4. Connections and job tool bundles.  5. Merrick + home jobs.
6. KG + Jev + model policy.  7. Monitors/digests.  8. Finance (standby).  9. QC over everything.
Each step ends with the health check passing and a line in STATE.md.

## The importer's role now
`install/import_v2.py` is no longer the way v3 is built. Its cron translation and validation stay as tools for
reading the export; nothing is copied blindly.
