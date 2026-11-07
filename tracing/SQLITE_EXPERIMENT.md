# C6 local trace sink experiment

Stacked on C6 `81b82679f0f902c18cb4310da855c5c0fd345963`; the existing
record, emitter and adapter contracts are unchanged. This is an offline
synthetic/replay storage prototype, not production audit storage.

`SqliteTraceSink.bootstrap(path, environment='synthetic')` exclusively creates
a new store. `open_existing` refuses a missing, invalid, incompatible or
wrong-environment store. It never bootstraps a replacement automatically.
The path and its parents must be trusted and not concurrently replaced.
Symlink checking is not a defense against a hostile same-user filesystem.

`put(event_id, jsonl)` validates a canonical UUID event ID and one bounded
JSONL record before writing. Only the two existing decision and uncorrelated
denial schemas are accepted, with their exact fields, bounded identifiers,
enumerations, digests and UTC timestamps. Unknown fields, duplicate JSON keys,
extra lines and arbitrary text are refused. One database has one fixed
environment; this prototype refuses staging and production. An idempotent
retry returns false; a reused ID with changed content raises. Keep both the
event ID and complete record (including time) stable across uncertain retries.

`for_event(event_id)` adapts the sink to the existing emitter seam.
`records()` validates stored records before returning them. SQLite transactions
serialize writers and the primary key owns uniqueness across processes. The
connection uses `synchronous=FULL`. This does not establish host power-loss,
filesystem, disk-failure or replicated-store guarantees.

Validation: 13 new local synthetic tests, plus the existing 14 tracing tests.
New tests cover restart, identical/conflicting retry, unknown store/schema,
environment separation, malformed/private text refusal, missing-store refusal,
separate denial routing, concurrent distinct and duplicate events across four
processes, and subprocess crashes before/after a local SQLite commit. The
uncommitted crash test exercises SQLite transaction recovery directly, rather
than an injected sink hook. No external effect accompanies these writes.

Still required: independent Claude review; dependency and T39 gates;
authenticated context/event provenance; authoritative restore/anti-rollback;
retention/volume limits; trace-version attribution and redaction policy;
runtime wiring and outage behavior; coordinated effect/audit transaction; real
storage and host evidence. An opaque digest is not a privacy guarantee if a
caller hashes low-entropy personal data. Validation limits format, not the
semantic provenance of a syntactically valid identifier or digest. No readiness,
merge, release or production authorization is implied.
