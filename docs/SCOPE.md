# Potter v3 - what is being built (source of truth, 2026-11-16)

Josh's rule: build only what was discussed. OpenClaw is the harness for now. Potter is the layer on top:
settings, personality, skills, branding and an installer. Nothing else.

## The install (Nov 27-29): what Josh gets
1. **Talk to Potter** in Telegram (its own bot) and in a web app that installs on a phone (chat + voice).
   The web app is OpenClaw's own UI, branded as Potter.
2. **A Morning Brief** every weekday at 8:30 ET, in the same style as v2's, including the Family Calendar
   (today + 7 days). Missing data is reported, never treated as "quiet".
3. **"Why did you do that / what did it cost"** on request.
4. **Read-only home status** (Home Assistant) and **read-only personal Gmail triage** (never the CMC mailbox).
   Gmail triage is the first thing dropped if time runs out.
5. **Connect more things by chatting**: Composio (Google, Todoist and the other apps) and AgentMail are added
   as connections; Potter proposes, Josh approves. Secrets are typed into a form, never into chat.
6. **An installer** with plain web forms, a health check, an uninstall script and a one-page runbook. The same
   installer works for anyone Josh gives it to.

## Rules that hold on day one
- Potter replies to Josh and reads. It does not send email, buy, trade, post, delete or control devices unless
  Josh switches that on, one capability at a time, from chat.
- Runs in its own new container. Live Potter v2, Alpha Bot, existing credentials, the EHR space and the CMC
  mailbox are never touched.
- Only Josh's Telegram ID can talk to it. No secrets in the repo, Notion or chat.

## Not part of this build (listed so nobody builds it by accident)
Phone number and calls, payments, agent browser, password vault, extra assistants for other people, proactive
monitors, memory writes, benchmark evals, a from-scratch app, custom Telegram/voice/scheduler code.

## Honest limits to know about
- **Spend caps.** OpenClaw has no per-request or per-task dollar ceilings. The protection is the model key's
  own limit (set $50, and a daily reset if OpenRouter offers it). The ratified $0.50/request, $2/task, $10/day
  ceilings are NOT enforced by this build.
- **Read-only through Composio** is set where the connection is made (read-only Google permissions / only read
  tools enabled in the Composio server). Potter's own settings cannot filter Composio's tools. The setup form
  says so, and the health check cannot prove it.
- Tested here against a real OpenClaw in a sandbox. Not yet run on the real Proxmox host, with Tailscale, or
  with live Composio/AgentMail.

## Audit questions (for Dolores) and where each is answered
1. External effects other than a Telegram reply: tool deny list, approvals, AgentMail send tools hidden,
   Composio scopes; check `verify_host.sh` and the connect-apps skill.
2. Reads outside the POA Notion root or of the CMC mailbox: connection scopes; Notion integration shared with
   the POA page only; Gmail is the personal account.
3. Non-Josh sender or content triggering a tool: Telegram allowlist + groups disabled; skills treat content as data.
4. Spend ceilings across restart: model-key limit only (see limits above).
5. Trace outage fails closed: not provided by this build; OpenClaw audit and logs are the record.
