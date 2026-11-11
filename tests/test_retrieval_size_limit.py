import unittest

from retrieval.core import Evidence, FakeBackend, benchmark, retrieve_turn
from retrieval.isolated import MAX_ID_CHARS, MAX_TEXT_CHARS


class EvidenceSizeLimits(unittest.IsolatedAsyncioTestCase):
    async def test_exact_limit_accepted_by_turn_and_benchmark(self):
        evidence = Evidence('i' * MAX_ID_CHARS, 't' * MAX_TEXT_CHARS)
        result = await retrieve_turn(FakeBackend([evidence]), 'q', consequential=True)
        self.assertEqual((result.status, result.items, result.blocked),
                         ('complete', (evidence,), True))
        report = await benchmark(FakeBackend([evidence]), 'q', [evidence.item_id])
        self.assertTrue(report.hit)

    async def test_oversized_field_refused_even_after_top_three(self):
        for oversized in (Evidence('i' * (MAX_ID_CHARS + 1), 't'),
                          Evidence('i', 't' * (MAX_TEXT_CHARS + 1))):
            items = [Evidence('a', '1'), Evidence('b', '2'), Evidence('c', '3'), oversized]
            result = await retrieve_turn(FakeBackend(items), 'q', consequential=True)
            self.assertEqual((result.status, result.items, result.blocked),
                             ('unavailable', (), True))
            report = await benchmark(FakeBackend(items), 'q', ['a'])
            self.assertEqual((report.status, report.top_ids, report.hit),
                             ('unavailable', (), False))

