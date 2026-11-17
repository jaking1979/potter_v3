# OpenClaw plugins in Potter v3 (checked 2026-11-17 against OpenClaw 2026.11.4 and ClawHub)

## Used now
| Plugin | Source | Why |
|---|---|---|
| `@agentmail/agentmail` | official (ClawHub) | Potter's own inbox: an email channel (inbound mail drives turns, durable queue, sender allowlist, replies in-thread) plus the full AgentMail CLI as a skill. Replaces the AgentMail MCP. Channel starts in standby (empty allowFrom) so it does not double-process v2's feed. |
| bundled: `telegram`, `openrouter`, `browser`, `cua-computer`, `memory-core`, `memory-wiki`, `active-memory`, `logbook`, `workboard`, `onepassword`, `vault`, `elevenlabs`, `deepgram`, `azure-speech`, `imap`, `github`, `x` ... | ships with OpenClaw | all trusted via the pinned `plugins.allow` list |
| Composio MCP (not a plugin) | Composio | one pasted URL; carries Google, Todoist and the other connected apps |

## Candidates, not installed (adopt on request)
| Plugin | Source | What it would add |
|---|---|---|
| `@composio/composio` | official | Composio CLI workflow with connect-a-missing-account-in-chat. Needs the Composio CLI installed and a `composio login`. Better for chat-driven connections; the MCP URL is simpler for first install. |
| `@openclaw/todoist` | official | Todoist directly (v2 reads/writes Todoist through Composio today). |
| `@openclaw/notion` | official | Notion pages and databases directly (v2 rules: reads open, writes ask-first). |
| `memory-wiki` (bundled) | bundled | closest match to v2's Karpathy/Wyndo interest wiki; v2's own wiki files still arrive with the export. |
| `@openclaw/voice-call`, `@openclaw/sms` | official | a phone number: Twilio/Telnyx/Plivo calls and texts. v2 has none; a v3 horizon item. |
| `@openclaw/stripe` | official | payments through Stripe's hosted MCP. v3 horizon item. |
| `@openclaw/memory-lancedb` | official | vector long-term memory with auto-recall. v2 uses potter-kg, which arrives with the export. |
| `@openclaw/google-meet` | official | join Meet calls. Not a v2 feature. |

Not needed: Anthropic Vertex (Claude via Google Vertex; v3 uses OpenRouter). No plugin exists for Home Assistant
(it uses HA's built-in MCP server) or Tailscale (built into the gateway).

## Rule
Any plugin Potter installs later must be added to `plugins.allow`; the `connect-apps` skill says so, and the
health check fails if `openclaw security audit` reports a critical finding.
