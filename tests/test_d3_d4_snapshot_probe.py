"""Offline integration probes; not production privacy or delivery evidence."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from memory.store import MemoryStore
from retrieval.core import retrieve_turn
from retrieval.snapshot_probe import SnapshotProbe


class SnapshotProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / 'synthetic.db'
        self.store = MemoryStore.bootstrap_offline(path, lambda *args: True,
                                                  environment='synthetic')
        self.other = MemoryStore.open_offline(path, lambda *args: True,
                                              environment='synthetic')
        staged = self.store.stage(request_id='one', key='test-key',
                                  value='synthetic-secret', origin='direct',
                                  source_ref='fixture')
        self.item = self.store.promote(staged.id, staged.version, proof='synthetic')

    def tearDown(self):
        self.other.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_current_snapshot_excludes_staged_rows(self):
        self.store.stage(request_id='two', key='test-key', value='staged-secret',
                         origin='external', source_ref='fixture')
        result = await retrieve_turn(SnapshotProbe(self.store), 'test-key',
                                     consequential=True)
        self.assertEqual((result.status, result.blocked), ('complete', True))
        self.assertEqual([x.text for x in result.items], ['synthetic-secret'])

    async def test_forget_before_check_rejects_detached_result(self):
        snapshot_taken, proceed = asyncio.Event(), asyncio.Event()
        async def pause():
            snapshot_taken.set()
            await proceed.wait()
        task = asyncio.create_task(retrieve_turn(
            SnapshotProbe(self.store, after_snapshot=pause), 'test-key',
            consequential=True))
        await snapshot_taken.wait()
        self.other.forget(self.item.id, self.item.version, proof='synthetic')
        proceed.set()
        result = await task
        self.assertEqual((result.status, result.items, result.blocked),
                         ('unavailable', (), True))

    async def test_unrelated_mutation_also_invalidates_global_stamp(self):
        snapshot_taken, proceed = asyncio.Event(), asyncio.Event()
        async def pause():
            snapshot_taken.set()
            await proceed.wait()
        task = asyncio.create_task(retrieve_turn(
            SnapshotProbe(self.store, after_snapshot=pause), 'test-key',
            consequential=True))
        await snapshot_taken.wait()
        self.other.stage(request_id='other', key='different-key', value='other',
                         origin='external', source_ref='fixture')
        proceed.set()
        result = await task
        self.assertEqual((result.status, result.items, result.blocked),
                         ('unavailable', (), True))

    async def test_unavailable_authority_rejects_snapshot(self):
        snapshot_taken, proceed = asyncio.Event(), asyncio.Event()
        async def pause():
            snapshot_taken.set()
            await proceed.wait()
        task = asyncio.create_task(retrieve_turn(
            SnapshotProbe(self.store, after_snapshot=pause), 'test-key',
            consequential=True))
        await snapshot_taken.wait()
        self.store.close()
        proceed.set()
        result = await task
        self.assertEqual((result.status, result.items, result.blocked),
                         ('unavailable', (), True))

    async def test_forget_after_check_exposes_known_race(self):
        checked, proceed = asyncio.Event(), asyncio.Event()
        async def pause():
            checked.set()
            await proceed.wait()
        task = asyncio.create_task(retrieve_turn(
            SnapshotProbe(self.store, after_check=pause), 'test-key',
            consequential=False))
        await checked.wait()
        self.other.forget(self.item.id, self.item.version, proof='synthetic')
        proceed.set()
        result = await task
        # This deliberate counterexample proves the check is not a fence.
        self.assertEqual((result.status, result.blocked), ('complete', False))
        self.assertEqual([x.text for x in result.items], ['synthetic-secret'])
