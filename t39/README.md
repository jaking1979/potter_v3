# t39 — receipt registry and consumer gates (B1, production path)

This package holds two of the production pieces of the ratified T39 receipt gate. Dolores's production verifier is **injected**, not implemented here. It has been revised after Dolores's review of `79e4c0e`.

## Lifecycle
1. **Merge admission (`admit_merge`).** The verifier checks the attestation pair, and the registry **issues** the receipt. Its two nonces are consumed in the same transaction. This is the only place nonces are consumed. If the issuer retries with byte-identical facts, the result is `ALREADY_ISSUED`. If either nonce was used before for anything else, the result is `REPLAY`.
2. **Every later consumer (`require_issued`):** the release builder, host updater, rollback and manual deploy. Each one:
   - re-verifies the signed receipt independently ("CI and the host gate both verify receipts");
   - requires the **exact issued record** to exist;
   - logs its use.

   Nothing is consumed, so the CI → builder → host → rollback sequence all runs on one receipt. A never-issued or mismatched receipt fails at every consumer, and on `main` that is flagged as an incident.
3. **Detector (`unreceipted_main_commits`):** reads issued receipts only, never bare nonces.

## Integrity (`receipt_registry.py`): the anchor log is the authority
- **Anchor first:** every event (genesis, issue, use) is appended to the **anchor log** before the local database, with compare-and-set on the sequence number. The anchor lives on a separate host with its own write credential (Brooklyn is proposed), so **database write access alone cannot add an event**.
- **The database is a replica.** On open, and inside every write transaction, the database must be an exact prefix of the anchor log:
  - extra or different local rows, or index rows the log doesn't explain, mean tampering, and the registry refuses;
  - anchor entries missing locally (a crash after the anchor append, a lost database, a restored older backup) are **replayed** from the anchor, so recovery never loses consumed nonces.
- **Reads come from the anchor.** `get`, `issued_trees` and `receipted_commits` are answered from the hash-verified anchor log, never from local rows, so a database tampered with while a process is running can't create a receipt for a consumer or the detector.
- **Bootstrap and restore:** `initialize` requires an empty anchor. A lost database is rebuilt with `restore_from_anchor`; opening never creates anything.
- **Durability:** WAL, `synchronous=FULL`, a schema-version check and `quick_check`. The anchor fsyncs each append, and its directory on creation.

## Tests (`tests/test_t39_store_gate.py`, 24)
- **Anchor authority:** Dolores's exact attack (a database-only injected issue, with matching indexes, then reopen) is refused and the anchor doesn't advance; the same injection in a running process isn't honoured by `get`, `receipted_commits` or consumers, and the next write refuses; the detector sees the injected commit as unreceipted; deleted nonce rows can't enable a replay.
- **Crash boundaries:** anchor unreachable (nothing written, retry refused, then recovery); crash after the anchor append but before the local commit (replayed on reopen, with nonces); lost database restored from the anchor; a restored older backup catches up without losing nonces.
- **Lifecycle:** the full CI → builder → host → rollback sequence, including across a restart; a never-issued receipt refused at every consumer; idempotent issuer retry; replay and partial-overlap replay (T39 test 3); a commit or tree changed after review (T39 test 4); verifier failures and incomplete facts, including identical keys for both roles; detector input; and 5 processes racing so that exactly one issues.
- **Integrity:** one-time bootstrap including after state loss; a restored older backup; crash recovery; a forked anchor; a forged index row; a deleted nonce; edited and deleted chain rows; a chain edit that leaves the indexes consistent; a garbage or missing anchor; a closed registry.
- **Mutation checks:** each of these makes at least one test fail when removed:
  - the anchor check
  - the chain-hash check
  - the nonce check at issuance
  - the issued-record check at consumers

## Not here
- **The verifier:** signatures, key policy and revocation (tests 2, 5 and 6), which is Dolores's package.
- **Where the anchor lives in production.** A file is used in the tests. In production it should be on a different host from the registry, for example the Brooklyn node under T35.
- **Wiring into the real consumers,** and the hourly detector job with its alerts and reverts.

## Direct-to-main detector and deploy gate (`main_detector.py`, T39 test 1)

### Merge and update policy (proposed; enforced here)
1. `main` moves only by (a) a fast-forward to an exact receipted commit, or (b) a `--no-ff` merge whose second parent is receipted and whose tree equals that receipt's tree. Squash and rebase merges are off.
2. **A force-push or history rewrite** is anything that leaves the last anchored clear tip off `main`'s first-parent chain. That is an incident, even if the old tip survives as a second parent and every new commit happens to be receipted.
3. **A direct write**, meaning any first-parent commit not covered by rule 1, is an incident.
4. **Audit trail:** every issuance, use, `clear` and `incident` is an event in the anchor log, which is authoritative, hash-chained, and written with its own credential on a separate host. The log *is* the audit trail; a database-only writer cannot add to it or hide from it.

### Detector (`run_detector`, at least hourly)
- **Start point:** it starts from the last anchored `clear` tip (or the trusted baseline) and requires that tip to be on `main`'s first-parent chain.
- **Check:** every first-parent commit after it is checked against rule 1, reading issued receipts from the anchor log.
- **Clean run:** anchors `clear {branch, tip, at}`.
- **Otherwise:** anchors `incident {branch, tip, from, unreceipted, revert_plan, error}` and alerts. The alert is sent even if anchoring fails, and the run is then still not clean.
- **No pushing:** it never pushes a revert.

### Deploy gate (`release_allowed`, called by every builder, updater, rollback or manual deploy)
- **Fails closed:** it returns True **only** if the latest anchored detector event for the branch is a `clear` of **exactly the commit being deployed**, no older than 65 minutes, and not from the future.
- **Refusals:** a missing, stale, incident, unreadable or tampered state refuses. A commit pushed after the last clean run is therefore not deployable until the detector clears it. No block file exists whose absence could unblock a deploy.

### Tests (`tests/test_t39_main_detector.py`, 16)
- **Clean path:** a fast-forward is clean and then deployable.
- **Gate timing:** a push after clearance isn't deployable; the clearance expires, and a future-dated one is refused.
- **Merges:** a merge with the reviewed tree is clean; a merge with an extra file is an incident.
- **Incidents:** a direct push is an incident, isn't deployable, and keeps being reported; unreviewed intermediate commits are incidents.
- **Rewrites:** **a checkpoint that survives only as a second parent is refused as a rewrite**; a history reset fails loud.
- **Anchor unreachable:** on a clean run, deploys stay refused; on an incident, the alert still goes out and deploys are refused.
- **Tampering and environment:** an unavailable registry fails loud; a DB-only forged receipt doesn't hide a push; a tampered anchor fails loud; a wrong git dir is refused; an inherited `GIT_DIR` is ignored.
- **Mutation checks:** removing the first-parent check, the exact-tip check, or the freshness check each makes a test fail.
