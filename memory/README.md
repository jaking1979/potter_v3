# D3 memory governance: offline prototype

This self-contained Python experiment models staging, reviewed promotion,
contradiction and supersession links, explicit unconfirmed retrieval, version
checks, undo, and logical tombstoning. Its default database is process-local
`:memory:`; explicit synthetic-only file stores support local restart and
cross-process tests. Its injected verifier is a test seam. It has no real
authentication, Notion adapter, outbox, C1/C2/C6 integration, real user data or effects.
It is not a production backend, governance receipt, or D3 completion.

Run `python3 -m unittest discover -s tests -p 'test_memory*.py'`.

`MemoryStore.bootstrap_offline(path, verifier, environment='synthetic')`
exclusively creates a new local store. `open_offline` refuses missing,
incompatible or wrongly versioned stores and never bootstraps a replacement.
The path and its parents must be trusted and not concurrently replaced;
symlink checks are not hostile-filesystem protection. No production or staging
environment is accepted. The default constructor is still memory-only.

`BEGIN IMMEDIATE` precedes mutation reads, so request dedupe, compare-and-set,
supersession and local audit share one SQLite transaction across processes.
Reads get a snapshot transaction. Connections use FULL synchronization and
explicit `close()`. Version, metadata, column shape and the required partial
unique index are checked on each operation. There is no legacy migration.
Tests exercise process crashes before and after commit, concurrent duplicate
staging and competing promotion, audit-failure rollback and restart after
forgetting. They do not demonstrate disk/power-loss/replication guarantees.

Consequential retrieval includes unpromoted direct statements only after the
injected provenance verifier approves them. Inferred, external and imported
staged matches are excluded from context and exposed as IDs for a separate
review surface. Advisory retrieval exposes staged entries as `provisional`.
Promotion uses an explicit proof seam, and batch mode refuses sensitive and
non-direct items. Competing corrections must be restaged after a new active
version appears. Undo uses an exact item version and refuses a stale chain.
Forget logically clears the values of all local rows for a fact key, including
staged and superseded rows; idempotency entries retain payload digests, and key,
source references, links and audit metadata remain. SQLite secure deletion is
requested but physical erasure, backup/WAL/snapshot sanitization and external
deletion are not proven. Digests can themselves reveal low-entropy facts.

Open before integration: independently authenticated subject/operation/content
binding; principal permissions; T15's full metadata and validity intervals;
authoritative replicated storage, restore/rollback defense; source-safe conflict and
scope handling; forget across external systems, caches, backups and all
views; Notion staging, outbox and replay; C1/C2/C6 wiring; freshness-bounded
canonical revalidation; generation rebuilds and retrieval indexes; atomic
trace/audit; import/rollback; T21 change governance; full adversarial and
opposite-agent review. None is claimed by this experiment.
