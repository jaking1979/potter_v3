import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from nonce_store import prepare, consume_pair

BINDING = {'commit': 'a' * 40, 'tree': 'b' * 40, 'receipt_hash': 'c' * 64}


class NonceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'scratch.db'
        with sqlite3.connect(self.path) as db:
            prepare(db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pair_replay_rolls_back_without_burning_other_nonce(self):
        with sqlite3.connect(self.path) as db:
            self.assertTrue(consume_pair(db, ('1' * 64, '2' * 64), BINDING))
            self.assertFalse(consume_pair(db, ('3' * 64, '1' * 64), BINDING))
            self.assertTrue(consume_pair(db, ('3' * 64, '4' * 64), BINDING))
        with sqlite3.connect(self.path) as reopened:
            self.assertEqual(reopened.execute('SELECT count(*) FROM used_nonces').fetchone()[0], 4)
            self.assertEqual(reopened.execute('SELECT DISTINCT commit_id, tree_id, receipt_hash FROM used_nonces').fetchall(),
                             [(BINDING['commit'], BINDING['tree'], BINDING['receipt_hash'])])

    def test_concurrent_consumers_exactly_one_wins(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def worker():
            with sqlite3.connect(self.path, timeout=5) as db:
                barrier.wait()
                outcomes.append(consume_pair(db, ('5' * 64, '6' * 64), BINDING))
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, [True, False])

    def test_invalid_and_open_caller_transaction_fail(self):
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(ValueError):
                consume_pair(db, ('1' * 64, '1' * 64), BINDING)
            db.execute('BEGIN')
            with self.assertRaises(ValueError):
                consume_pair(db, ('1' * 64, '2' * 64), BINDING)
            db.rollback()
            self.assertEqual(db.execute('SELECT count(*) FROM used_nonces').fetchone()[0], 0)
