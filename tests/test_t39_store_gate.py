"""T39 receipt registry and consumer gates (Dolores review on 79e4c0e).

- Lifecycle: one issuance at merge; CI -> builder -> host -> rollback all
  succeed on that receipt without consuming nonces; a never-issued receipt
  fails at every consumer (finding 1).
- Detector reads issued receipts only; forged, partial or deleted state makes
  the registry unavailable (finding 2).
- Explicit bootstrap; re-initialisation, restored older backups and forks are
  refused; a crash between commit and anchor write recovers (finding 3).
- T39 test 3 (replay) and test 4 (commit added after review) at consumption.
"""
import multiprocessing as mp
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from t39.consumer_gate import Refused, admit_merge, require_issued, unreceipted_main_commits
from t39.receipt_registry import (FileAnchor, Issue, ReceiptRegistry, RegistryUnavailable,
                                  Facts)

NOW = datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)
C1, T1, C2, T2 = "a" * 40, "b" * 40, "c" * 40, "d" * 40
R1, R2 = "e" * 64, "f" * 64
N1, N2, N3 = "1" * 64, "2" * 64, "3" * 64


def facts(commit=C1, tree=T1, rh=R1, nonces=(N1, N2), **over):
    base = {"commit": commit, "tree": tree, "receipt_hash": rh, "nonces": nonces,
            "agents": {"author": "dolores", "reviewer": "claude"},
            "key_fingerprints": {"author": "SHA256:aaa", "reviewer": "SHA256:bbb"},
            "verifier_version": "t39-verify/1"}
    base.update(over)
    return base


def _race(path, anchor_path, q):
    reg = ReceiptRegistry(Path(path), FileAnchor(Path(anchor_path)))
    q.put(reg.issue(Facts(**{**facts(), "nonces": (N1, N2)}), "ci", NOW).value)
    reg.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.path, self.anchor_path = d / "receipts.db", d / "anchor.jsonl"
        self.anchor = FileAnchor(self.anchor_path)
        self.reg = ReceiptRegistry.initialize(self.path, self.anchor, "potter-main", NOW)

    def tearDown(self):
        self.reg.close()
        self.tmp.cleanup()

    def reopen(self):
        self.reg.close()
        self.reg = ReceiptRegistry(self.path, self.anchor)

    def admit(self, f=None, commit=C1, tree=T1):
        return admit_merge(commit=commit, tree=tree, load_receipt=lambda c: "r",
                           verify=lambda r: f or facts(), registry=self.reg, issuer="ci", now=NOW)

    def use(self, consumer, f=None, commit=C1, tree=T1, on_main=True, receipt="r"):
        return require_issued(commit=commit, tree=tree, load_receipt=lambda c: receipt,
                              verify=lambda r: f or facts(), registry=self.reg,
                              consumer=consumer, purpose="deploy", on_main=on_main, now=NOW)


