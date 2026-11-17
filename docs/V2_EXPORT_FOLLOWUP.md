# Follow-up message to paste to your v2 Potter (one message)

Everything between the lines is the message. Read-only for v2 apart from its own export repo.

---
Potter, thank you, the export is close. Three fixes, all inside `potter-v2-export` only. Do not change, stop or
restart anything else.

**1. A live secret got through.** `apps/ha-monitor/ha-monitor.sh` line 8 contains a real Telegram bot token
(`TELEGRAM_BOT=...`). Deleting the line is not enough because it stays in git history. Do this: delete the GitHub
repo `potter-v2-export`, recreate it PRIVATE (confirm `private: true`), and push a fresh single commit that never
contained the token. In the new copy replace the literal with `${TELEGRAM_BOT_TOKEN}` and add it to
`secrets_needed`. Then re-scan every file for: bot tokens (digits, colon, 30+ characters), JWTs (`eyJ...`),
`sk-...`, `am_...`, `ghp_...`, `tskey-...`, private keys. Tell me which bot the token belongs to so I can decide
about rotating it in BotFather.

**2. Keep code where it lives.** Regrouping programs under `apps/` broke every path the cron commands and prompts
use (for example `/home/josh/.openclaw/workspace/scripts/quality-control/verifier.py`,
`.../workspace/merrick/scripts/...`, `.../workspace/main/potter-kg/scripts/kg-curate.mjs`). Replace `workspace/`
and `apps/` with ONE mirrored tree:

```
tree/home/josh/.openclaw/workspace/...     everything, at its real relative path: behavior files AND code
tree/home/josh/poa/...                     anything under ~/poa that crons, scripts or prompts reference
tree/<any other absolute path>/...         same rule for any other referenced location
```

Include source code and small data (SQLite etc. under 50 MB each). Still exclude: secrets, session transcripts and
logs, clinical/EHR material, DREAMS.md, `.git`, `node_modules`, `venv`/`.venv`, `__pycache__`, `site-packages`,
caches, screenshots, files over 50 MB (list them). Keep `package.json`, lockfiles and `requirements.txt` so
dependencies can be reinstalled; where a Python program has no requirements file, generate one and say so.
Include the unit file text for every systemd service you run (for example `agentmail-listener.service`) under
`inventory/units/`, and the user crontab as `inventory/crontab.txt`.

**3. Manifest version 2.** Set `"schema": "potter.v2-export/2"`. Keep `mcp_servers`, `secrets_needed`, `models`,
`notes`. Remove `workspace`, `crons` and `apps`, and add:

```
"tree_root": "tree",
"agents": [{"id": "main", "workspace": "/home/josh/.openclaw/workspace/main", "model": "...", "purpose": "..."}],
"plugins": [{"id": "potter-guardrails", "path": "/home/josh/.openclaw/workspace/plugins/potter-guardrails",
             "purpose": "..."}],
"services": [{"name": "agentmail-listener", "unit": "inventory/units/agentmail-listener.service",
              "cwd": "/abs/path", "exec": "exact ExecStart", "touches_shared_external_state": true}],
"dependencies": [{"dir": "/abs/path", "install": "npm ci | npm install | pip install -r requirements.txt"}],
"system_packages": ["apt package names the programs need, e.g. python3-venv, sqlite3"],
"runtimes": {"node": "v24.15.0", "python": "3.x", "ollama_models": ["gemma4:e2b-it-qat"]},
"shared_state_crons": ["names of crons that trade, send, post, write to a shared account, or are the only
                        consumer of a shared feed"]
```

Crons: I will read `inventory/cron.json` directly, so make sure every job there has its real `schedule`
(`cron`/`every`/`at`), `payloadKind`, the FULL untruncated `message` or command, `sessionTarget`, `agentId`,
`channel`, `delivery_mode`, `delivery_to`, `enabled`. For MCP servers whose URL contains a secret, give the URL
with the secret part written as `${NAME}` and list NAME in `secrets_needed`.

Reply with the new repo URL, commit SHA, file count, what changed, and what you excluded.
---

## Why (for Josh)
- The token: private repo, so only you, GitHub and my session could see it, but it is in history. The safe move
  is to recreate the repo; whether to also rotate the bot token in BotFather is your call (I recommend it).
- The layout: my first message asked v2 to group programs under `apps/`. That was my mistake: v2's schedules run
  commands at their real paths. Mirroring the real tree lets v3 recreate those paths exactly, so every command and
  prompt works unchanged.
