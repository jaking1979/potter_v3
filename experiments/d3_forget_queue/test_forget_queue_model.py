"""Offline SQLite ordering model; not D3, C3, a connector, or a protocol."""
import os
import sqlite3
import tempfile
import unittest


class QueueModel:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=2)
        self.db.execute('PRAGMA foreign_keys=ON')

    @staticmethod
    def bootstrap(path):
        c = sqlite3.connect(path)
        c.executescript('''
          CREATE TABLE fact (id TEXT PRIMARY KEY, value TEXT, active INTEGER NOT NULL);
          CREATE TABLE work (id TEXT PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES fact(id),
                             payload TEXT, state TEXT NOT NULL);
          CREATE TABLE unknown (work_id TEXT PRIMARY KEY);
        ''')
        c.close()

    def transaction(self, sql, values=()):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = self.db.execute(sql, values)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def enqueue(self, work_id, fact_id):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT value FROM fact WHERE id=? AND active=1', (fact_id,)).fetchone()
            if row is None:
                self.db.rollback()
                return False
            self.db.execute('INSERT INTO work VALUES (?, ?, ?, ?)', (work_id, fact_id, row[0], 'pending'))
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def claim(self, work_id, after_lock=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if after_lock is not None: after_lock()
            row = self.db.execute('SELECT payload FROM work WHERE id=? AND state=?', (work_id, 'pending')).fetchone()
            if row is None:
                self.db.rollback()
                return None
            self.db.execute('UPDATE work SET state=? WHERE id=?', ('in_flight', work_id))
            self.db.commit()
            return row[0]
        except BaseException:
            self.db.rollback()
            raise

    def forget(self, fact_id, after_lock=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if after_lock is not None: after_lock()
            self.db.execute('INSERT OR IGNORE INTO unknown SELECT id FROM work WHERE fact_id=? AND state=?', (fact_id, 'in_flight'))
            self.db.execute('DELETE FROM work WHERE fact_id=? AND state=?', (fact_id, 'pending'))
            self.db.execute('UPDATE work SET payload=NULL WHERE fact_id=? AND state=?', (fact_id, 'in_flight'))
            self.db.execute('UPDATE fact SET value=NULL, active=0 WHERE id=?', (fact_id,))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise


class OrderingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, 'test.sqlite')
        QueueModel.bootstrap(self.path)
        self.a, self.b = QueueModel(self.path), QueueModel(self.path)
        self.a.transaction('INSERT INTO fact VALUES (?, ?, ?)', ('f', 'synthetic-secret', 1))

    def tearDown(self):
        self.a.db.close(); self.b.db.close(); self.temp.cleanup()

    def test_forget_before_claim_purges_pending_and_blocks_later_enqueue(self):
        self.assertTrue(self.a.enqueue('q', 'f'))
        self.b.forget('f')
        self.assertIsNone(self.a.claim('q'))
        self.assertFalse(self.a.enqueue('q2', 'f'))
        self.assertEqual(self.a.db.execute('SELECT count(*) FROM work').fetchone()[0], 0)

    def test_claim_before_forget_is_unknown_not_asserted_canceled(self):
        self.assertTrue(self.a.enqueue('q', 'f'))
        detached = self.a.claim('q')
        self.b.forget('f')
        self.assertEqual(detached, 'synthetic-secret')  # already escaped transaction
        self.assertEqual(self.a.db.execute('SELECT payload, state FROM work').fetchone(), (None, 'in_flight'))
        self.assertEqual(self.a.db.execute('SELECT work_id FROM unknown').fetchone(), ('q',))

    def test_unknown_survives_reopen(self):
        self.a.enqueue('q', 'f'); self.a.claim('q'); self.b.forget('f')
        self.a.db.close(); self.a = QueueModel(self.path)
        self.assertEqual(self.a.db.execute('SELECT work_id FROM unknown').fetchone(), ('q',))

    def test_forget_rollback_leaves_fact_and_pending_work_unchanged(self):
        self.a.enqueue('q', 'f')
        self.a.db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON work BEGIN SELECT RAISE(ABORT, 'failed'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.a.forget('f')
        self.assertEqual(self.b.db.execute('SELECT value, active FROM fact').fetchone(), ('synthetic-secret', 1))
        self.assertEqual(self.b.db.execute('SELECT payload, state FROM work').fetchone(), ('synthetic-secret', 'pending'))


# Multiprocess contention exercises the toy's shared SQLite ordering only.
def _race_operation(path, kind, gate, outcomes):
    db = QueueModel(path)
    try:
        gate.wait(timeout=10)
        if kind == 'claim':
            result = db.claim('q')
        else:
            db.forget('f')
            result = 'forgotten'
        outcomes.put((kind, result, None))
    except BaseException as exc:
        outcomes.put((kind, None, repr(exc)))
    finally:
        db.db.close()


class ProcessOrderingTests(unittest.TestCase):
    def test_concurrent_claim_and_forget_have_only_two_safe_model_outcomes(self):
        import multiprocessing as mp
        ctx = mp.get_context('spawn')
        with tempfile.TemporaryDirectory() as tmp:
            for iteration in range(20):
                path = os.path.join(tmp, f'{iteration}.sqlite')
                QueueModel.bootstrap(path)
                setup = QueueModel(path)
                setup.transaction('INSERT INTO fact VALUES (?, ?, ?)', ('f', 'synthetic-secret', 1))
                self.assertTrue(setup.enqueue('q', 'f'))
                setup.db.close()
                gate, outcomes = ctx.Barrier(3), ctx.Queue()
                procs = [ctx.Process(target=_race_operation, args=(path, op, gate, outcomes))
                         for op in ('claim', 'forget')]
                try:
                    for proc in procs:
                        proc.start()
                    gate.wait(timeout=10)
                    results = [outcomes.get(timeout=10) for _ in procs]
                    for proc in procs:
                        proc.join(10)
                        self.assertEqual(proc.exitcode, 0)
                    self.assertTrue(all(error is None for _, _, error in results), results)
                    claimed = next(result for kind, result, _ in results if kind == 'claim')
                    self.assertIn(claimed, (None, 'synthetic-secret'))
                    check = QueueModel(path)
                    try:
                        self.assertEqual(check.db.execute('SELECT value, active FROM fact').fetchone(), (None, 0))
                        work = check.db.execute('SELECT payload, state FROM work').fetchall()
                        unknown = check.db.execute('SELECT work_id FROM unknown').fetchall()
                        if claimed is None:
                            self.assertEqual((work, unknown), ([], []))
                        else:
                            self.assertEqual((work, unknown), ([(None, 'in_flight')], [('q',)]))
                        self.assertFalse(check.enqueue('new', 'f'))
                    finally:
                        check.db.close()
                finally:
                    for proc in procs:
                        if proc.is_alive():
                            proc.terminate()
                            proc.join(5)
                    outcomes.close()
                    outcomes.join_thread()


# Explicitly force both serialization orders, not just scheduler-selected races.
def _ordered_operation(path, kind, is_first, locked, release, attempting, outcomes):
    db = QueueModel(path)
    try:
        def hold():
            locked.set()
            if not release.wait(10):
                raise TimeoutError("first transaction was not released")
        if not is_first:
            attempting.set()
        hook = hold if is_first else None
        if kind == "claim":
            result = db.claim("q", after_lock=hook)
        else:
            db.forget("f", after_lock=hook)
            result = "forgotten"
        outcomes.put((kind, result, None))
    except BaseException as exc:
        outcomes.put((kind, None, repr(exc)))
    finally:
        db.db.close()


class ForcedProcessOrderingTests(unittest.TestCase):
    def test_each_transaction_order_is_forced_at_the_write_lock(self):
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        for first_kind in ("forget", "claim"):
            with self.subTest(first=first_kind), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "queue.sqlite")
                QueueModel.bootstrap(path)
                setup = QueueModel(path)
                setup.transaction("INSERT INTO fact VALUES (?, ?, ?)", ("f", "synthetic-secret", 1))
                self.assertTrue(setup.enqueue("q", "f"))
                setup.db.close()
                locked, release, attempting, outcomes = ctx.Event(), ctx.Event(), ctx.Event(), ctx.Queue()
                second_kind = "claim" if first_kind == "forget" else "forget"
                procs = [ctx.Process(target=_ordered_operation,
                    args=(path, kind, is_first, locked, release, attempting, outcomes))
                    for kind, is_first in ((first_kind, True), (second_kind, False))]
                try:
                    procs[0].start()
                    self.assertTrue(locked.wait(10), "first did not acquire lock")
                    procs[1].start()
                    self.assertTrue(attempting.wait(10), "second did not attempt operation")
                    release.set()
                    results = [outcomes.get(timeout=10) for _ in procs]
                    for proc in procs:
                        proc.join(10)
                        self.assertEqual(proc.exitcode, 0)
                    self.assertTrue(all(error is None for _, _, error in results), results)
                    claimed = next(value for kind, value, _ in results if kind == "claim")
                    self.assertEqual(claimed, None if first_kind == "forget" else "synthetic-secret")
                    check = QueueModel(path)
                    try:
                        self.assertEqual(check.db.execute("SELECT value, active FROM fact").fetchone(), (None, 0))
                        work = check.db.execute("SELECT payload, state FROM work").fetchall()
                        unknown = check.db.execute("SELECT work_id FROM unknown").fetchall()
                        self.assertEqual((work, unknown), ([], []) if first_kind == "forget"
                                         else ([(None, "in_flight")], [("q",)]))
                        self.assertFalse(check.enqueue("later", "f"))
                    finally:
                        check.db.close()
                finally:
                    release.set()
                    for proc in procs:
                        if proc.is_alive():
                            proc.terminate()
                        if proc.pid is not None:
                            proc.join(5)
                    outcomes.close()
                    outcomes.join_thread()


if __name__ == '__main__': unittest.main()