class Lifecycle(Base):
    def test_ci_builder_host_rollback_share_one_issued_receipt(self):
        self.admit()
        for consumer in ("release-builder", "host-updater", "rollback"):
            self.assertEqual(self.use(consumer).receipt_hash, R1)
        self.reopen()                                   # survives restart
        self.use("rollback")

    def test_never_issued_receipt_refused_at_every_consumer(self):
        for consumer in ("release-builder", "host-updater", "rollback"):
            with self.subTest(consumer), self.assertRaises(Refused) as ctx:
                self.use(consumer)
            self.assertTrue(ctx.exception.incident)

    def test_issuer_retry_is_idempotent_but_replay_refused(self):
        self.admit()
        self.admit()                                    # exact same facts: ALREADY_ISSUED
        with self.assertRaises(Refused):                # T39 test 3: nonce reused elsewhere
            self.admit(facts(commit=C2, tree=T2, rh=R2), commit=C2, tree=T2)
        with self.assertRaises(Refused):                # partial overlap
            self.admit(facts(commit=C2, tree=T2, rh=R2, nonces=(N1, N3)), commit=C2, tree=T2)

    def test_registry_reports_replay_outcome(self):
        f1 = Facts(**{**facts(), "nonces": (N1, N2)})
        f2 = Facts(**{**facts(commit=C2, tree=T2, rh=R2), "nonces": (N2, N3)})
        self.assertIs(self.reg.issue(f1, "ci", NOW), Issue.ISSUED)
        self.assertIs(self.reg.issue(f1, "ci", NOW), Issue.ALREADY_ISSUED)
        self.assertIs(self.reg.issue(f2, "ci", NOW), Issue.REPLAY)
        self.assertIsNone(self.reg.get(R2))

    def test_presented_receipt_must_match_issued_binding(self):
        self.admit()
        with self.assertRaises(Refused):
            self.use("host-updater", f=facts(key_fingerprints={"author": "SHA256:aaa",
                                                                "reviewer": "SHA256:zzz"}))

    def test_verifier_upgrade_does_not_invalidate_issued_receipts(self):
        # Review note on PR #15: verifier_version is audit-only.
        self.admit()
        self.use("host-updater", f=facts(verifier_version="t39-verify/2"))
        self.admit(facts(verifier_version="t39-verify/2"))      # issuer retry: ALREADY_ISSUED

    def test_commit_added_after_review_refused(self):
        # T39 test 4: receipt for C1 presented for C2 / altered tree.
        with self.assertRaises(Refused):
            self.admit(commit=C2)
        with self.assertRaises(Refused):
            self.admit(tree=T2)
        self.admit()                                    # nonces were not burned
        with self.assertRaises(Refused) as ctx:
            self.use("host-updater", commit=C2)
        self.assertTrue(ctx.exception.incident)

    def test_verifier_failures_and_incomplete_facts_refuse(self):
        def bad(_):
            raise ValueError("bad signature")
        with self.assertRaises(Refused):
            admit_merge(commit=C1, tree=T1, load_receipt=lambda c: "r", verify=bad,
                        registry=self.reg, issuer="ci", now=NOW)
        for broken in (facts(nonces=(N1,)), facts(nonces=(N1, N1)),
                       facts(key_fingerprints={"author": "k", "reviewer": "k"}),
                       facts(agents={"author": "dolores"}), facts(verifier_version=""),
                       facts(agents={"author": "dolores", "reviewer": "dolores"})):
            with self.subTest(broken=broken), self.assertRaises(Refused):
                self.admit(broken)
        with self.assertRaises(Refused):
            self.use("host-updater", receipt=None)

    def test_detector_uses_issued_receipts(self):
        self.admit()
        self.assertEqual(unreceipted_main_commits([C1, C2], self.reg), [C2])

    def test_concurrent_issuers_exactly_one_issues(self):
        self.reg.close()
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_race, args=(str(self.path), str(self.anchor_path), q))
                 for _ in range(5)]
        for p in procs:
            p.start()
        results = [q.get(timeout=60) for _ in procs]
        for p in procs:
            p.join(timeout=60)
        self.assertEqual(results.count("issued"), 1)
        self.assertEqual(results.count("already_issued"), 4)
        self.reg = ReceiptRegistry(self.path, self.anchor)
        self.assertEqual(len(self.anchor.entries()), 2)


class FlakyAnchor(FileAnchor):
    """Anchor whose append can be made to fail (host unreachable)."""

    def __init__(self, path):
        super().__init__(path)
        self.fail = False

    def append(self, entry):
        if self.fail:
            raise OSError("anchor host unreachable")
        super().append(entry)


