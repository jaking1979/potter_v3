import unittest

from retrieval.core import Evidence, FakeBackend, benchmark


class BenchmarkInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_excess_stream_is_bounded_and_refused(self):
        consumed = []
        def unbounded():
            while True:
                consumed.append(1)
                if len(consumed) > 4:
                    raise AssertionError('unbounded input consumption')
                yield str(len(consumed))
        with self.assertRaises(ValueError):
            await benchmark(FakeBackend(), 'q', unbounded())
        self.assertEqual(len(consumed), 0)

    async def test_invalid_values_refused_and_valid_set_scores(self):
        for values in (None, 'ab', b'ab', [['unhashable']], ['a', 'a'], ['']):
            with self.subTest(values=values), self.assertRaises(ValueError):
                await benchmark(FakeBackend(), 'q', values)
        result = await benchmark(FakeBackend([Evidence('a', 'text')]), 'q', ['a'])
        self.assertTrue(result.hit)
        result = await benchmark(FakeBackend([Evidence('a', 'text')]), 'q', ('a',))
        self.assertTrue(result.hit)

