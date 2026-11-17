# Paste this to your v2 Potter in Telegram (one message)

It only asks v2 to READ and package what it already has. It changes nothing in v2.

---
Potter, I'm building v3 and it must do everything you do. Please create ONE zip file named `potter-v2-export.zip`
and send it to me here. Read-only: do not change, stop or restart anything. Do not include secrets, session
transcripts, or any clinical, EHR or patient material. If a file contains a token or key, leave that file out and
list the secret's NAME in `secrets_needed`.

The zip must contain `manifest.json` and these folders:

- `workspace/` : every file in your agent workspace that defines how you work (IDENTITY, SOUL, AGENTS, TOOLS,
  HEARTBEAT, USER, rules/ with INDEX and all domain rules, prompts/ including the morning brief skill and v1/v0
  prompts, skills/, state/ templates such as the compiling doc, learnings, anything else that shapes behavior).
  Keep the same relative paths.
- `apps/<name>/` : the source code (and data files if they are under 100 MB each) of every program you run besides
  OpenClaw itself: potter-kg and the Jev curator, Merrick V1, Alpha Bot and its loop, the AgentMail listener, the
  Brooklyn events scraper, the Margaretville monitor, the financial digest jobs, the nightly QC recorder, verifier
  and recheck scripts, and anything else.

`manifest.json` must be exactly this shape (JSON):

```json
{
  "schema": "potter.v2-export/1",
  "workspace": {"files": ["rules/INDEX.md", "..."]},          // every file under workspace/, relative paths
  "crons": [
    {"name": "morning-brief-weekday", "cron": "30 8 * * 1-5", "tz": "America/New_York",
     "message": "the exact trigger prompt text, or use message_file", "message_file": "prompts/x.md",
     "channel": "telegram", "enabled": true,
     "touches_shared_external_state": false}                    // true if it trades, sends, posts, writes, or
  ],                                                            // is the only consumer of a shared feed
  "mcp_servers": [
    {"name": "robinhood", "url": "https://...", "transport": "streamable-http",
     "or_command": "npx", "args": ["-y", "pkg"], "purpose": "one line"}   // NO secret values anywhere
  ],
  "apps": [
    {"name": "potter-kg", "path": "potter-kg", "install": "pip install -r requirements.txt",
     "service": {"exec": "python3 serve.py"}, "touches_shared_external_state": false, "purpose": "one line"}
  ],
  "secrets_needed": [
    {"name": "ROBINHOOD_TOKEN", "used_by": "alpha-bot", "how_to_get": "one line",
     "target": {"kind": "app_env", "app": "alpha-bot"}}
  ],
  "models": {"primary": "...", "fallback": "..."},
  "notes": "anything I need to know about how these pieces fit together"
}
```

Include EVERY cron job you have (morning brief, check-ins, evening review, monitors, digests, nightly QC, the
potter-kg curator every 6 hours, Alpha Bot's daily loop, anything else) and EVERY MCP server and app. For
`install`, use only `pip install -r requirements.txt`, `npm ci` or `npm install`. For `secrets_needed.target.kind`
use `app_env` (a program reads it from its environment), `mcp_env`, `mcp_header` (with `server`, `header`,
`prefix`) or `openclaw_env`.

When it is ready, send the zip and also a plain-language list of anything you run that I did not ask for here.
---

Then save the zip it sends you. You upload it on the Potter setup form (the "Potter v2 export file" box).
The form checks it on the spot and tells you in plain words if anything is wrong; if it is, paste that message
back to v2 and ask it to fix the file.
