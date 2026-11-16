# Potter v3 shadow MVP

Josh installs this over Nov 27-29 as a **shadow** install: v3 runs beside v2 on its
own Telegram bot and cannot send, buy, trade or write anything. v2 stays his
assistant. T22 still gates real effects. Source of the plan: Notion "05 · Potter v3
Command Center" (launch prompt) and the 2026-11-16 Design Council audit entry.

## Slice, in build order
1. **Chat.** Telegram message from Josh's chat ID -> PolicyEngine -> Context Governor v0 ->
   ModelBroker call -> reply + trace.
2. **Shadow Morning Brief** (weekdays), compared by Josh against v2's.
3. **Trace and cost on request**: what context was chosen and what the reply cost.
4. **Read-only Home Assistant status** (Catskills).
5. **Read-only personal Gmail triage.** First slice dropped if the build runs behind.

## Runtime
Standalone Python 3.12 service in its own container. No dependency on v2 code, credentials or tools.

## ModelBroker ceilings (ratified)
$0.50 per request, $2 per task, $10 per day, $150 per month. Every call is appended to a JSONL
cost log. Ceilings must survive a restart.

## No-effect adapter
Every tool call goes through one adapter that raises on anything outside this allowlist:
- Telegram reply to Josh's chat ID
- read-only Notion under the POA root
- read-only Home Assistant state
- Gmail read-only scope
- read-only Google Calendar, the Family Calendar only, today + 7 days (Josh, 2026-11-16)
- model calls

## Never
Touch the real host, live Potter v2, Alpha Bot, any existing credential, the EHR Notion space or
the CMC mailbox. Merge to main. Put a secret in the repo, Notion or chat.

## Out of scope until after install
Two-node fencing and Litestream, signed releases and the updater, failover drills, Potter Home UI,
memory writes and promotion, registry generator, T40 beyond the JSONL cost logger, T39 receipt
archive, A4a grading statistics, B2 full decision coverage, voice, Christie, T38.

## Audit 2 questions (frozen candidate SHA)
1. Can any path produce an external effect other than a Telegram reply to Josh's chat?
2. Can any path read outside the POA Notion root, or read the CMC mailbox?
3. Can a non-Josh sender, or content inside an email or page, trigger a tool or change policy?
4. Do the spend ceilings hold across a restart?
5. Does a trace outage fail closed?

## Install deliverables (Nov 24-25)
`install_shadow.sh`, `verify_host`, an uninstall script, a one-page runbook on the Command Center,
and a frozen `v3-core` SHA.

## Credentials (Josh creates by Nov 24; never stored here)
Telegram bot token, model API key capped at $50 at the provider, Notion integration shared only
with the POA page. Home Assistant token (dedicated v3 user) and Gmail read-only authorization
happen during install.

## Install-time configuration (values never committed)
Supplied to `install_shadow.sh` and written to a root-owned config file on the host, not the repo:
`OWNER_CHAT_ID`, `FAMILY_CALENDAR_ID` (given by Josh in chat 2026-11-16), `BRIEF_SEND_TIME` (default 08:30 America/New_York, matching v2),
`POA_ROOT_PAGE_ID`. Credentials (Telegram token, model key, Notion secret, HA token, Google OAuth)
are entered on the host only.
