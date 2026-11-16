---
name: connect-apps
description: Add or fix a connection (an MCP server, a connector, a skill, a Tailscale or channel setting) when the owner asks in chat. Use for "connect X", "add X", "why can't you reach X".
---
# Connecting apps from chat

The owner does not use a terminal. You make the change; they approve it.

1. Find out what is needed. Prefer, in order: (a) the owner's Composio connection (it already carries Google,
   Todoist and many others; check its tools first), (b) an existing MCP server or OpenClaw skill for the app,
   (c) a ClawHub skill. Say which you chose and why in one line.
2. Never ask for a secret in chat and never repeat one. If a key or token is needed, send the owner to the
   Potter settings page (secrets form) or the setup page, and wait. If a service uses OAuth, start the login
   flow and send the owner the sign-in link.
3. Propose the exact change (server name, URL or command, tools to expose) and let the owner approve it through
   the approval prompt. Do not try to bypass the approval.
4. After it is applied, probe it, run one harmless read to prove it works, and report PASS or the exact error.
5. Start every new connection read-only. Turn on writes (send, post, buy, delete, control) only when the owner
   asks, per capability, and say what you switched on.
6. Do not touch other people's accounts, the clinical/EHR space, or the CMC mailbox.
