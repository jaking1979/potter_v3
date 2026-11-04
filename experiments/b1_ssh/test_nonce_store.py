import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from nonce_store import prepare, consume_pair, lookup, pair_matches

BINDING = {'commit': 'a' * 40, 'tree': 'b' * 40, 'receipt_hash': 'c' * 64}


class NonceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'scratch.db'
        with sqlite3.connect(self.path) as db:
            prepare(db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pair_matching_requires_both_nonces_and_exact_binding(self):
        pair = ('1' * 64, '2' * 64)
        other_pair = ('3' * 64, '4' * 64)
        other = {**BINDING, 'receipt_hash': 'd' * 64}
        with sqlite3.connect(self.path) as db:
            self.assertFalse(pair_matches(db, pair, BINDING))
            self.assertTrue(consume_pair(db, pair, BINDING))
            self.assertTrue(consume_pair(db, other_pair, other))
            self.assertTrue(pair_matches(db, pair, BINDING))
            self.assertFalse(pair_matches(db, pair, other))
            self.assertFalse(pair_matches(db, (pair[0], other_pair[0]), BINDING))
            self.assertFalse(pair_matches(db, (pair[0], 'f' * 64), BINDING))
            with self.assertRaises(ValueError):
                pair_matches(db, (pair[0], pair[0]), BINDING)
        with sqlite3.connect(self.path) as reopened:
            self.assertTrue(pair_matches(reopened, pair, BINDING))

    def test_second_insert_failure_rolls_back_first(self):
        first, second = '7' * 64, '8' * 64
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON used_nonces "
                       "WHEN NEW.nonce = '%s' BEGIN SELECT RAISE(ABORT, 'injected'); END" % second)
            db.commit()
            self.assertFalse(consume_pair(db, (first, second), BINDING))
            self.assertIsNone(lookup(db, first))
            self.assertIsNone(lookup(db, second))
            db.execute('DROP TRIGGER injected_failure')
            db.commit()
            self.assertTrue(consume_pair(db, (first, second), BINDING))

    def test_abrupt_child_exit_before_and_after_commit(self):
        script = '''
import os, sqlite3, sys
from nonce_store import consume_pair
db = sqlite3.connect(sys.argv[1])
pair = ('9' * 64, 'a' * 64)
binding = {'commit': 'a' * 40, 'tree': 'b' * 40, 'receipt_hash': 'c' * 64}
if sys.argv[2] == 'before':
    db.execute('BEGIN IMMEDIATE')
    db.execute('INSERT INTO used_nonces VALUES (?, ?, ?, ?)',
               (pair[0], binding['commit'], binding['tree'], binding['receipt_hash']))
    os._exit(21)
if not consume_pair(db, pair, binding):
    os._exit(24)
os._exit(22)
'''
        pair = ('9' * 64, 'a' * 64)
        for stage, exit_code in (('before', 21), ('after', 22)):
            child = subprocess.run([sys.executable, '-c', script, str(self.path), stage],
                                   cwd=str(Path(__file__).resolve().parent), timeout=10)
            self.assertEqual(child.returncode, exit_code)
            with sqlite3.connect(self.path) as db:
                if stage == 'before':
                    self.assertIsNone(lookup(db, pair[0]))
                    self.assertIsNone(lookup(db, pair[1]))
                else:
                    self.assertTrue(pair_matches(db, pair, BINDING))
                    self.assertFalse(consume_pair(db, pair, BINDING))

    def test_pair_replay_rolls_back_without_burning_other_nonce(self):
        with sqlite3.connect(self.path) as db:
            self.assertTrue(consume_pair(db, ('1' * 64, '2' * 64), BINDING))
            self.assertFalse(consume_pair(db, ('3' * 64, '1' * 64), BINDING))
            self.assertTrue(consume_pair(db, ('3' * 64, '4' * 64), BINDING))
        with sqlite3.connect(self.path) as reopened:
            self.assertEqual(lookup(reopened, '1' * 64), BINDING)
            self.assertEqual(lookup(reopened, 'f' * 64), None)
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
                lookup(db, 'not-a-nonce')
            with self.assertRaises(ValueError):
                consume_pair(db, ('1' * 64, '1' * 64), BINDING)
            db.execute('BEGIN')
            with self.assertRaises(ValueError):
                consume_pair(db, ('1' * 64, '2' * 64), BINDING)
            db.rollback()
            self.assertEqual(db.execute('SELECT count(*) FROM used_nonces').fetchone()[0], 0)
