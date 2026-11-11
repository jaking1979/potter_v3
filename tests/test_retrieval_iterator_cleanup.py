import asyncio
import time
import unittest

from retrieval.core import Evidence, MAX_ASYNC_RESULTS, retrieve_turn


class IteratorCleanup(unittest.IsolatedAsyncioTestCase):
    async def test_cap_explicitly_closes_retained_generator(self):
        closed = []
        def rows():
            try:
                while True:
                    yield Evidence('id', 'text')
            finally:
                closed.append(True)
        retained = rows()
        class Backend:
            async def search(self, query):
                return retained
        result = await retrieve_turn(Backend(), 'q', consequential=True)
        self.assertEqual((result.status, result.items, result.blocked), ('unavailable', (), True))
        self.assertEqual(closed, [True])
        self.assertIsNone(retained.gi_frame)

    async def test_deadline_closes_retained_generator(self):
        closed = []
        def rows():
            try:
                time.sleep(.04)
                yield Evidence('id', 'text')
            finally:
                closed.append(True)
        retained = rows()
        class Backend:
            async def search(self, query):
                return retained
        result = await retrieve_turn(Backend(), 'q', consequential=True, timeout=.01)
        self.assertEqual((result.status, result.items, result.blocked), ('deadline', (), True))
        self.assertEqual(closed, [True])
        self.assertIsNone(retained.gi_frame)

    async def test_close_failure_does_not_replace_refusal(self):
        class BrokenClose:
            def __iter__(self):
                return self
            def __next__(self):
                return Evidence('id', 'text')
            def close(self):
                raise RuntimeError('close failed')
        class Backend:
            async def search(self, query):
                return BrokenClose()
        result = await retrieve_turn(Backend(), 'q', consequential=True)
        self.assertEqual((result.status, result.items, result.blocked), ('unavailable', (), True))

    async def test_caller_cancellation_still_propagates(self):
        started = asyncio.Event()
        class Backend:
            async def search(self, query):
                started.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(retrieve_turn(Backend(), 'q', consequential=True))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
