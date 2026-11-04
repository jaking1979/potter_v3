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
1. **Fast-forward only.** `main` moves only by fast-forward to an exact receipted commit, whose tree equals the receipt's tree. Merge commits, squash and rebase merges are off. Because of this, the deployed commit is always the exact commit a T39 receipt covers.
2. **Force-push or history rewrite:** any change that leaves the last anchored clear tip off `main`'s first-parent chain is an incident, even if the old tip survives as a second parent.
3. **Direct write:** any first-parent commit that isn't receipted is an incident.
4. **Audit trail:** every issuance, use, `clear` and `incident` is an event in the anchor log, which is authoritative, hash-chained, and written with its own credential on a separate host. Detector writes use compare-and-set on the branch's latest detector event, so a delayed or concurrent run can't record a stale clear over a newer result.

### Detector (`run_detector`, at least hourly)
- **Start point and check:** it starts from the last anchored `clear` tip (or the trusted baseline), requires that tip to be on `main`'s first-parent chain, and checks every later first-parent commit against rule 1.
- **Clean run:** anchors `clear {branch, tip, at}`.
- **Otherwise:** anchors `incident {…, revert_plan}` and alerts. The alert is sent even if anchoring fails.
- **No pushing:** it never pushes a revert.

### Deploy gate (`release_allowed`, called by every builder, updater, rollback or manual deploy)
It is fail-closed and independent of whether any incident was recorded. It returns True **only** when all three hold:
- (a) `main`'s **current** tip, re-read from Git, is exactly the commit being deployed;
- (b) that commit carries an issued receipt for its exact tree;
- (c) the latest anchored detector event is a `clear` of that commit, no older than 65 minutes and not future-dated.

Once `main` moves to an unreceipted commit, every earlier clear is void because (a) fails, even if the incident append failed and even after the append service recovers.

### Tests (`tests/test_t39_main_detector.py`, 20)
- **Clean path:** a fast-forward is clean and deployable, and so is fast-forward merging a reviewed branch.
- **Gate timing:** a push after clearance voids the old clear; the clearance expires, and a future-dated one is refused.
- **Dolores's exact sequence:** clear C, push an unreceipted D, the incident append fails. Both C and D are refused, before and after the append service recovers.
- **Stale writes:** stale and out-of-order detector writes are refused by the compare-and-set.
- **Receipts:** a clear without a receipt doesn't permit a deploy; a `--no-ff` merge commit is an incident.
- **Incidents and rewrites:** a direct push is an incident and not deployable; unreviewed intermediate commits are incidents; a second-parent-only checkpoint is refused as a rewrite; a history reset fails loud.
- **Anchor and registry failures:** anchor failures on clean and incident runs, and an unavailable registry, all leave deploys refused.
- **Tampering and environment:** a DB-forged receipt and a tampered anchor are both handled; a wrong git dir is refused; `GIT_DIR` is ignored.
- **Mutation checks:** removing the current-tip check, the exact-commit receipt check, or the detector compare-and-set each fails a test.