def forge_issue_in_db(path, receipt_hash, commit, tree, nonces):
    """Attacker with registry-DB write access only: append a correctly hashed
    issue event plus matching index rows. Touches nothing in the anchor."""
    import json as _json
    from t39.receipt_registry import _link
    db = sqlite3.connect(path)
    seq, prev = db.execute("SELECT seq, head FROM chain ORDER BY seq DESC LIMIT 1").fetchone()
    body = {"facts": {"commit": commit, "tree": tree, "receipt_hash": receipt_hash,
                      "nonces": sorted(nonces),
                      "agents": {"author": "dolores", "reviewer": "claude"},
                      "key_fingerprints": {"author": "SHA256:aaa", "reviewer": "SHA256:bbb"},
                      "verifier_version": "t39-verify/1"},
            "issuer": "ci", "at": NOW.isoformat()}
    head = _link(prev, seq + 1, "issue", body)
    db.execute("INSERT INTO chain VALUES (?, 'issue', ?, ?)",
               (seq + 1, _json.dumps(body, sort_keys=True), head))
    db.executemany("INSERT INTO nonces VALUES (?, ?)", [(n, receipt_hash) for n in nonces])
    db.execute("INSERT INTO receipts VALUES (?, ?, ?)", (receipt_hash, commit, seq + 1))
    db.commit()
    db.close()


class AnchorAuthority(Base):
    """Dolores review on 6f63a67: the DB alone must never be able to create a receipt."""

    def test_db_only_injection_refused_on_reopen(self):
        # Exactly the reported attack: close, inject a valid-looking tail, reopen.
        self.reg.close()
        forge_issue_in_db(self.path, R2, C2, T2, (N1, N2))
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry(self.path, self.anchor)
        self.assertEqual(len(self.anchor.entries()), 1)          # anchor did not advance
        self.reg = ReceiptRegistry.initialize(Path(self.tmp.name) / "s.db",
                                              FileAnchor(Path(self.tmp.name) / "s.jsonl"),
                                              "scratch", NOW)

    def test_db_injection_in_running_process_is_not_honoured(self):
        # Inject while the registry is open in this process.
        forge_issue_in_db(self.path, R2, C2, T2, (N1, N2))
        self.assertIsNone(self.reg.get(R2))                      # reads come from the anchor
        self.assertEqual(self.reg.receipted_commits([C2]), set())
        with self.assertRaises(Refused):                          # consumer refuses
            self.use("host-updater", f=facts(commit=C2, tree=T2, rh=R2), commit=C2, tree=T2)
        with self.assertRaises(Refused):                          # next write detects tamper
            self.admit(facts(commit=C1, tree=T1, rh=R1, nonces=(N3, "4" * 64)))
        self.assertEqual(len(self.anchor.entries()), 1)

    def test_detector_sees_injected_receipt_as_missing(self):
        from t39.consumer_gate import unreceipted_main_commits
        forge_issue_in_db(self.path, R2, C2, T2, (N1, N2))
        self.assertEqual(unreceipted_main_commits([C2], self.reg), [C2])

    def test_deleted_nonce_rows_cannot_enable_replay(self):
        self.admit()
        db = sqlite3.connect(self.path)
        db.execute("DELETE FROM nonces")
        db.commit()
        db.close()
        with self.assertRaises(Refused):                          # tamper caught in write txn
            self.admit(facts(commit=C2, tree=T2, rh=R2), commit=C2, tree=T2)


