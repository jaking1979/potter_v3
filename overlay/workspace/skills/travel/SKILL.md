---
name: travel
description: The owner's trips: what's coming up, adding a booking, check-in, changes and delays. Use for "my flights", "when do I check in", "add this booking", "is my flight on time", "check me in", or when the travel watcher's reminder gets a reply.
---
# Travel

Trips come from booking and change emails automatically (the mail watcher hands them to `potter-travel capture`;
every flight number, airport and code must appear in the email). Reminders are sent by code on time: check-in
opens (24 hours before) and leave for the airport (3 hours before). Live delays and gate changes need a FlightAware
AeroAPI key in `aeroapi.env`; without it, changes arrive by email.

- "What trips do I have?": `potter-travel list --days 30`; answer with dates, times, flight numbers, confirmation codes.
- A booking the watcher did not see (it went to the owner's personal mail): ask the owner to forward it to your
  email address, or take the text they paste and pipe it into `potter-travel add`. Report its one line.
- "Check me in" (or yes to the check-in reminder): a web task (web-tasks skill) on the airline's site with the
  confirmation code and the owner's name; any login comes from the vault. Seat changes or paid extras ask first.
  Send the boarding pass as a screenshot or PDF, and the gate if shown.
- "Is my flight on time?": the latest status from the watcher if there is a key; otherwise look it up on the
  airline's site in the browser (a read, no approval needed) and say where the answer came from.
- A wrong or duplicate trip: `potter-travel remove <id>` after the owner confirms.
Never rebook, cancel or change a seat without the owner's explicit yes on the exact option and price.
