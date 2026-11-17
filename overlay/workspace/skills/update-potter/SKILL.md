---
name: update-potter
description: Update Potter itself to new code ("update yourself", "install the new version", "deploy potter-core") without a reinstall. Use only when the owner asks.
---
# Update Potter

The owner can ask you to update yourself. The new code must already be in `/opt/potter/src` (the owner's helper
copies it in; you cannot fetch the private repository).

1. Run `bash /opt/potter/src/install/update.sh --check` and tell the owner which code version it found.
2. Run `bash /opt/potter/src/install/update.sh` (add `--profile <zip>` only if the owner gave you a v2 export file).
   It starts the update as a separate background service and returns at once; relay its two lines to the owner.
3. That service stops your gateway for a minute or two, installs potter-core, the model policy, Merrick's Jev
   decider, memory search and the shipped skills and rules, starts the gateway, runs the health check, and sends
   the owner the result on Telegram itself. You will not see it happen: your next turn runs on the new code.
4. Never stop or restart the gateway yourself for an update, and never edit installed files by hand. If the owner
   reports a FAIL line from the update message, read `journalctl -u potter-update-*` and report what it says.
