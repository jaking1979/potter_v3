# Rules index: trigger -> file

Load only what matches. One fact lives in one file; if you find the same rule in two places, keep the one this
index points to and delete the other.

## Always-on rules
| When | File |
|---|---|
| Any action beyond reading and drafting: send, post, buy, book, schedule, delete, trade, control a device, write to Notion | `global/permissions.md` |
| How to talk to the owner; what never to surface; background and scheduled turns | `global/communication.md` |
| "Remember this", a correction, a preference, "you forgot", where to store something | `global/memory.md` |
| Finding information, "where is", reading before answering | `global/retrieval.md` |
| Finishing work, "are you sure", claiming something is done | `global/verification.md` |
| How email is run: Potter's inbox, conversations, intake, urgent items, sending | `global/email-pipeline.md` |
| Creating, changing or debugging any scheduled job, monitor or reminder | `global/schedules.md` |

## Stores outside rules/
| Information | Store |
|---|---|
| Past mistakes and their prevention rules | `learnings/ERRORS.md`, `learnings/CORRECTIONS.md` |
| Open decisions waiting on the owner | `learnings/OPEN_QUESTIONS.md` |
| Daily working memory for the Morning Brief | `state/compiling-doc.md` |
| Urgent mail waiting for delivery (written by mail-intake, sent by a program) | `state/urgent-mail-queue.jsonl` |
| Standby / handover of jobs from an older assistant | `/etc/potter/handover.json` (see the `handover` skill) |

## Personal rules (domains, projects, people, preferences)
<!-- PERSONAL-INDEX: filled in at install from the owner's profile. Add rows here as new rules are created. -->
