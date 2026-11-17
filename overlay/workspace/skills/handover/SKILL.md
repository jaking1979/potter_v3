---
name: handover
description: Switch on a v2 duty in v3 ("take over the trading loop", "take over email", "what is still in standby"). Use after the owner says v2 should stop doing that job.
---
# Handover from Potter v2

Everything from v2 is installed in v3. Items that act on shared real accounts (trading, the AgentMail listener,
monitors that message the owner, mailbox processing) arrive in STANDBY so v2 and v3 never do the same job twice.
Standby is not a limit on you: it is a one-line switch the owner flips by asking.

State lives in `/etc/potter/handover.json` (crons and apps, each with `enabled`) and `/etc/potter/import-report.json`.

**"What's in standby?"** Read the registry and list each item with a one-line purpose and whether it is on.

**"Take over X":**
1. Ask once: "Has v2 stopped doing X?" (the owner can stop it by telling v2). Do not enable until they say yes.
2. Crons: `openclaw cron enable <name>`. Apps: `systemctl enable --now potter-app-<name>.service`. Email: the
   AgentMail channel is registered under `channels` in the registry; run the command in its `enable` field (it
   sets who may email Potter), then `openclaw plugins reload agentmail` and send a test mail from an allowed address.
   Hand back: run its `disable` command.
3. Update `enabled` in the registry, run one safe check (cron status, service status), and report ON or the exact error.
4. If a secret is still missing for that item, send the owner to the settings page for it. Never ask for it in chat.

**"Hand X back":** reverse it (disable the cron or stop and disable the service) and say so.
