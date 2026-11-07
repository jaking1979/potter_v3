# D3 memory governance: offline prototype

This self-contained Python experiment models staging, reviewed promotion,
contradiction and supersession links, explicit unconfirmed retrieval, version
checks, undo, and tombstoning. Its database is process-local `:memory:` and its
injected verifier is a test seam. It has no real authentication, Notion
adapter, outbox, C1/C2/C6 integration, persistence, real user data or effects.
It is not a production backend, governance receipt, or D3 completion.

Run `python3 -m unittest discover -s tests -p test_memory_store.py`.

Consequential retrieval includes unpromoted direct statements only after the
injected provenance verifier approves them. Inferred, external and imported
staged matches are excluded from context and exposed as IDs for a separate
review surface. Advisory retrieval exposes staged entries as `provisional`.
Promotion uses an explicit proof seam, and batch mode refuses sensitive and
non-direct items. Competing corrections must be restaged after a new active
version appears. Undo uses an exact item version and refuses a stale chain.

Open before integration: independently authenticated subject/operation/content
binding; principal permissions; T15's full metadata and validity intervals;
idempotency under durable multiprocess concurrency; source-safe conflict and
scope handling; forget across prior versions, staged rows, caches and all
views; Notion staging, outbox and replay; C1/C2/C6 wiring; freshness-bounded
canonical revalidation; generation rebuilds and retrieval indexes; atomic
trace/audit; import/rollback; T21 change governance; full adversarial and
opposite-agent review. None is claimed by this experiment.
