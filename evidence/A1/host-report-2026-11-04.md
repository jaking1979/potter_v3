# A1 — redacted host evidence report, 2026-11-04

Status: **reported findings, not a pass**. Source: Claude's read-only OptiPlex report, collected 2026-11-04 14:20–14:35 UTC, in the [Build Execution Plan](https://app.notion.com/p/406a667c6316819db335d31cb4b8b69a). Dolores did not independently access the host. No orders, jobs or device actions were triggered. This record is a redacted summary and omits credentials, account numbers, message content and exact secret locations.

## Environment and evidence limits

The report identifies Proxmox VE `pve-manager/9.2.3`, Debian 13.5, Potter container Debian 13.5 and OpenClaw `2026.8.33 (f773aa0)`, and HAOS 18.1. Host configuration and individual scripts were not bound to a content hash in the report. Exact command transcripts and full source artifacts were not supplied here. Treat these as point-in-time observations; revalidate exact versions before a pass or release decision.

## A1-1 — secrets scan / inventory

**Result: incomplete; findings reported.** A read-only pattern inventory found ten distinct findings, one reported as a probable test-fixture placeholder but unverified, and additional proliferation of account identifiers; a dedicated secrets scanner was unavailable. The inventory excluded several directories and binary/large files and did not cover all systems. It does not establish a clean scan or a complete inventory. No credential was changed. The Plan report contains the redacted scope and limitations. Do not commit secret values or detailed token locations here. A2 owns remediation once its dependencies and authorization permit it.

## A1-2 — Alpha Bot containment negative test

**Result: missing / not run.** The report found no negative-test artifact and no configured financial mandate or kill-switch guard; it reported broad brokerage tool access and a trade-log gap. Those are reported configuration findings, not an executed negative test. A test must use an isolated broker stub and prove denied calls never reach the live account. Josh directed the existing bot to continue. No live configuration, credentials or trading were altered.

## A1-3 — Home Assistant / watchdog last-success

**Result: partial; monitoring failure reported.** A single read-only check of the Catskills HA endpoint returned HTTP 200. An existing monitor's last recorded success was 2026-10-08 14:37:07 UTC and the report found no active recurring alert covering it; a one-time enabled-job cadence check did not cover that script. An ad-hoc success is not proof of a deployed watchdog. The report also identified the Bay Ridge HA peer as offline, last seen 51 days before the report; this is a reported observation, not an independent verification. Its exact script/config hashes and a working alert test remain unverified.

## A1-4 — seven healthy briefs

**Result: partial; criterion not established.** The report records fifteen consecutive successful job results through 2026-11-04, but a successful generation alone does not establish the required health-line criterion. The inspection found no health marker in the skill definition or the eleven most recent archived daily documents; delivered message contents were not inspected. The required seven version-bound healthy briefs with health-line evidence remain unproven.

## Acceptance boundary

These four entries document observations and gaps; none changes an A1 checkbox to pass, proves complete safety, or authorizes live changes. Current T22/T39 and independent review/merge gates remain in force. Exact command identifiers, hashes, source-artifact locations, and safe validation evidence must be attached before a claim requiring them.
