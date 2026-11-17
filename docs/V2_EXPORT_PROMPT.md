# Message to paste to your v2 Potter in Telegram (one message)

Everything between the lines is the message. It asks v2 to READ and COPY its own files into a private GitHub repo.
It changes nothing in v2.

---
Potter, I'm building v3 and it must do everything you do. Please copy a complete, secret-free picture of yourself
into a new PRIVATE GitHub repository. This is read-only for you: do not change, stop, restart or reconfigure
anything, and do not touch any other repository.

**Step 0 - access.** Use only a GitHub credential you already have or that I give you for this single repo. If you
have none, stop now, tell me exactly what you need (a fine-grained token limited to one new repo, contents:
read/write), and do nothing else. Do not ask me to paste a token in chat.

**Step 1 - the repo.** Create `potter-v2-export` under my account as PRIVATE. After creating it, call the GitHub
API and confirm `private: true`. If it is not private, delete it and stop.

**Step 2 - what to copy.** Build this folder, then commit it to `main`:

```
README.md                 what is here and how to read it (you write this)
manifest.json             exact shape below
export-report.md          what you excluded and why, and anything you run that I did not list here
inventory/                read-only command output, saved as text files, secrets redacted:
    cron.json               every cron/automation (name, schedule, timezone, target, full message text)
    mcp.json                every MCP server (name, transport, command or URL; env/header NAMES only)
    skills.txt, plugins.txt channels.txt, models.txt, openclaw-version.txt
    processes.txt           every service/process you run besides OpenClaw (systemd units, docker, cron entries)
    network.txt             ports you listen on and hostnames you call (no keys)
workspace/                every file in your agent workspace that shapes behavior (IDENTITY, SOUL, AGENTS, TOOLS,
                          HEARTBEAT, USER, rules/ incl. INDEX and every domain rule, prompts/ incl. the morning
                          brief skill and the v0/v1 prompts, skills/, state/ templates like the compiling doc,
                          .learnings/, propagation map, anything else). Same relative paths.
apps/<name>/              source code of every program you run besides OpenClaw: potter-kg and the Jev curator,
                          Merrick V1, Alpha Bot and its loop, the AgentMail listener, the Brooklyn events
                          scraper, the Margaretville monitor, financial digest jobs, nightly QC recorder/
                          verifier/recheck, and any other. Include data files (SQLite etc.) under 50 MB each.
```

**Step 3 - what must NOT go in.** No secrets of any kind (API keys, tokens, passwords, OAuth files, `.env`,
private keys, cookies, session files). No chat or session transcripts. No clinical, EHR or patient material, and
nothing from the CMC mailbox or the clinical email address. No files over 50 MB (list them in export-report.md).
Before committing, scan every file for secrets (key and token patterns, `.env`, `*.pem`, `*.key`, `credentials*`,
`token*.json`). If anything matches: leave that file out, record its path and the secret's NAME in
`secrets_needed`, and mention it in export-report.md. If you are unsure about a file, leave it out and list it.

**Step 4 - manifest.json**, exactly this shape (JSON, no comments):

```
{
  "schema": "potter.v2-export/1",
  "workspace": {"files": ["every file under workspace/, relative to workspace/"]},
  "crons": [{"name": "kebab-case-name", "cron": "30 8 * * 1-5", "tz": "America/New_York",
             "message": "exact trigger prompt text", "message_file": "optional path under workspace/",
             "channel": "telegram", "enabled": true,
             "touches_shared_external_state": false}],
  "mcp_servers": [{"name": "robinhood", "url": "https://...", "transport": "streamable-http",
                   "purpose": "one line"}],
  "apps": [{"name": "potter-kg", "path": "potter-kg", "install": "pip install -r requirements.txt",
            "service": {"exec": "python3 serve.py"}, "touches_shared_external_state": false,
            "purpose": "one line"}],
  "secrets_needed": [{"name": "ROBINHOOD_TOKEN", "used_by": "alpha-bot", "how_to_get": "one line",
                      "target": {"kind": "app_env", "app": "alpha-bot"}}],
  "models": {"primary": "...", "fallback": "..."},
  "notes": "how the pieces fit together; anything surprising"
}
```

Rules for the manifest: include EVERY cron (morning brief, check-ins, evening review, monitors, digests, nightly
QC, the potter-kg curator every 6 hours, Alpha Bot's loop, everything else), EVERY MCP server and EVERY app. Set
`touches_shared_external_state` to true for anything that trades, sends, posts, writes to a shared account, or is
the only consumer of a shared feed (the AgentMail listener, Alpha Bot, monitors that message me); false for pure
read/compute jobs. For `install` use only `pip install -r requirements.txt`, `npm ci` or `npm install`. For
`secrets_needed.target.kind` use `app_env`, `mcp_env`, `mcp_header` (add `server`, `header`, `prefix`) or
`openclaw_env`. Names and kinds only, never a value. Cron names: lowercase letters, digits, `-` and `_`.

**Step 5 - commit and report.** One commit on `main` with the message `v2 export`. Then reply to me with: the
repo URL, the commit SHA, the number of files, the list of every cron, MCP server and app you included, everything
you excluded and why, and anything you run that this message did not list. Also send me `potter-v2-export.zip`
containing the same folder, as a backup.
---

## After v2 replies (you)
1. Open the repo on GitHub and skim the file list once. If you see anything that should not be there, tell v2 to
   fix it and force-remove it, or delete the repo and start over.
2. Tell me the repo name. I attach it to my session and only read it.
3. I check it with the importer, fix what it reports, and tell you if v2 needs to re-export anything.
4. At install, upload the zip in the form's "Potter v2 export file" box.
