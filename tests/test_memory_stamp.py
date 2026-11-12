"""Synthetic stamp semantics; never a delivery or restore guarantee."""
import os
import shutil
import tempfile
import unittest
from memory.store import MemoryStore, Invalid


def verify(*_):
    return True


class StampTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, 'store.db')
        self.a = MemoryStore.bootstrap_offline(self.path, verify, environment='synthetic')
        self.b = MemoryStore.open_offline(self.path, verify, environment='synthetic')

    def tearDown(self):
        self.a.close(); self.b.close(); self.temp.cleanup()

    def stage(self, request, value):
        return self.a.stage(request_id=request, key='k', value=value, origin='inferred', source_ref='synthetic')

    def test_snapshot_and_invalidations(self):
        item = self.stage('r1', 'one')
        snap, s1 = self.b.snapshot_with_stamp(['k'])
        self.assertEqual(snap['k'].provisional[0].value, 'one')
        self.assertTrue(self.a.is_current_stamp(s1))
        self.a.stage(request_id='r1', key='k', value='one', origin='inferred', source_ref='synthetic')
        self.assertTrue(self.b.is_current_stamp(s1))  # idempotent no-op
        item = self.a.promote(item.id, item.version, object())
        self.assertFalse(self.b.is_current_stamp(s1))
        _, s2 = self.b.snapshot_with_stamp(['k'])
        item2 = self.stage('r2', 'two')
        self.assertFalse(self.b.is_current_stamp(s2))
        item2 = self.a.promote(item2.id, item2.version, object())
        _, s3 = self.b.snapshot_with_stamp(['k'])
        self.a.undo(item2.id, item2.version, object())
        self.assertFalse(self.b.is_current_stamp(s3))
        snap, s4 = self.b.snapshot_with_stamp(['k'])
        active = snap['k'].active[0]
        self.a.forget(active.id, active.version, object())
        self.assertFalse(self.b.is_current_stamp(s4))
        snap, _ = self.b.snapshot_with_stamp(['k'])
        self.assertEqual(snap['k'].active, ())
        self.assertEqual(snap['k'].provisional, ())

    def test_failed_write_rolls_generation_back(self):
        item = self.stage('r1', 'one')
        _, stamp = self.b.snapshot_with_stamp(['k'])
        import sqlite3
        self.a._db.set_authorizer(lambda op, arg1, arg2, db, src: sqlite3.SQLITE_DENY if op == sqlite3.SQLITE_INSERT and arg1 == 'audit' else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(sqlite3.DatabaseError): self.a.promote(item.id, item.version, object())
        finally:
            self.a._db.set_authorizer(None)
        self.assertTrue(self.b.is_current_stamp(stamp))
        snap, _ = self.b.snapshot_with_stamp(['k'])
        self.assertEqual(snap['k'].provisional[0].value, 'one')

    def test_validation_rejects_malformed(self):
        _, stamp = self.a.snapshot_with_stamp([])
        for bad in (None, [], (stamp[0], True), (stamp[0], -1), ('bad', 0), (stamp[0], 2**63)):
            with self.subTest(bad=bad), self.assertRaises(Invalid): self.a.is_current_stamp(bad)

    def test_distinct_bootstraps_and_restore_limitation(self):
        self.stage('r1', 'one')
        _, old_stamp = self.a.snapshot_with_stamp(['k'])
        self.a.close(); self.b.close()
        backup = os.path.join(self.temp.name, 'old.db')
        shutil.copyfile(self.path, backup)
        self.a = MemoryStore.open_offline(self.path, verify, environment='synthetic')
        self.b = MemoryStore.open_offline(backup, verify, environment='synthetic')
        self.stage('r2', 'two')
        self.assertFalse(self.a.is_current_stamp(old_stamp))
        self.assertTrue(self.b.is_current_stamp(old_stamp))  # rollback/clone detection NOT provided
        other = MemoryStore.bootstrap_offline(os.path.join(self.temp.name, 'fresh.db'), verify, environment='synthetic')
        try: self.assertFalse(other.is_current_stamp(old_stamp))
        finally: other.close()

    def test_stamp_and_rows_share_snapshot_when_writer_commits_between_reads(self):
        self.a._db.execute('PRAGMA journal_mode=WAL')
        self.stage('r1', 'one')
        original = self.b._retrieve
        calls = []
        def concurrent_writer(key, consequential):
            if not calls:
                calls.append(True)
                self.stage('r2', 'two')
            return original(key, consequential)
        self.b._retrieve = concurrent_writer
        snap, stamp = self.b.snapshot_with_stamp(['k'])
        self.assertEqual([x.value for x in snap['k'].provisional], ['one'])
        self.assertFalse(self.a.is_current_stamp(stamp))

    def test_exhaustion_fails_without_change(self):
        item = self.stage('r1', 'one')
        self.a._db.execute('UPDATE metadata SET generation=?', (2**63-1,))
        _, stamp = self.b.snapshot_with_stamp(['k'])
        with self.assertRaises(Invalid): self.a.promote(item.id, item.version, object())
        self.assertTrue(self.b.is_current_stamp(stamp))
        self.assertEqual(self.b.snapshot_with_stamp(['k'])[0]['k'].active, ())

if __name__ == '__main__': unittest.main()
