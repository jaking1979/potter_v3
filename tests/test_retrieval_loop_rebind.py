import asyncio
import unittest

from retrieval.admission import BoundedBackend


class LoopRebindTests(unittest.TestCase):
    def test_rebind_after_old_loop_closes_and_no_active_work(self):
        class Backend:
            async def search(self, query):
                return query
        guard = BoundedBackend(Backend())
        self.assertEqual(asyncio.run(guard.search("first")), "first")
        self.assertEqual(asyncio.run(guard.search("second")), "second")

    def test_refuses_other_loop_while_old_loop_is_open(self):
        class Backend:
            async def search(self, query):
                return query
        guard = BoundedBackend(Backend())
        old_loop = asyncio.new_event_loop()
        try:
            self.assertEqual(old_loop.run_until_complete(guard.search("first")), "first")
            with self.assertRaisesRegex(RuntimeError, "another loop"):
                asyncio.run(guard.search("second"))
        finally:
            old_loop.close()
        self.assertEqual(asyncio.run(guard.search("third")), "third")

    def test_refuses_closed_loop_if_active_work_remains(self):
        class Backend:
            async def search(self, query):
                return query
        guard = BoundedBackend(Backend())
        self.assertEqual(asyncio.run(guard.search("first")), "first")
        guard._active = 1  # simulate an orphaned permit; never transfer it
        with self.assertRaisesRegex(RuntimeError, "another loop"):
            asyncio.run(guard.search("second"))
