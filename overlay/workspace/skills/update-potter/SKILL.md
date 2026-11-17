---
name: update-potter
description: Update Potter itself to new code ("update yourself", "install the new version", "deploy potter-core") without a reinstall. Use only when the owner asks.
---
# Update Potter

The owner can ask you to update yourself. The new code must already be in `/opt/potter/src` (the owner's helper
copies it in; you cannot fetch the private repository).

1. Run `bash /opt/potter/src/install/update.sh --check` and tell the owner which code version it found.
2. Run `bash /opt/potter/src/install/update.sh`. It installs potter-core, wires Merrick's Jev decider and refreshes
   the shipped skills and rules. It does not touch personal rules, secrets or schedules.
3. Report its output in a few lines. The gateway restarts about 8 seconds after it finishes, so send your report
   right away; the owner's next message reaches the updated Potter.
4. If it stops with an error, report the error line exactly and change nothing by hand.
