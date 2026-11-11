import asyncio
import time
import unittest

from retrieval.core import Evidence, FakeBackend, benchmark, retrieve_turn, MAX_ASYNC_RESULTS


class RetrievalContract(unittest.IsolatedAsyncioTestCase):
    async def test_unbounded_and_oversized_iterables_fail_closed(self):
        class Repeating:
            async def search(self, query):
                def values():
                    while True:
                        yield Evidence('id', 'text')
                return values()
        class Many:
            async def search(self, query):
                return [Evidence(str(i), '') for i in range(MAX_ASYNC_RESULTS + 1)]
        for adapter in (Repeating(), Many()):
            turn = await retrieve_turn(adapter, 'q', consequential=True)
            self.assertEqual((turn.status, turn.items, turn.blocked), ('unavailable', (), True))
            result = await benchmark(adapter, 'q', ['id'])
            self.assertEqual((result.status, result.top_ids, result.hit), ('unavailable', (), False))

    async def test_result_cap_accepts_limit_and_validates_beyond_top_three(self):
        class AtLimit:
            async def search(self, query):
                return [Evidence(str(i), '') for i in range(MAX_ASYNC_RESULTS)]
        class InvalidLater:
            async def search(self, query):
                return [Evidence('1', ''), Evidence('2', ''), Evidence('3', ''), object()]
        result = await retrieve_turn(AtLimit(), 'q', consequential=False)
        self.assertEqual((result.status, len(result.items)), ('complete', 3))
        bad = await retrieve_turn(InvalidLater(), 'q', consequential=False)
        self.assertEqual((bad.status, bad.items), ('unavailable', ()))

    async def test_malformed_evidence_fields_are_unavailable(self):
        for item in (Evidence(['unhashable'], 'text'), Evidence(1, 'text'),
                     Evidence('id', None), Evidence('id', {'body': 'object'})):
            with self.subTest(item=item):
                result = await retrieve_turn(FakeBackend([item]), 'q', consequential=True)
                self.assertEqual((result.status, result.items, result.blocked),
                                 ('unavailable', (), True))
                scored = await benchmark(FakeBackend([item]), 'q', ['id'])
                self.assertEqual((scored.status, scored.top_ids, scored.hit),
                                 ('unavailable', (), False))

    async def test_blocking_backend_cannot_return_late_turn_or_benchmark(self):
        class Blocking:
            async def search(self, query):
                time.sleep(.06)
                return [Evidence('late', 'must not enter turn')]
        turn = await retrieve_turn(Blocking(), 'q', consequential=False, timeout=.01)
        scored = await benchmark(Blocking(), 'q', ['late'], timeout=.01)
        self.assertEqual((turn.status, turn.items), ('deadline', ()))
        self.assertEqual((scored.status, scored.top_ids, scored.hit), ('deadline', (), False))

    async def test_result_materialization_cannot_cross_deadline(self):
        class SlowItems:
            def __iter__(self):
                time.sleep(.06)
                yield Evidence('late', 'must not enter turn')
        class Backend:
            async def search(self, query):
                return SlowItems()
        turn = await retrieve_turn(Backend(), 'q', consequential=False, timeout=.01)
        self.assertEqual((turn.status, turn.items), ('deadline', ()))

    async def test_invalid_durations_rejected_before_backend_work(self):
        class Never:
            async def search(self, query):
                raise AssertionError('invalid duration must not reach backend')
        for value in (float('nan'), float('inf'), -float('inf'), 0, -1,
                      True, '1', None, 10 ** 10000):
            # Avoid formatting the huge integer in unittest's subtest label.
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(ValueError):
                    await retrieve_turn(Never(), 'q', consequential=False, timeout=value)
                with self.assertRaises(ValueError):
                    await benchmark(Never(), 'q', ['a'], timeout=value)
                with self.assertRaises(ValueError):
                    await benchmark(Never(), 'q', ['a'], production_deadline=value)

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

