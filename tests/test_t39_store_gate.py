"""T39 durable nonce store and consumer gate.

Covers T39 acceptance test 3 (replay rejected) and the consumption half of
test 4 (a receipt for another commit/tree is refused), plus restart,
concurrency across processes, store unavailability, and the hourly
unreceipted-main detector input for test 1.
"""
import multiprocessing as mp
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from t39.consumer_gate import Refused, require_receipt, unreceipted_main_commits
from t39.nonce_store import Binding, NonceStore, Outcome, StoreUnavailable

C1, T1 = "a" * 40, "b" * 40
C2, T2 = "c" * 40, "d" * 40
R1, R2 = "e" * 64, "f" * 64
N1, N2, N3 = "1" * 64, "2" * 64, "3" * 64


def _race_worker(path, q):
    with NonceStore(Path(path)) as s:
        q.put(s.consume((N1, N2), Binding(C1, T1, R1), f"w{os.getpid()}").value)


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nonces.db"
        self.store = NonceStore(self.path, create=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_first_use_then_replay_outcomes(self):
        b = Binding(C1, T1, R1)
        self.assertIs(self.store.consume((N1, N2), b, "release"), Outcome.CONSUMED)
        self.assertIs(self.store.consume((N1, N2), b, "release"), Outcome.ALREADY_CONSUMED_SAME)
        self.assertIs(self.store.consume((N2, N1), b, "release"), Outcome.ALREADY_CONSUMED_SAME)
        self.assertIs(self.store.consume((N1, N2), Binding(C2, T2, R2), "x"), Outcome.REPLAY)
        self.assertIs(self.store.consume((N1, N3), b, "release"), Outcome.REPLAY)  # partial overlap
        self.assertIsNone(self.store.lookup(N3))  # partial overlap inserted nothing
        rec = self.store.lookup(N1)
        self.assertEqual((rec.binding, rec.consumer), (b, "release"))

    def test_input_validation(self):
        b = Binding(C1, T1, R1)
        for bad in [(N1,), (N1, N1), (N1, "x"), [N1, N2], ("A" * 64, N2)]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.consume(bad, b, "release")
        with self.assertRaises(ValueError):
            Binding(C1, T1, "e" * 63)
        with self.assertRaises(ValueError):
            self.store.consume((N1, N2), b, "Release Builder")

    def test_survives_restart(self):
        self.store.consume((N1, N2), Binding(C1, T1, R1), "release")
        self.store.close()
        self.store = NonceStore(self.path)
        self.assertIs(self.store.consume((N1, N2), Binding(C2, T2, R2), "x"), Outcome.REPLAY)

    def test_missing_store_is_unavailable_not_empty(self):
        with self.assertRaises(StoreUnavailable):
            NonceStore(Path(self.tmp.name) / "absent.db")

    def test_wrong_schema_version_unavailable(self):
        db = sqlite3.connect(self.path)
        db.execute("UPDATE meta SET value = '99'")
        db.commit()
        db.close()
        with self.assertRaises(StoreUnavailable):
            NonceStore(self.path)

    def test_garbage_file_unavailable(self):
        junk = Path(self.tmp.name) / "junk.db"
        junk.write_bytes(b"not a database" * 100)
        with self.assertRaises(StoreUnavailable):
            NonceStore(junk)

    def test_concurrent_processes_exactly_one_consumes(self):
        self.store.close()
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_race_worker, args=(str(self.path), q)) for _ in range(6)]
        for p in procs:
            p.start()
        results = [q.get(timeout=60) for _ in procs]
        for p in procs:
            p.join(timeout=60)
        self.assertEqual(results.count("consumed"), 1)
        self.assertEqual(results.count("same"), 5)
        self.store = NonceStore(self.path)

    def test_commits_with_receipts(self):
        self.store.consume((N1, N2), Binding(C1, T1, R1), "release")
        self.assertEqual(unreceipted_main_commits([C1, C2], self.store), [C2])


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NonceStore(Path(self.tmp.name) / "n.db", create=True)
        self.facts = {"commit": C1, "tree": T1, "receipt_hash": R1,
                      "nonces": (N1, N2), "agents": {"author": "dolores", "reviewer": "claude"}}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def gate(self, commit=C1, tree=T1, receipt="r", verify=None, on_main=True):
        return require_receipt(
            commit=commit, tree=tree, load_receipt=lambda c: receipt,
            verify=verify or (lambda r: self.facts), store=self.store,
            consumer="release", on_main=on_main)

    def test_admits_once_then_refuses_replay(self):
        admitted = self.gate()
        self.assertEqual((admitted.commit, admitted.agents["reviewer"]), (C1, "claude"))
        with self.assertRaises(Refused) as ctx:
            self.gate()
        self.assertIn("already used", ctx.exception.reason)

    def test_unreceipted_main_commit_is_incident(self):
        with self.assertRaises(Refused) as ctx:
            self.gate(receipt=None)
        self.assertTrue(ctx.exception.incident)
        with self.assertRaises(Refused) as ctx:
            self.gate(receipt=None, on_main=False)
        self.assertFalse(ctx.exception.incident)

    def test_receipt_for_other_commit_or_tree_refused_without_consuming(self):
        # T39 test 4: a commit added after review voids the receipt.
        with self.assertRaises(Refused):
            self.gate(commit=C2)
        with self.assertRaises(Refused):
            self.gate(tree=T2)
        self.assertIsNone(self.store.lookup(N1))
        self.gate()  # the receipt is still usable for its own candidate

    def test_verifier_exception_and_lookup_failure_refuse(self):
        def bad(_):
            raise ValueError("bad signature")
        with self.assertRaises(Refused):
            self.gate(verify=bad)
        with self.assertRaises(Refused):
            require_receipt(commit=C1, tree=T1, load_receipt=lambda c: 1 / 0,
                            verify=lambda r: self.facts, store=self.store,
                            consumer="release", on_main=True)
        self.assertIsNone(self.store.lookup(N1))

    def test_malformed_facts_refuse(self):
        for key, value in [("nonces", (N1,)), ("receipt_hash", "x"), ("nonces", None)]:
            with self.subTest(key=key), self.assertRaises(Refused):
                self.gate(verify=lambda r, k=key, v=value: {**self.facts, k: v})

    def test_unresolved_commit_refused(self):
        for c, t in [("HEAD", T1), (C1, ""), (C1.upper(), T1)]:
            with self.subTest(c=c), self.assertRaises(Refused):
                self.gate(commit=c, tree=t)

    def test_closed_store_refuses(self):
        self.store.close()
        with self.assertRaises(Refused):
            self.gate()
        self.store = NonceStore(Path(self.tmp.name) / "n.db")


if __name__ == "__main__":
    unittest.main()
