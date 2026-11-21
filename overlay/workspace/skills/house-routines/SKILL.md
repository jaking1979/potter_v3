---
name: house-routines
description: The owner's house routines run by Merrick - bedtime, wake, close-up, coming up, arrival, the fireplace, humidity and the family room fan. Use when the owner says "I'm going to bed", "close up the house", "we're coming up Friday at 7", "we're here", "how did the night go", or asks what a routine does.
---
# House routines (Merrick runs them; you relay)

A house directive is Merrick's job: run `merrick ask "<the owner's words>"` and relay its reply as it is. Never
call Home Assistant yourself to do a routine's work, and never reload an integration or change Home Assistant's
setup without asking. The routines are data in the house file's `routines` block (`rules/projects/home-assistant.md`
says where the house file is); `merrick routines-check` says whether it is usable.

## What the owner says -> what runs
- "I'm going to bed" (optionally "up at 7:30"): bedtime now; the wake warm-up is planned so the living area is
  warm BY the wake time. Merrick's reply includes the plan line; relay it.
- "Close up the house": the perimeter check, then lock and arm only after the owner says "confirm departure";
  the rest (outlets off, away hold, the house flagged closed) runs with it. If the owner vouches for a door or
  window ("the living room window is closed"), include those words in the request: their word counts.
- "We're coming up Friday at 7 pm": deck outlet on now; warm by arrival (68 at night, 70 by day). If Merrick asks
  "am or pm", ask the owner that one question.
- "We're here": arrival (outlets on, the house no longer flagged closed). The front door being unlocked or the
  owner's phone arriving home does the same on its own while the house is closed.
- "How did the night go?": `merrick ask "how did the night go"`; relay the record. Never grade from memory.
- The fireplace fan coming on, humidity and the family room fan are handled by `potter-merrick-watch` every 5
  minutes. Nothing for you to do; if the owner asks, read `state/routines-log.jsonl` in Merrick's state folder.

## Shadow mode
Until the switch-over (`house-cutover`), the block says `"mode": "shadow"`: every routine runs but writes nothing,
and replies start "Shadow run". The old assistant still runs the house; say so if the owner seems to expect a change.

## Never
Answer a "what would you do if..." by running a routine: describe it. Lock, arm or unlock anything outside the
close-up confirmation. Edit the routines block without the owner's words for the change; after any edit, run
`merrick routines-check` and send the owner what changed. No output beyond the reply when nothing failed.
