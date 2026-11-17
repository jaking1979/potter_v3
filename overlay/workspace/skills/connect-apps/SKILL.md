---
name: connect-apps
description: Add or fix a connection (an MCP server, a connector, a skill, a Tailscale or channel setting) when the owner asks in chat. Use for "connect X", "add X", "why can't you reach X".
---
# Connecting apps from chat

The owner does not use a terminal. You make the change; they approve it.

1. Find out what is needed. Prefer, in order: (a) the owner's Composio connection (it already carries Google,
   Todoist and many others; check its tools first), (b) an existing MCP server or OpenClaw skill for the app,
   (c) a ClawHub skill. Say which you chose and why in one line.
   Notion is pre-registered: sign the owner in with `openclaw mcp login notion` and send them the link it prints.
2. Never ask for a secret in chat and never repeat one. If a key or token is needed, send the owner to the
   Potter settings page (secrets form), and wait. If a service uses OAuth, start the login
   flow and send the owner the sign-in link.
3. Make the change yourself (you have the full toolset): add the MCP server, skill or plugin, then tell the owner in
   one line what you added. A new plugin must also be added to `plugins.allow` (the plugin trust list), or it will not
   load; check with `openclaw plugins list` and `openclaw security audit`.
4. Probe it, run one harmless read to prove it works, and report PASS or the exact error.
5. External actions still follow the v2 rule: show the draft or action and get a yes before it goes out.
6. Do not touch other people's accounts, the clinical/EHR space, or the CMC mailbox.
