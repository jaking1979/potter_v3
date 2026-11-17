---
name: notion-outbox
description: Retry Notion writes that failed earlier (the outbox), so nothing the owner asked to save is lost. Runs on a schedule; also on request ("what is stuck for Notion").
---
# Notion outbox

1. Call `notion_outbox_list`. If it is empty, output nothing.
2. For each item, call the same Notion tool (`tool`) with the same arguments (`params`), exactly as stored. A retry
   of an outbox item is allowed without asking again: it was decided when it was first made.
3. Call `notion_outbox_settle` with the item's `key` and `ok` true or false.
4. Output nothing, unless an item has failed 5 or more times: then tell the owner once, in one line, which page could
   not be updated and the last error. Never retry the EHR space; a write the guardrails block is settled as failed.
