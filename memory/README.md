# D3 memory governance: offline prototype

This self-contained Python experiment models staging, reviewed promotion,
contradiction and supersession links, explicit unconfirmed retrieval, version
checks, undo, and logical tombstoning. Its default database is process-local
`:memory:`; explicit synthetic-only file stores support local restart and
cross-process tests. Its injected verifier is a test seam. It has no real
authentication, Notion adapter, outbox, C1/C2/C6 integration, real user data or effects.
It is not a production backend, governance receipt, or D3 completion.

`review_snapshot(id, expected_version, operation=..., batch=False)` prepares a
detached synthetic review packet and deterministic digest. Promotion binds the
candidate and currently active rows; undo binds the predecessor to be restored;
forget binds every local row in the deletion set. The mutation transaction
recomputes that digest and includes it in the injected verifier's payload,
alongside exact operation, ID, version and batch mode. A binding test verifier
rejects swapped candidates, changed content, changed predecessor and an enlarged
deletion set. A callback that ignores the digest remains insecure: no actual
issuer authentication, single-use approval, expiry, C0/E1 binding or principal
authority is implemented. The packet contains content and must never be logged
as a trace. Boolean/nonnumeric versions and ambiguous batch modes are refused.

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
staged and superseded rows; affected request IDs are retained as payload-free tombstones; keys and
source references on affected rows are cleared. Opaque row links and audit metadata remain. SQLite secure deletion is
requested but physical erasure, backup/WAL/snapshot sanitization and external
deletion are not proven. Other copies and pre-forget snapshots may still contain sensitive data.

Open before integration: independently authenticated subject/operation/content
binding; principal permissions; T15's full metadata and validity intervals;
authoritative replicated storage, restore/rollback defense; source-safe conflict and
scope handling; forget across external systems, caches, backups and all
views; Notion staging, outbox and replay; C1/C2/C6 wiring; freshness-bounded
canonical revalidation; generation rebuilds and retrieval indexes; atomic
trace/audit; import/rollback; T21 change governance; full adversarial and
opposite-agent review. None is claimed by this experiment.

`memory.views.LocalView` is an additional synthetic ID-only view experiment.
A rebuild reads all explicit keys in one local snapshot and publishes an entire
immutable ID/version/status generation at once. A failed rebuild retains the
last good generation and marks it stale; reads during rebuilding see the old
complete generation. The view stores no fact values, source pointers or content
digests. Every read obtains a fresh canonical local snapshot and applies the
store's consequential-tier filter. A changed/missing indexed row uses canonical
fallback, so forgotten/superseded values and staged injected claims are not
returned from stale view content. Canonical outage fails closed. Index age over
24 hours and clock regression flag staleness; synthetic clock inputs are finite.

This is not an embedding/FTS index, shared durable generation store, Notion
canonical backend, authenticated invalidation stream or production freshness
claim. It performs direct reads every time instead of trusting a 15-minute
cache. Read-time revalidation is not an action-time fence: C1/C2 still need
canonical validation at consequential dispatch. Returned objects already held
by a caller, previous review packets and external copies cannot be recalled;
external forgetting, backup erasure, restore authority and runtime integration
remain open. Generations contain canonical IDs and versions but do not yet
implement T15's full provenance/validity fields. The bounded snapshot API has
an explicit 1,000-key limit; this is a local test limit, not a product contract.

The synthetic derived view stores nonempty groups of opaque item IDs, versions
and status only, rather than retaining queried fact keys. Empty results use a
canonical fallback and are never marked as index hits. ID and status metadata
may still persist in memory, and neither this change nor a local forget can
recall caller-held objects or prove erasure of external copies.