class CrashBoundaries(Base):
    """Recovery at every boundary of append-to-anchor-then-commit-locally."""

    def setUp(self):
        super().setUp()
        self.reg.close()
        self.anchor = FlakyAnchor(self.anchor_path)
        self.reg = ReceiptRegistry(self.path, self.anchor)

    def test_anchor_unreachable_writes_nothing_then_recovers(self):
        self.anchor.fail = True
        with self.assertRaises(Refused):
            self.admit()
        with self.assertRaises(Refused):
            self.admit()                                          # retry while unavailable
        self.assertEqual(len(self.anchor.entries()), 1)
        self.anchor.fail = False
        self.admit()
        self.use("host-updater")
        self.assertEqual(len(self.anchor.entries()), 3)

    def test_crash_after_anchor_append_before_local_commit(self):
        real_apply = self.reg._apply
        calls = {"n": 0}

        def crash(e):
            calls["n"] += 1
            raise sqlite3.OperationalError("simulated crash")
        self.reg._apply = crash
        with self.assertRaises(Refused):
            self.admit()
        self.reg._apply = real_apply
        self.assertEqual(len(self.anchor.entries()), 2)           # anchored, not local
        self.reopen()                                             # replays the tail
        self.admit()                                              # ALREADY_ISSUED
        self.use("host-updater")
        with self.assertRaises(Refused):                          # nonces were replayed too
            self.admit(facts(commit=C2, tree=T2, rh=R2), commit=C2, tree=T2)

    def test_lost_database_restored_from_anchor(self):
        self.admit()
        self.reg.close()
        self.path.unlink()
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry(self.path, self.anchor)               # never silently empty
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry.initialize(self.path, self.anchor, "potter-main", NOW)
        self.reg = ReceiptRegistry.restore_from_anchor(self.path, self.anchor)
        self.use("host-updater")
        with self.assertRaises(Refused):
            self.admit(facts(commit=C2, tree=T2, rh=R2), commit=C2, tree=T2)

    def test_restored_older_backup_catches_up_without_losing_nonces(self):
        backup = Path(self.tmp.name) / "backup.db"
        self.reg.close()
        shutil.copy(self.path, backup)
        self.reg = ReceiptRegistry(self.path, self.anchor)
        self.admit()
        self.reg.close()
        shutil.copy(backup, self.path)
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        self.reg = ReceiptRegistry(self.path, self.anchor)        # replays from anchor
        with self.assertRaises(Refused):                          # replay still refused
            self.admit(facts(commit=C2, tree=T2, rh=R2), commit=C2, tree=T2)


class Integrity(Base):
    def tamper(self, sql, *args):
        self.reg.close()
        db = sqlite3.connect(self.path)
        db.execute(sql, args)
        db.commit()
        db.close()

    def _assert_unavailable_after(self, sql, *args):
        self.admit()
        self.tamper(sql, *args)
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry(self.path, self.anchor)
        self.reg = ReceiptRegistry.initialize(Path(self.tmp.name) / "w.db",
                                              FileAnchor(Path(self.tmp.name) / "w.jsonl"),
                                              "scratch", NOW)

    def test_forged_receipt_index_row(self):
        self._assert_unavailable_after("INSERT INTO receipts VALUES (?, ?, 99)", R2, C2)

    def test_edited_local_chain_body(self):
        self._assert_unavailable_after(
            "UPDATE chain SET body = replace(body, '\"reviewer\": \"claude\"',"
            " '\"reviewer\": \"dolores\"') WHERE seq = 1")

    def test_tampered_anchor_log_refused(self):
        self.admit()
        lines = self.anchor_path.read_text().splitlines()
        lines[1] = lines[1].replace('"claude"', '"mallory"')
        self.anchor_path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(RegistryUnavailable):
            self.reopen()
        self.reg = ReceiptRegistry.initialize(Path(self.tmp.name) / "x.db",
                                              FileAnchor(Path(self.tmp.name) / "x.jsonl"),
                                              "scratch", NOW)

    def test_use_of_never_issued_receipt_refused(self):
        with self.assertRaises(RegistryUnavailable):
            self.reg.record_use(R2, "host-updater", "deploy", NOW)
        self.assertEqual(len(self.anchor.entries()), 1)

    def test_bootstrap_is_one_time(self):
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry.initialize(self.path, self.anchor, "potter-main", NOW)
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry.initialize(Path(self.tmp.name) / "other.db", self.anchor,
                                       "potter-main", NOW)

    def test_garbage_or_missing_anchor(self):
        self.reg.close()
        self.anchor_path.write_text("nope\n")
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry(self.path, self.anchor)
        self.anchor_path.unlink()
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry(self.path, self.anchor)
        self.reg = ReceiptRegistry.initialize(Path(self.tmp.name) / "v.db",
                                              FileAnchor(Path(self.tmp.name) / "v.jsonl"),
                                              "scratch", NOW)

    def test_closed_registry_refuses_consumers(self):
        self.admit()
        self.reg.close()
        with self.assertRaises(Refused):
            self.use("host-updater")
        self.reg = ReceiptRegistry(self.path, self.anchor)


if __name__ == "__main__":
    unittest.main()
