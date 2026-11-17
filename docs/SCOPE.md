# Potter v3 - scope (source of truth, revised 2026-11-17)

Josh's rule (2026-11-17): v3 does **everything v2 does**, built together, all of it available and working, with
nothing locked down by me. OpenClaw is the harness for now; Potter is what sits on top.

The full list of what v2 does and how v3 gets each item is `docs/V2_PARITY.md`. How v2's real files reach v3 is
`docs/V2_EXPORT_PROMPT.md` (one message to v2) and `install/import_v2.py` (the importer).

## What the install contains
- OpenClaw (latest, fresh container) with the full toolset, Telegram bot for you only, Potter web app with
  chat and voice, Potter look.
- Potter's behavior: v2's rules (Notion ask-first, EHR wall, no unreviewed sends) and v2's Morning Brief.
- Connections: Composio, AgentMail, Home Assistant (state and control), Tailscale funnel, OpenAI voice.
- Everything from v2 via the export: rules, prompts, schedules, MCP servers, apps (potter-kg, Merrick, Alpha Bot,
  listeners, monitors, QC), installed and running or in standby.
- Installer: web form (one box per setting, one upload box for the v2 export), settings page for secrets, health
  check, uninstall, runbook.

## Handover, not lockdown
Items that act on shared real accounts come in switched off so v2 and v3 never do the same job twice. You turn each
on by asking v3 "take over X". Nothing else is restricted.

## Still true
Only your Telegram ID can talk to v3. No secrets in the repo, Notion or chat; they go in the setup form and the
settings page. v2 is only ever asked to read and package its own files.

## Honest limits
- No per-request dollar ceilings in OpenClaw. The protection is the model key's own limit.
- Tested against a real OpenClaw in a sandbox with a synthetic v2 export; not yet run on your Proxmox host, with
  Tailscale, or with the real v2 export.
