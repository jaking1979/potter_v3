# t39 — durable nonce store and consumer gate (B1, production path)

This package holds two of the production pieces of the ratified T39 receipt gate. The production signature verifier is Dolores's package; this code **injects** it and does not implement it.

- **`nonce_store.py`** — a durable, multi-process-safe record of consumed nonce pairs, each tied to (commit, tree, receipt_hash). It is the successor to `experiments/b1_ssh/nonce_store.py`.
  - **Explicit outcomes:** `CONSUMED`, `ALREADY_CONSUMED_SAME` and `REPLAY`, decided inside one IMMEDIATE transaction. The experiment returned a bare boolean instead.
  - **Durability:** WAL and `synchronous=FULL` are always set.
  - **Unavailable, never empty:** the store refuses to start empty, and a schema-version mismatch or failed integrity check makes it unavailable.
  - **Who used it first:** each record keeps the consumer's id and the time.
- **`consumer_gate.py`** — `require_receipt(...)`. Every builder, updater, rollback or manual deploy path calls this before using a commit.
  - **What it checks:** the consumer passes the commit and tree it resolved from git. The receipt is loaded and fully verified by the injected verifier. The receipt must bind exactly that commit and tree. The nonce pair must be a first use.
  - **Refusal:** anything else is refused, and a missing receipt on `main` is flagged as an incident.
  - **Detector input:** `unreceipted_main_commits` feeds the hourly direct-push detector.

## T39 acceptance tests covered here
- **Test 3 (replay):** rejected, including partial-overlap and reordered pairs.
- **Test 4 (commit added after review):** rejected at consumption, and the nonce is not burned.
- **Test 1 (detector input):** the list of unreceipted main commits.

Also covered: survives a restart; 6 spawned processes racing produce exactly one `CONSUMED`; missing, garbage, wrong-version and closed stores fail closed; verifier and lookup exceptions refuse.

## Not here
- The production verifier (tests 2, 5 and 6).
- Wiring into the real release builder and updater, and the hourly detector job and its alert/revert.
- Choosing the store's host and backup.

Those need the consumer interfaces confirmed on the host.
