# Setup without a terminal (Josh, 2026-11-16)

1. One command starts the installer on the Proxmox host (or the host-connected Claude session runs it). After
   that, everything happens in a browser: the installer prints a one-time page address; the page is a plain form
   (name, time zone, Telegram bot token and ID, model key, optional Composio URL, AgentMail key, Tailscale key).
   The page validates every box, never echoes a secret, takes one submission, and expires after 30 minutes.
2. The container joins the existing tailnet and publishes Potter's own funnel (OpenClaw `gateway.tailscale.mode
   = funnel`, with password auth). The web UI password is shown once on the final page.
3. Composio and AgentMail are added as MCP servers (`mcp.servers`). Composio supplies Google, Todoist and the
   other apps already connected there. AgentMail runs as a stdio MCP (`agentmail-mcp`) with its key in the
   container only.
4. After install, new connections happen in chat: Potter runs the `connect-apps` skill, proposes the change,
   the owner approves it (`tools.openclaw.autoApproveChanges = false`), Potter proves it with one read.
   Secrets never go through chat: they use the settings form.
