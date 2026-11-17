# Potter v2 -> v3: what v2 does and how v3 gets it

Source: your POA page (status as of Oct 2026) and the morning-brief reference. "Arrives with the export" means
v2 hands the real files over (docs/V2_EXPORT_PROMPT.md); the installer puts them in place.

| v2 does | v3 gets it by | Status |
|---|---|---|
| Chief of Staff in Telegram | OpenClaw Telegram channel, v3's own bot, only you can talk to it | built |
| Web/phone app, voice | OpenClaw Control UI branded Potter, talk mode (needs OpenAI voice key) | built |
| Models: OpenRouter primary, fallback model | form takes the OpenRouter key; fallback comes in the export notes | built / export |
| Morning Brief 8:30 ET, 7 formats, compiling doc, Todoist reconciliation, weather, family calendar | brief skill + v2's real prompt, compiling doc template and state files | skill built; v2 files with export |
| Daily check-in, evening review (silent stress/busy capture), capture protocol, article capture, interest wiki | v2 rules and prompts | arrives with the export |
| Knowledge graph (potter-kg) + Jev curator every 6 hours | app + cron | arrives with the export |
| Merrick V1 home specialist (thermal learning, HVAC plans) + Home Assistant | app + HA connection (full state and control) | HA connection built; Merrick with export |
| Email: AgentMail listener, Gmail (jaking1979), compiler into the compiling doc | official AgentMail plugin (email channel + CLI skill, standby until "take over email"), Gmail via Composio, v2's listener logic from the export | plugin built; listener logic with export |
| Alpha Bot trading loop, Robinhood MCP (79 tools) | app + MCP + secrets on the settings page | arrives with the export; starts in standby |
| Event and market monitors (Brooklyn events, Margaretville, financial digests) | apps + crons | arrives with the export |
| Nightly QC recorder, verifier, recheck | crons + scripts | arrives with the export |
| Notion ask-first writes, EHR hard wall, no unreviewed sends | written into Potter's SOUL.md as rules (as in v2) | built |
| Check-In Log, Operational/Durable Memory, Active Work, Inbox, Monitoring List (Notion) | Notion connection via Composio or the Notion integration | needs your Notion key |
| Operating Manual | in Notion, unchanged | n/a |

**Standby (handover).** Anything that acts on a shared real account (the trading loop, the email listener,
monitors that message you) is imported switched off. Tell v3: "what is in standby?" and "take over <name>" once v2
has stopped doing that job, so nothing runs twice. Everything else starts running.

**What I cannot know until I see the export:** v2's exact files, app layout and schedules. The importer validates
the export and reports exactly what it could not use. I fix anything it reports.
