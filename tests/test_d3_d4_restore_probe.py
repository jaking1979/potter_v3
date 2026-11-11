"""Synthetic D3/D4 stale-replica counterexample, not a production test."""
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory.store import MemoryStore
from retrieval.core import retrieve_turn
from retrieval.snapshot_probe import SnapshotProbe


class RestoredReplicaProbe(unittest.IsolatedAsyncioTestCase):
    async def test_old_replica_accepts_preforget_stamp_and_releases_stale_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            live_path = Path(tmp) / "live.db"
            replica_path = Path(tmp) / "replica.db"
            verify = lambda *args: True
            live = MemoryStore.bootstrap_offline(live_path, verify, environment="synthetic")
            staged = live.stage(request_id="s1", key="subject", value="synthetic-secret",
                                origin="direct", source_ref="fixture")
            item = live.promote(staged.id, staged.version, proof="synthetic")
            # A point-in-time backup stands for an independently opened stale replica.
            with sqlite3.connect(live_path) as src, sqlite3.connect(replica_path) as dest:
                src.backup(dest)
            replica = MemoryStore.open_offline(replica_path, verify, environment="synthetic")
            _, old_stamp = live.snapshot_with_stamp(["subject"])
            self.assertTrue(replica.is_current_stamp(old_stamp))
            captured, resume = asyncio.Event(), asyncio.Event()

            async def pause():
                captured.set()
                await resume.wait()

            probe = SnapshotProbe(live, after_snapshot=pause)
            task = asyncio.create_task(retrieve_turn(probe, "subject", consequential=False))
            await asyncio.wait_for(captured.wait(), timeout=1)
            live.forget(item.id, item.version, proof="synthetic")
            # Control: authoritative store detects the intervening mutation.
            self.assertFalse(live.is_current_stamp(old_stamp))
            # Simulate a failover that mistakenly accepts the unanchored backup.
            probe.store = replica
            resume.set()
            result = await task
            self.assertEqual((result.status, result.blocked), ("complete", False))
            self.assertEqual([x.text for x in result.items], ["synthetic-secret"])
            self.assertTrue(replica.is_current_stamp(old_stamp))
            replica.close()
            live.close()


if __name__ == "__main__":
    unittest.main()
