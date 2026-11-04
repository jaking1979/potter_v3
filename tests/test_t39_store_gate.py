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
        self.path, self.anchor_path = d / "receipts.db", d / "anchor.json"
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

    def test_presented_receipt_must_match_issued_record(self):
        self.admit()
        with self.assertRaises(Refused):
            self.use("host-updater", f=facts(verifier_version="t39-verify/0"))

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
                       facts(agents={"author": "dolores"}), facts(verifier_version="")):
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
        self.assertEqual(self.anchor.read()[0], 1)


class Integrity(Base):
    def tamper(self, sql, *args):
        self.reg.close()
        db = sqlite3.connect(self.path)
        db.execute(sql, args)
        db.commit()
        db.close()

    def test_bootstrap_is_one_time(self):
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry.initialize(self.path, self.anchor, "potter-main", NOW)
        self.reg.close()
        self.path.unlink()                              # state loss
        with self.assertRaises(RegistryUnavailable):    # anchor proves prior state existed
            ReceiptRegistry.initialize(self.path, self.anchor, "potter-main", NOW)
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry(self.path, self.anchor)
        self.reg = ReceiptRegistry.initialize(Path(self.tmp.name) / "x.db",
                                              FileAnchor(Path(self.tmp.name) / "x.anchor"),
                                              "scratch", NOW)

    def test_restored_older_backup_refused(self):
        backup = Path(self.tmp.name) / "backup.db"
        self.reg.close()
        shutil.copy(self.path, backup)
        self.reg = ReceiptRegistry(self.path, self.anchor)
        self.admit()
        self.reg.close()
        shutil.copy(backup, self.path)                  # roll back: nonces forgotten
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry(self.path, self.anchor)
        self.reg = ReceiptRegistry.initialize(Path(self.tmp.name) / "y.db",
                                              FileAnchor(Path(self.tmp.name) / "y.anchor"),
                                              "scratch", NOW)

    def test_crash_between_commit_and_anchor_write_recovers(self):
        self.admit()
        self.anchor_path.write_text('{"seq": 0, "head": "%s"}' %
                                    self.reg._db.execute("SELECT head FROM chain WHERE seq=0")
                                    .fetchone()[0])
        self.reopen()
        self.assertEqual(self.anchor.read()[0], 1)      # caught up
        self.use("host-updater")

    def test_forked_chain_refused(self):
        self.admit()
        self.anchor_path.write_text('{"seq": 1, "head": "%s"}' % ("9" * 64))
        with self.assertRaises(RegistryUnavailable):
            self.reopen()
        self.reg = ReceiptRegistry.initialize(Path(self.tmp.name) / "z.db",
                                              FileAnchor(Path(self.tmp.name) / "z.anchor"),
                                              "scratch", NOW)

    def _assert_unavailable_after(self, sql, *args):
        self.admit()
        self.tamper(sql, *args)
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry(self.path, self.anchor)
        self.reg = ReceiptRegistry.initialize(Path(self.tmp.name) / "w.db",
                                              FileAnchor(Path(self.tmp.name) / "w.anchor"),
                                              "scratch", NOW)

    def test_forged_receipt_index_row(self):
        self._assert_unavailable_after("INSERT INTO receipts VALUES (?, ?, 99)", R2, C2)

    def test_deleted_nonce_row(self):
        self._assert_unavailable_after("DELETE FROM nonces WHERE nonce = ?", N1)

    def test_edited_chain_body(self):
        self._assert_unavailable_after("UPDATE chain SET body = replace(body, ?, ?) WHERE seq = 1",
                                       C1, C2)

    def test_chain_edit_consistent_with_indexes(self):
        # Only the hash chain can catch this: indexes are untouched.
        self._assert_unavailable_after(
            "UPDATE chain SET body = replace(body, '\"reviewer\": \"claude\"',"
            " '\"reviewer\": \"dolores\"') WHERE seq = 1")

    def test_deleted_chain_row(self):
        self._assert_unavailable_after("DELETE FROM chain WHERE seq = 1")

    def test_garbage_and_missing_anchor(self):
        self.reg.close()
        self.anchor_path.write_text("nope")
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry(self.path, self.anchor)
        self.anchor_path.unlink()
        with self.assertRaises(RegistryUnavailable):
            ReceiptRegistry(self.path, self.anchor)
        self.reg = ReceiptRegistry.initialize(Path(self.tmp.name) / "v.db",
                                              FileAnchor(Path(self.tmp.name) / "v.anchor"),
                                              "scratch", NOW)

    def test_closed_registry_refuses_consumers(self):
        self.admit()
        self.reg.close()
        with self.assertRaises(Refused):
            self.use("host-updater")
        self.reg = ReceiptRegistry(self.path, self.anchor)


if __name__ == "__main__":
    unittest.main()
