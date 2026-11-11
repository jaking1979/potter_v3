"""Synthetic D3/D4 experiment, not a delivery or authorization boundary.

An atomic local snapshot and subsequent check can detect an intervening store
mutation. They cannot exclude mutation immediately after the check, invalidate
external copies, prove restore safety, or serialize a network send. Never use
this experiment to release data in a real assistant.
"""
from __future__ import annotations

from retrieval.core import Evidence


class SnapshotProbe:
    def __init__(self, store, *, after_snapshot=None, after_check=None):
        self.store = store
        self.after_snapshot = after_snapshot
        self.after_check = after_check

    async def search(self, key):
        snapshots, stamp = self.store.snapshot_with_stamp([key])
        if self.after_snapshot:
            await self.after_snapshot()
        if not self.store.is_current_stamp(stamp):
            raise ValueError('snapshot invalidated')
        if self.after_check:
            await self.after_check()
        # Only active items; staged material is deliberately excluded.
        return tuple(Evidence(item.id, item.value) for item in snapshots[key].active)
