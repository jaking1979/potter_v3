---
name: evening-checkin
description: The evening check-in / shutdown ritual. Use when the owner answers the "Evening check-in: how did the day actually go?" nudge, or says they want to close out or recap the day.
---
# Evening check-in (shutdown ritual)

A program sends the opening question once each weekday evening (see `state/evening-nudge.json` for today's send).
When the owner answers it, run this. Two to four exchanges. One question at a time. No coaching filler.

1. **Review.** Let them tell it. From what they say and did, never inference, read two signals: stress
   (high / normal / low) and busy (packed / normal / slack). Do not ask these as a checklist. No signal means normal.
2. **Sweep open loops.** For anything left open, capture loop + next action + when. If a loop has no next step, ask
   once: "What's the next move on that?" Never nag.
3. **Look ahead.** Note tomorrow's known commitments or constraints. Name the first hard commitment if knowable.
4. **Close.** One line: "You're clear. I've got N loops tracked and tomorrow's shape noted. It'll be in the morning
   brief. Done for the day."

## Silent capture (after they sign off; never announce it)
Edit `state/compiling-doc.md`:
- **State Signals:** replace the three lines with today's stress, busy (one line of context if notable), and
  tomorrow's outlook (or "clear").
- **Open Loops & Commitments:** one line each: `- [loop] - next: [action] - [when]`.
- **Manual Captures:** only what they explicitly said to remember for tomorrow.

Do not capture: sensitive family or emotional material, anything clinical, raw quotes, or your interpretations.
If they do not answer the nudge, do nothing: the signals stay as they were (carried forward, not reset).
