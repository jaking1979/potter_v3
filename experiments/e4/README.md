# E4a offline operator read-model prototype

Package E4a, T1 · Dolores → Claude review. Governing contracts: T25 and T37 clause 11 / S2; E4b action endpoints remain separate T2 work. Dependencies C7, E1 and E2 are not satisfied or integrated. Build-ahead only under Josh's labelled-assumption direction. Nothing here establishes a release, production boundary, T22 readiness or T39 receipt.

`home.project()` converts a detached JSON-compatible snapshot into six constrained sections. `respond()` formats GET `/` and `/api/status`; it does not start a listener. Every other path and every write method refuses without touching the snapshot. No code calls the harness, a store, a connector, a model or a notification API. No approval, stop or recovery action exists.

## Observed seams and explicit assumptions

- E1 #28 at `4078f970318b1403420011e82a872e79cf6b43eb` exposes `ApprovalIssuer`, `ChannelAuth`, `CommandRouter`, notifier and durable approval state. E2 #30 at `0ce8894d014e995299b0006a27ab1f7520ede6b1` exposes runner/state and relies on E1; no direct imports are made here.
- A repository/PR search found no C7 Inspect implementation during this pass. This local snapshot shape is **provisional**, not a revision to a shared contract. A reviewed C7 adapter must map the actual six endpoints and authentication/redaction boundary before integration. It must not query an unavailable harness to render durable stores.
- The snapshot schema is `potter.e4a-snapshot/v0`, with sections `health`, `tasks`, `activity`, `approvals`, `responsibilities`, `recovery`. Each is `{observed_at: canonical UTC timestamp (seconds or six fractional digits), items: [{id: canonical UUID, status: section enum, updated_at: timestamp}]}`. This preview contains IDs and status only. It intentionally has no titles, task text, exception messages, raw trace payloads, credentials, source contents, screenshots or arbitrary links.
- Only a detached trusted-adapter JSON value is supported. This is not a sandbox against hostile Python objects. Upstream source authorization, ID provenance/privacy, clinical boundary enforcement and retention are not established here. Caller-supplied UUIDs/statuses can be false; this view cannot authenticate their meaning.

## Failure and presentation behavior

Missing or malformed sections are unavailable and stale with unknown age. Other sections remain readable. Future observations, duplicate IDs, malformed timestamps/statuses/IDs and sections over 100 records refuse per section. Unknown top-level/item content is dropped. Section freshness is determined from observed_at: strictly more than 300 seconds is stale. Item age is shown neutrally as time since last change, not evidence that data is stale. A fresh empty section is described only as no items in that snapshot, not proof that the underlying system is healthy. Health-stopped input does not disable the detached read model. All output is explicitly an offline read-only preview. Approval/recovery instructions direct the reader to the established authenticated channel without inventing a destination.

HTML includes landmarks, a skip link, headings, language, table captions and header scope. It has no form, buttons, scripts or remote resources; response headers prohibit forms and framing, disable caching, and set Referrer-Policy: no-referrer. These static checks do **not** establish WCAG 2.2 AA, keyboard/screen-reader usability or a real-browser S2 result. The complete Today/Work/Activity/Memory/More PWA and E4b approval/status/recovery UX remain unfinished.

## Evidence and remaining gates

Run `python3 -m unittest discover -s tests -v`. Ten local synthetic tests passed: stopped harness data; five-minute staleness boundary and independent item age; malformed/future/missing/duplicate/oversize rejection; sensitive-payload canary exclusion; action/path refusal; response security headers; semantic HTML/no actions; invalid clocks/envelopes; forged view revalidation; fractional-second staleness preserved across HTML formatting. No network or host test ran. No CI or independent review is claimed.

Before merge/integration: actual C7 contract and read-only durable-store adapter; authenticated identity and least-privilege source access; trusted filtering and ID provenance; E1/E2 current-state projection; state consistency/cursor bounds; actual harness-down S2 test; redaction/privacy adversarial tests; browser/accessibility evidence; opposite-agent review and T39 signed evidence. E4b adds the separate S1/S2/S11 exact-approval and passkey/stop/recovery requirements. Migration is additive offline files only; rollback deletes this prototype, with no mutable runtime state.

## Bounded synthetic bytes adapter

`transport.py` adds a pure `load_snapshot(bytes)` / `project_json(bytes, now=...)`
/ `respond_json(method, path, bytes, now=...)` seam. It opens no socket and reads
no files, credentials, harness or backend. It accepts only exact immutable
`bytes`, up to 128 KiB, decoded as strict UTF-8. It preflights nesting before
JSON parsing (maximum depth eight), rejects duplicate keys including escaped
aliases, NaN/Infinity/overflow, numeric tokens over 64 characters, strings over
4,096 characters, lone surrogates, trailing data and non-object roots. These are
explicit synthetic prototype bounds, not an adopted C7 transport contract.

Malformed transport input produces six unavailable sections without reflecting
input, decoder messages or prior successful state. Valid transport with one
malformed section preserves the other five. Every parse creates detached plain
JSON values; no previous state is retained. Unknown input fields are discarded
by the existing allowlisted projector. Write methods and unknown routes refuse
before parsing or clock access. New transport tests cover all six sections,
bounds, invalid encoding/duplicate keys, sensitive canaries, fresh parse state,
partial-section failure and refusal ordering. The local E4 suite now has 19
passing tests (10 existing, nine transport tests), no skips.

The bytes seam neither authenticates its sender nor establishes source truth,
clinical filtering, a C7 adapter, a service-down S2 test or actual browser and
accessibility evidence. No actual server/authentication, action endpoint or
E4b capability exists. C7/E1/E2 integration dependencies and independent Claude
review remain open. Existing T22/T39/readiness requirements still apply.

