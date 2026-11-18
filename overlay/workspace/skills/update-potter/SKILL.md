---
name: update-potter
description: Update Potter itself to new code ("update yourself", "install the new version", "pull the latest") without a reinstall. Also the one-time GitHub setup for updates. Use only when the owner asks.
---
# Update Potter

## Normal case: "update yourself"
1. Run `potter-update --check`. It fetches from GitHub (read-only) and lists what is new. Tell the owner the
   commits in one short list (subjects only). If it says "Already up to date", say so and stop.
2. Run `potter-update` (add `--profile <zip>` only if the owner gave you a v2 export file). It pulls the code
   (fast-forward only; the previous code is kept at `/opt/potter/src.prev`), then starts the update as a
   separate background service and returns at once. Relay its last lines to the owner.
3. That service stops your gateway for a minute or two, installs the plugins, policy, skills and rules, starts
   the gateway, runs the health check, and sends the owner the result on Telegram itself. Your next turn runs on
   the new code.

## If it says "not set up" (one time)
Run `potter-update --setup <owner/repo>` with the repository the owner names (branch `v3-core` unless they say
otherwise). It makes a read-only deploy key on this machine and prints its PUBLIC half. Send the owner exactly:
- the public key line it printed (it starts with `ssh-ed25519`; it is safe to share: it is the public half)
- "Add it at GitHub: the repository > Settings > Deploy keys > Add deploy key. Title: potter-v3. Leave
  'Allow write access' OFF. Then tell me 'update yourself'."
Never show, copy or move the private key file.

## If something fails
- "GitHub refused the key": the deploy key is not added yet, or was added to a different repository.
- "edited by hand" or "branch was rewritten": stop and tell the owner; do not work around it.
- A FAIL line in the Telegram update message: read `journalctl -u 'potter-update*' --no-pager | tail -40` and
  report what it says.
- Never stop or restart the gateway yourself, and never edit installed files by hand.
- If GitHub cannot be reached at all, the owner's helper can still copy the code in and you run
  `bash /opt/potter/src/install/update.sh` as before.
