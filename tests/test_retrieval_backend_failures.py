import asyncio
import unittest

from retrieval.core import benchmark, retrieve_turn


class BackendFailures(unittest.IsolatedAsyncioTestCase):
    async def test_synchronous_adapter_failure_is_unavailable(self):
        class Broken:
            def search(self, query):
                raise RuntimeError('synthetic setup failure')

        result = await retrieve_turn(Broken(), 'q', consequential=True)
        self.assertEqual((result.status, result.items, result.blocked),
                         ('unavailable', (), True))
        scored = await benchmark(Broken(), 'q', ['id'])
        self.assertEqual((scored.status, scored.top_ids, scored.hit),
                         ('unavailable', (), False))

    async def test_non_awaitable_result_is_unavailable(self):
        class Broken:
            def search(self, query):
                return []

        result = await retrieve_turn(Broken(), 'q', consequential=True)
        self.assertEqual((result.status, result.items, result.blocked),
                         ('unavailable', (), True))

    async def test_task_self_cancel_is_unavailable(self):
        class SelfCancel:
            async def search(self, query):
                asyncio.current_task().cancel()
                await asyncio.sleep(0)

        result = await retrieve_turn(SelfCancel(), 'q', consequential=True)
        self.assertEqual((result.status, result.items, result.blocked),
                         ('unavailable', (), True))
        scored = await benchmark(SelfCancel(), 'q', ['id'])
        self.assertEqual((scored.status, scored.top_ids, scored.hit),
                         ('unavailable', (), False))

    async def test_external_cancel_still_propagates(self):
        class Slow:
            async def search(self, query):
                await asyncio.sleep(30)

        task = asyncio.create_task(retrieve_turn(Slow(), 'q', consequential=True))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
