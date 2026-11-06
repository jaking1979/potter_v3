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

## Main-branch protocol: promotion, detector, deploy gate (T39 test 1)

### 1. Candidate construction and promotion (`promote.py`)
- **One commit per change.** A change is reviewed as **one commit whose parent is the current authoritative `main` tip**: authors squash, then rebase onto `main`, before requesting review.
- **How promotion works.** `promote()` fast-forwards `main` to exactly that receipted commit with an ordinary, non-forced push. It is refused if the candidate isn't exactly one commit on top of the authoritative tip, or has no issued receipt for its exact tree. The remote also refuses any push that isn't a fast-forward.
- **Rebase means re-review.** Rebasing creates a new SHA with no receipt, which must be re-reviewed (T39 test 4).
- **Concurrent promotion.** The first change wins. The second is no longer on top of `main`, so it's refused and must be rebased and re-reviewed.

### 2. Detector (`run_detector`, at least hourly)
- **Live main:** it reads `main` from the **authoritative remote** (`ls_remote_tip`), using objects fetched locally, never a local ref.
- **Start point:** the last anchored `clear` tip (or the baseline) must be on the first-parent chain, or the run is refused as a force-push or rewrite.
- **Rule:** every first-parent commit after it must be an exact receipted commit, or it's an incident. A `--no-ff` merge commit is not receipted.
- **Recording:** it anchors `clear` or `incident`, with compare-and-set on the branch's latest detector event, and always alerts on an incident.

### 3. Deploy gate (`release_allowed`), called by every builder, updater, rollback or manual deploy
It reads the tip from the **authoritative remote at call time**. If the remote is unreachable, nothing is allowed.
- **`purpose="deploy"`:** the commit **is** the authoritative tip, carries a receipt for its exact tree, and has a fresh anchored `clear` (≤ 65 min).
- **`purpose="rollback"`:** the commit carries an exact receipt and lies on the authoritative first-parent chain **at or before the last anchored `clear` tip**. It is allowed during an open incident, because that's the remedy.
- **The deploy-window race:** consumers call `deploy` again *after* deploying. If it now fails, `main` moved during the window, and they roll back to the last cleared commit with `rollback`. That leaves an exposure window of one deploy duration, which is detected and remedied rather than eliminated. Fencing the remote ref itself would require the protected-branch setting.

### 4. Audit trail
Every issuance, use, `clear` and `incident` is an event in the authoritative anchor log.

### Tests (`tests/test_t39_main_detector.py`, 29)
**Logic tests, against a scratch repo acting as the authority (20):**
- **Gate timing:** fast-forward clean and deployable; a push after clearance voids the clear; expiry and future-dated clears refused; prior clear followed by an unrecorded incident, with recovery.
- **Recording and receipts:** compare-and-set on stale and out-of-order writes; clear-without-receipt refused; a `--no-ff` merge is an incident.
- **Incidents and rewrites:** intermediate commits; a second-parent rewrite; a reset.
- **Failure modes:** anchor and registry failures; a forged DB; a tampered anchor; a wrong git dir; `GIT_DIR` ignored.

**End-to-end, with a bare remote plus separate author and consumer clones (9):**
- **Stale and unreachable remote:** a stale consumer ref can't deploy after the remote advances; an unreachable remote refuses everything.
- **Deploy race:** the post-deploy recheck catches the race, and the rollback is allowed.
- **Rollback rules:** rollback to an older cleared release works; rollback during an incident works; rollback to an unreceipted, off-mainline, or beyond-last-clear commit is refused.
- **Promotion:** a multi-commit candidate is refused; an unreceipted candidate is refused; in a concurrent promotion the second change must rebase, its stale review is refused, and it passes once re-reviewed.

**Mutation checks:**
- reading the local `origin/main` instead of the remote fails 6 tests;
- dropping the rollback ordering fails 1;
- dropping the one-commit promotion rule fails 1.
