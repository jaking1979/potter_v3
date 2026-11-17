---
name: notion-architecture-reconciler
description: Keep the Potter architecture pages in Notion true to the running system after installs, upgrades and configuration changes. "Reconcile Notion" = audit (read-only); "sync the architecture to Notion" = apply.
---
# Notion architecture reconciler

Keeps the Personal Operating Assistant architecture pages in Notion accurate, without blurring what is live,
configured but unverified, designed only, disabled, or historical.

**Modes.** `audit` (default) is read-only: find what changed, verify it on the running system, map it to Notion
pages, and save a proposed update set. `sync` loads the latest audit, checks it is still fresh, and applies it;
asking for sync is the approval for those exact Notion writes and nothing else.

**When.** After any change to models, plugins, skills, connected apps, services, schedules, network exposure,
memory or permissions (the `update-potter` skill counts), and on request. Run `audit` on your own after such a
change; `sync` only when the owner asks for it.

**Never.** Read, search or write the EHR space or any client material (the guardrails block its ids; never use broad
Notion search). Never put keys, tokens, raw logs or personal content in Notion or in the package. Never call an app
usable without a live check. Never rewrite history: add a dated current-state note to an old page instead.

**Audit.**
1. Change window: since the last package in `state/notion-architecture-reconciler/`, or the triggering task.
2. Verify the actual state: `openclaw --version`, models (`openclaw config get agents.defaults.model`), plugins
   (`openclaw plugins list`), jobs (`openclaw cron list --all --json`), services, the health check
   (`/opt/potter/src/install/verify_host.sh`), connected apps by a safe read.
3. Label each finding Live / Configured-unverified / Designed / Disabled / Historical, with evidence and time.
4. Write the package `state/notion-architecture-reconciler/<id>.json`: findings, evidence, target page ids, the
   exact narrow edits, fingerprints of what was checked, open risks. It expires after 7 days. Keep ten.
5. Reply with a short table: finding, status, evidence, proposed page edit, confidence, package id.

**Sync.** Load the newest valid package; re-check its fingerprints (if changed, audit again first); fetch each target
page; apply the narrow edits (never replace whole pages); re-fetch and confirm; mark the package synced; reply with
links to the changed pages and anything left unresolved. Stop and report instead of writing if evidence conflicts
or a page's ownership is unclear.
