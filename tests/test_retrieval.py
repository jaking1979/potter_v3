import asyncio
import unittest

from retrieval.core import Evidence, FakeBackend, benchmark, retrieve_turn


class RetrievalContract(unittest.IsolatedAsyncioTestCase):
    async def test_successful_retrieval_does_not_grant_consequential_authority(self):
        r = await retrieve_turn(FakeBackend([Evidence('a', 'synthetic')]), 'q', consequential=True)
        self.assertEqual((r.status, r.blocked), ('complete', True))

    async def test_outage_advisory_and_consequential(self):
        b = FakeBackend(error=True)
        advisory = await retrieve_turn(b, 'q', consequential=False)
        blocked = await retrieve_turn(b, 'q', consequential=True)
        self.assertEqual((advisory.status, advisory.blocked, advisory.items),
                         ('unavailable', False, ()))
        self.assertEqual((blocked.status, blocked.blocked), ('unavailable', True))

    async def test_late_correct_answer_never_enters_turn_but_scores_in_benchmark(self):
        b = FakeBackend([Evidence('a', 'synthetic')], delay=.04)
        turn = await retrieve_turn(b, 'q', consequential=True, timeout=.01)
        scored = await benchmark(b, 'q', ['a'], production_deadline=.01, timeout=.2)
        self.assertEqual((turn.status, turn.blocked, turn.items), ('deadline', True, ()))
        self.assertTrue(scored.hit)
        self.assertFalse(scored.production_deadline_met)
        self.assertEqual(scored.top_ids, ('a',))

    async def test_evidence_set_requires_every_item_within_top_three(self):
        b = FakeBackend([Evidence('a', ''), Evidence('x', ''), Evidence('y', ''), Evidence('b', '')])
        scored = await benchmark(b, 'q', ['a', 'b'])
        self.assertFalse(scored.hit)
        self.assertEqual(scored.top_ids, ('a', 'x', 'y'))

    async def test_backend_timeout_is_failure_and_miss(self):
        result = await benchmark(FakeBackend(delay=.05), 'q', ['a'], timeout=.005)
        self.assertEqual((result.status, result.hit, result.production_deadline_met),
                         ('deadline', False, False))

    async def test_invalid_evidence_and_empty_ground_truth_fail_closed(self):
        with self.assertRaises(ValueError):
            await benchmark(FakeBackend(), 'q', [])
        r = await retrieve_turn(FakeBackend(['not evidence']), 'q', consequential=True)
        self.assertEqual((r.status, r.blocked), ('unavailable', True))

    async def test_cancel_resistant_backend_cannot_add_late_evidence(self):
        class Resistant:
            async def search(self, query):
                try:
                    await asyncio.sleep(.04)
                except asyncio.CancelledError:
                    await asyncio.sleep(.02)
                return [Evidence('late', 'not injected')]
        result = await retrieve_turn(Resistant(), 'q', consequential=False, timeout=.005)
        await asyncio.sleep(.04)
        self.assertEqual((result.status, result.items), ('deadline', ()))


if __name__ == '__main__':
    unittest.main()
