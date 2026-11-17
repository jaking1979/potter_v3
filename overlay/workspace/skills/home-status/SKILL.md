---
name: home-status
description: Read-only home status from Home Assistant. Use for "is the house ok", "what's the temperature", "any doors open".
---
# Home status (read-only)

Use only the Home Assistant live-state tool. Report what matters first (heat/cold risk, doors and locks, leaks,
offline devices), then the rest briefly. State the time of the reading. You cannot change anything at home: if
asked to, say that control is switched off and offer to ask the owner to enable it. If the tool is missing or
fails, say Home Assistant is not connected or unreachable; do not guess values.
