import asyncio
import unittest

from retrieval.admission import BoundedBackend
from retrieval.core import Evidence, retrieve_turn


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_resistant_worker_keeps_slot_until_done(self):
        started, release = asyncio.Event(), asyncio.Event()
        class Resistant:
            calls = 0
            async def search(self, query):
                self.calls += 1
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                return [Evidence("late", "discard")]
        inner = Resistant()
        guard = BoundedBackend(inner)
        first = asyncio.create_task(retrieve_turn(guard, "a", consequential=True, timeout=.03))
        await started.wait()
        result = await first
        self.assertEqual(result.status, "deadline")
        self.assertTrue(result.blocked)
        second = await retrieve_turn(guard, "b", consequential=True, timeout=.1)
        self.assertEqual(second.status, "unavailable")
        self.assertEqual(inner.calls, 1)
        release.set()
        for _ in range(100):
            if guard._active == 0: break
            await asyncio.sleep(.001)
        self.assertEqual(guard._active, 0)

    async def test_exceptions_release_slot(self):
        class Broken:
            calls = 0
            async def search(self, query):
                self.calls += 1
                raise RuntimeError("not shown")
        backend = Broken()
        guard = BoundedBackend(backend)
        for _ in range(2):
            r = await retrieve_turn(guard, "x", consequential=True, timeout=.1)
            self.assertEqual(r.status, "unavailable")
        self.assertEqual(backend.calls, 2)

    async def test_limit_validation(self):
        for x in (0, -1, True, 33, 1.5):
            with self.assertRaises(ValueError):
                BoundedBackend(object(), x)
