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
| C1 PolicyEngine | policy/ | present | unaudited-allow dispatch (below) |
| C2 task store / effect journal | runtime/tasks.py, effects.py | present | none known |
| C3 outbox | runtime/outbox.py | present | none known |
| C4 fencing (single node) | runtime/fencing.py | present; two-node/Litestream deferred | none known |
| C5 identity grants | identity/ | present | none known |
| C6 trace | tracing/ (trace, emitter, policy adapter) | present, join fields added; no SQLite sink | trace-outage retry |
| E1 approvals / notifier | notifier/ | present | none known |
| E2 responsibility runner | responsibilities/ | present | none known |
| E3 ingress (Telegram only for MVP) | ingress/ | present | none known |
| No-effect adapter | - | not started | - |
| ModelBroker (ceilings + JSONL cost log) | - | not started | - |
| Context Governor v0 | - | not started | - |
| Install / verify / uninstall scripts | - | not started | - |

## Open blocking findings (fix Nov 17-18)
1. Trace outage with a consumed approval: the task is retried three times, escalates, and the approved action is stranded; there is no recovery.
2. ~~Trace rows lack join fields~~ FIXED in the head below: schema v1 rows carry action hash, effect id, capability and reason; the digest commits to them.
3. Unaudited-allow dispatch: an approved intent can dispatch under a reconcile record when its original allow trace was never persisted. Add the dispatch mark.

## Backlog (one line each)
- Two-node fencing, Litestream, signed releases, updater: deferred until after install.
- E3 inbox migration and bounded recovery: present; revisit only if a blocking invariant is shown.

## Log
- 2026-11-16: v3-core created; CI gate and no-deploy guard added; MVP.md written.
- 2026-11-16: finding 2 (trace join fields) fixed; 12 tests, 15 of 15 mutations killed after one known-answer test was added.
