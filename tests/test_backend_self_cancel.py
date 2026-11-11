import asyncio
import unittest

from retrieval.core import benchmark, retrieve_turn


class SelfCancellingBackend:
    async def search(self, query):
        raise asyncio.CancelledError


class SelfCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_self_cancel_is_unavailable(self):
        result = await retrieve_turn(SelfCancellingBackend(), 'q', consequential=True)
        self.assertEqual(result.status, 'unavailable')
        self.assertTrue(result.blocked)
        self.assertEqual(result.items, ())
        result = await benchmark(SelfCancellingBackend(), 'q', ['id'])
        self.assertEqual(result.status, 'unavailable')
        self.assertFalse(result.hit)

    async def test_caller_cancellation_still_propagates(self):
        class Slow:
            async def search(self, query):
                await asyncio.sleep(30)
        task = asyncio.create_task(retrieve_turn(Slow(), 'q', consequential=False))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
