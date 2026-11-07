import multiprocessing as mp
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory import MemoryStore, Conflict, Invalid


def verifier(op, proof, payload):
    return proof == ('synthetic', op)


def stage(store, request='same', value='synthetic-value'):
    return store.stage(request_id=request, key='synthetic-key', value=value,
                       origin='external', source_ref='synthetic-source')


def worker(path, mode, queue, ident=None):
    store = MemoryStore.open_offline(path, verifier, environment='synthetic')
    try:
        if mode == 'stage':
            queue.put(stage(store).id)
        elif mode == 'promote':
            try:
                store.promote(ident, 1, ('synthetic', 'promote'))
                queue.put('promoted')
            except Conflict:
                queue.put('conflict')
        elif mode == 'crash_before':
            def crash(*args):
                os._exit(42)
            store._audit = crash
            stage(store)
        elif mode == 'crash_after':
            stage(store)
            os._exit(43)
    finally:
        store.close()


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'memory.sqlite'
        self.store = MemoryStore.bootstrap_offline(self.path, verifier, environment='synthetic')
        self.addCleanup(self.store.close)
        self.ctx = mp.get_context('spawn')

    def reopen(self):
        store = MemoryStore.open_offline(self.path, verifier, environment='synthetic')
        self.addCleanup(store.close)
        return store

    def start(self, mode, queue, ident=None):
        p = self.ctx.Process(target=worker, args=(self.path, mode, queue, ident))
        p.start()
        return p

    def finish(self, process, expected=0):
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join()
            self.fail('synthetic worker did not exit')
        self.assertEqual(process.exitcode, expected)

    def test_restart_keeps_staging_promotion_audit_and_dedupe(self):
        item = stage(self.store)
        promoted = self.store.promote(item.id, item.version, ('synthetic', 'promote'))
        reopened = self.reopen()
        self.assertEqual(reopened.retrieve('synthetic-key', consequential=True).active, (promoted,))
        self.assertEqual(stage(reopened).id, item.id)
        self.assertEqual(reopened._db.execute('SELECT count(*) FROM audit').fetchone()[0], 2)

    def test_competing_process_stage_has_one_request_and_audit(self):
        q = self.ctx.Queue()
        processes = [self.start('stage', q) for _ in range(4)]
        for p in processes:
            self.finish(p)
        self.assertEqual(len({q.get(timeout=2) for _ in processes}), 1)
        for table in ('item', 'request', 'audit'):
            self.assertEqual(self.store._db.execute('SELECT count(*) FROM '+table).fetchone()[0], 1)
        q.close()

    def test_competing_process_promotions_have_one_winner(self):
        a = stage(self.store, 'a', 'first')
        b = stage(self.store, 'b', 'second')
        q = self.ctx.Queue()
        processes = [self.start('promote', q, item.id) for item in (a, b)]
        for p in processes:
            self.finish(p)
        self.assertCountEqual([q.get(timeout=2) for _ in processes], ['promoted', 'conflict'])
        self.assertEqual(len(self.store.retrieve('synthetic-key', consequential=True).active), 1)
        q.close()

    def test_crash_before_commit_keeps_items_requests_audit_atomic(self):
        q = self.ctx.Queue()
        self.finish(self.start('crash_before', q), 42)
        reopened = self.reopen()
        for table in ('item', 'request', 'audit'):
            self.assertEqual(reopened._db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
        self.assertEqual(stage(reopened).status, 'staged')
        q.close()

    def test_crash_after_commit_uncertain_retry_is_idempotent(self):
        q = self.ctx.Queue()
        self.finish(self.start('crash_after', q), 43)
        reopened = self.reopen()
        item = stage(reopened)
        self.assertEqual(reopened._db.execute('SELECT item_id FROM request').fetchone()[0], item.id)
        self.assertEqual(reopened._db.execute('SELECT count(*) FROM audit').fetchone()[0], 1)
        q.close()

    def test_forget_and_supersession_survive_restart(self):
        a = stage(self.store, 'a', 'synthetic-old')
        self.store.promote(a.id, a.version, ('synthetic', 'promote'))
        b = stage(self.store, 'b', 'synthetic-new')
        b = self.store.promote(b.id, b.version, ('synthetic', 'promote'))
        stage(self.store, 'c', 'synthetic-staged')
        self.store.forget(b.id, b.version, ('synthetic', 'forget'))
        reopened = self.reopen()
        self.assertTrue(all(row[0] == '' for row in reopened._db.execute('SELECT value FROM item')))
        self.assertEqual(reopened.retrieve('synthetic-key', consequential=False).provisional, ())
        self.assertEqual(reopened.retrieve('synthetic-key', consequential=True).active, ())

    def test_missing_incompatible_and_production_stores_refused(self):
        missing = self.path.parent / 'missing.sqlite'
        with self.assertRaises(Invalid):
            MemoryStore.open_offline(missing, verifier, environment='synthetic')
        self.assertFalse(missing.exists())
        with self.assertRaises(FileExistsError):
            MemoryStore.bootstrap_offline(self.path, verifier, environment='synthetic')
        with self.assertRaises(Invalid):
            MemoryStore.open_offline(self.path, verifier, environment='production')
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaises(Invalid):
            self.reopen()
        with self.assertRaises(Invalid):
            stage(self.store)

    def test_missing_unique_index_fails_closed(self):
        with sqlite3.connect(self.path) as db:
            db.execute('DROP INDEX one_active_key')
        with self.assertRaises(Invalid):
            stage(self.store)

    def test_injected_audit_failure_rolls_back_every_write(self):
        def fail(*args):
            raise RuntimeError('synthetic audit failure')
        self.store._audit = fail
        with self.assertRaises(RuntimeError):
            stage(self.store)
        for table in ('item', 'request', 'audit'):
            self.assertEqual(self.store._db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)

    def test_closed_store_and_symlink_refused(self):
        link = self.path.parent / 'link.sqlite'
        link.symlink_to(self.path)
        with self.assertRaises(Invalid):
            MemoryStore.open_offline(link, verifier, environment='synthetic')
        self.store.close()
        with self.assertRaises(Invalid):
            stage(self.store)


if __name__ == '__main__':
    unittest.main()
