---
name: house-cutover
description: The house dry-run report and the one-day switch-over of the house from the old assistant to Potter. Use when the owner asks "how is the house dry-run going", "are you ready to run the house", or "take over the house".
---
# House: dry-run and switch-over

One owner per house: until the switch-over, the old assistant runs the house and you only watch (Merrick's
control is in standby; `potter-merrick-shadow` records what you WOULD do, read-only).

## "How is the house dry-run going?"
Run `merrick shadow-report --days 7` and send its text as it is, then one line of your judgment:
- agreement 90% or more and every divergence explained (a preheat the owner asked the old assistant for, an
  arrival) -> "ready when you are";
- otherwise name the divergence that worries you and what would explain it. Never guess what the old assistant
  was told: ask the owner.

## "Take over the house" (only on the owner's explicit words, on a day nobody is travelling)
Do these in order; stop and say so if any step fails.
1. `merrick shadow-report --days 7`: send the summary; if agreement is under 80%, ask once whether to go ahead.
2. Ask the owner for every house plan the old assistant holds: preheats, holds, arrival or departure plans,
   and anything it was told to do or not do. Write each one down as the owner says it.
3. The owner tells the old assistant to stop running the house (its house jobs and its house automations). Wait
   for the owner to confirm that it has. Never touch the old assistant yourself.
4. Re-create each plan in Merrick: `merrick intend --description "<plan>" --due <ISO time with zone> --zone <zone>
   --temperature <F>` (with `--until` and `--revert-temperature` for a period). Read them back with
   `merrick intentions` and send the owner the list to confirm.
5. Switch on: `potter-cron on potter-merrick-tick` (the job that carries out plans). The cold watch and the
   readings collector are already on. Leave `potter-merrick-shadow` on for a week as a check, then
   `potter-cron off potter-merrick-shadow`.
6. Tell the owner, in one line: "I'm running the house now. Tell me, not the old assistant, about anything
   house-related from here on."

## Never
Lock, arm, or open/close a water valve (refused by design). Change a setpoint during the dry-run. Run the house
while the old assistant still does.
