# STATE (150 lines max: package, head SHA, status, open blocking findings)

Branch: `v3-core`, created 2026-11-16 from `claude/integration-staging` @ f611f4b.
CI is the only test authority. Nothing merges to main. One audit per integrated SHA.
A finding blocks only if it breaks a ratified invariant on Linux / Python 3.12; everything else is one backlog line.

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
- 2026-11-16: merged claude/mvp-shadow-runtime (32cc58c); wired C1 PolicyEngine + durable JSONL policy trace into the turn loop (12 tests, mutations killed). Context Governor v0 built (18 tests). Telegram front door wired (11 tests). Morning Brief built (9 tests). Finding 1 (trace-outage retry) concerns approval-bound effects; the shadow install has none (chat.turn needs no approval), so it is backlog, not blocking. Next: runner process, home status.
- 2026-11-16: finding 3 (dispatch mark) fixed; C2 schema c2.v4; 13 tests; all non-equivalent mutations killed.
- 2026-11-16: finding 2 (trace join fields) fixed; 12 tests, 15 of 15 mutations killed after one known-answer test was added.
