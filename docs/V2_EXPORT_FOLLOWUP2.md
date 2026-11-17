# Second follow-up to paste to your v2 Potter (one message)

---
Potter, the schema 2 export is the right shape. One more secret got through and I need one more inventory file.
Work only inside `potter-v2-export`; change nothing else.

**1. Live secret.** `tree/home/josh/.composio/user_data.json` contains a real Composio API key (`api_key`, starts
`uak_`). Delete the GitHub repo again, recreate it PRIVATE (confirm `private: true`), and push one fresh commit
that never contained that file. Exclude `~/.composio/user_data.json` and `~/.composio/analytics.json` entirely;
keep the rest of `~/.composio` only if a cron, script or prompt references it, otherwise drop the whole folder.
Add `COMPOSIO_USER_API_KEY` to `secrets_needed`.

**2. Scan by key name as well as by pattern.** Before committing, also flag any JSON, YAML, env or JS file where a
key named like `api_key`, `apiKey`, `token`, `secret`, `password`, `authorization` or `bearer` has a literal value
of 20+ characters. Leave those files out (or replace the value with `${NAME}`) and list each in your reply.

**3. Model configuration.** Add `inventory/models.json`: the exact `agents.defaults` model settings and each
agent's model settings from the live config (model, fallbacks, utilityModel, decisionModel, models map,
modelPolicy), the `models.providers` block with every secret value replaced by `${NAME}`, any session or channel
model overrides currently in force, and one paragraph on how the Jev decider picks a model today (which code path,
which config). No secret values.

Reply with the new repo URL, commit SHA, and the list of files you excluded or redacted this time.
---

## For Josh
The Composio key was readable by you, GitHub and my session only (private repo), but it reaches every app you
connected through Composio. Rotate it in the Composio dashboard and give v2 the new one. I removed my local copy.
