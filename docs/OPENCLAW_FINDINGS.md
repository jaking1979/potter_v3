# What OpenClaw 2026.11.4 already ships (verified by installing it, 2026-11-16)

Installed with Node 24 (`npm i -g openclaw@latest`), MIT licensed. Bundled and documented in the package:

| v3 need | Already in OpenClaw | Notes |
|---|---|---|
| Web UI with native feel | Control UI (Solid, served by the gateway), **PWA install + web push**, offline/reconnect, themes | rebrand/extend; `docs/web/control-ui/*` |
| Voice | **Talk mode (browser realtime)**, `talk-voice`, `elevenlabs`, `deepgram`, `azure-speech`, TTS plugins, whisper skills | in-app voice and chat exist |
| Dashboards / "today" view | Session dashboards with agent-built widgets, boards, tabs, sandboxed widgets | our Today view = widget set + skill |
| Telegram and other channels | `telegram` plus ~20 channels, DM pairing | |
| Mobile | iOS/Android companion apps, `qr` pairing, HealthKit doc | |
| Browser / computer use | `browser`, `cua-computer`, Chrome extension, sandbox | live view in UI |
| Credentials | `vault` (HashiCorp), `onepassword`, `secrets` runtime, SecretRefs | |
| Memory | `memory-core`, `memory-wiki`, `active-memory` | provenance/inspection UX is ours |
| Scheduling / responsibilities | `cron` (automations), `workboard`, `logbook`, heartbeat | |
| Approvals | `approvals`, exec approvals, permission modes, `ask-user` | |
| Multi-person | multi-agent profiles, `users`, per-agent sandbox/tools | |
| Models | `openrouter`, `anthropic`, `openai`, `litellm`, local, `clawrouter` | spend caps via provider key limits + our broker rules |
| Ops | `backup`, `audit`, `doctor`, `security audit`, `update`, `uninstall`, `onboard`, `migrate` (incl. from Hermes/Claude) | |
| Connectors | `imap`, `github`, `x`, `mcp` servers, skills: gog (Google), notion, openhue, weather, trello, obsidian... | |

## Corrections to the plan
- `policy` plugin only reports config drift ("does not enforce individual tool calls"). Per-call enforcement
  must come from tool allow/deny, exec approvals, sandboxing, and hooks. To investigate: `openclaw hooks`.
- Our own UX/UI = a Potter-branded Control UI (theme plugin + Today dashboard widgets + settings), not a
  from-scratch PWA. If Josh later wants a separate app, the Control UI source (MIT) can be forked.
- Not found in the package and still ours to add: phone number and calls, payments (Stripe Link style),
  proactivity dial and interruption policy, Morning Brief skill, the guided installer, per-install
  switches for outward actions, Josh Task Set evals.
