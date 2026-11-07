import asyncio
import multiprocessing
import time
import unittest

from retrieval.core import benchmark, retrieve_turn
from retrieval.isolated import SyntheticProcessBackend, _decode, bounded_search


class IsolatedRetrievalContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.children_before = {p.pid for p in multiprocessing.active_children()}

    def tearDown(self):
        children = {p.pid for p in multiprocessing.active_children()}
        self.assertEqual(children - self.children_before, set(), 'unreaped worker')

    async def test_real_blocking_does_not_block_parent_or_cross_turn(self):
        ticks = []
        async def heartbeat():
            for _ in range(10):
                await asyncio.sleep(.01)
                ticks.append(time.monotonic())
        beat = asyncio.create_task(heartbeat())
        start = time.monotonic()
        late = await retrieve_turn(SyntheticProcessBackend((('late', 'secret'),),
                                   delay_s=10, mode='blocking'), 'q',
                                   consequential=True, timeout=.2)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertGreater(len(ticks), 5)
        await beat
        self.assertEqual((late.status, late.items, late.blocked), ('deadline', (), True))
        fresh = await retrieve_turn(SyntheticProcessBackend((('new', 'fresh'),)),
                                    'q', consequential=False)
        self.assertEqual(tuple(i.item_id for i in fresh.items), ('new',))
        self.assertEqual(late.items, ())

    async def test_termination_resistant_worker_is_killed_and_reaped(self):
        start = time.monotonic()
        result = await retrieve_turn(SyntheticProcessBackend(delay_s=10,
                                     mode='ignore_termination'), 'q',
                                     consequential=False, timeout=.4)
        self.assertEqual((result.status, result.items), ('deadline', ()))
        self.assertLess(time.monotonic() - start, 1.5)
        # Grace consumed demonstrates the worker actually ignored SIGTERM.
        self.assertGreaterEqual(result.elapsed_s, .44)

    async def test_faults_are_unavailable_and_cleanup_allows_restart(self):
        for mode in ('exception', 'malformed', 'oversized'):
            with self.subTest(mode=mode):
                bad = await retrieve_turn(SyntheticProcessBackend(mode=mode),
                                          'q', consequential=True)
                self.assertEqual((bad.status, bad.items, bad.blocked),
                                 ('unavailable', (), True))
        good = await retrieve_turn(SyntheticProcessBackend((('a', 'ok'),)),
                                   'q', consequential=True)
        self.assertEqual((good.status, good.blocked), ('complete', True))

    async def test_caller_cancellation_reaps_worker_before_propagation(self):
        task = asyncio.create_task(retrieve_turn(SyntheticProcessBackend(delay_s=10),
                                   'q', consequential=True))
        await asyncio.sleep(.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_partial_wire_never_blocks_deadline_or_parent_loop(self):
        for mode in ('partial_header', 'partial_body'):
            ticks = []
            async def heartbeat():
                for _ in range(12):
                    await asyncio.sleep(.01)
                    ticks.append(time.monotonic())
            beat = asyncio.create_task(heartbeat())
            start = time.monotonic()
            result = await retrieve_turn(SyntheticProcessBackend(mode=mode),
                                        'q', consequential=True, timeout=.3)
            await beat
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertEqual((result.status, result.items, result.blocked),
                             ('deadline', (), True))
            self.assertGreater(len(ticks), 8)

    async def test_partial_wire_cancellation_reaps_before_propagation(self):
        task = asyncio.create_task(retrieve_turn(
            SyntheticProcessBackend(mode='partial_body'), 'q', consequential=True))
        await asyncio.sleep(.15)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_closed_truncated_frame_is_unavailable(self):
        result = await retrieve_turn(SyntheticProcessBackend(mode='short_frame'),
                                     'q', consequential=True)
        self.assertEqual((result.status, result.items, result.blocked),
                         ('unavailable', (), True))

    async def test_spawn_revalidates_plain_synthetic_config(self):
        class Custom(SyntheticProcessBackend):
            def __reduce__(self):
                raise AssertionError('must not pickle caller subclass')
        with self.assertRaises(ValueError):
            await bounded_search(Custom(), 'q', 1)
        class CustomMode(str):
            def __reduce__(self):
                raise AssertionError('must not pickle caller field subclass')
        with self.assertRaises(ValueError):
            SyntheticProcessBackend(mode=CustomMode('normal'))
        altered = SyntheticProcessBackend()
        object.__setattr__(altered, 'items', (('x', 'y' * 2049),))
        with self.assertRaises(ValueError):
            await bounded_search(altered, 'q', 1)

    async def test_benchmark_has_separate_deadline_and_returns_only_ids(self):
        backend = SyntheticProcessBackend((('a', 'never returned by benchmark'),), .15)
        missed = await retrieve_turn(backend, 'q', consequential=True, timeout=.05)
        score = await benchmark(backend, 'q', ['a'], production_deadline=.05, timeout=2)
        self.assertEqual((missed.status, missed.items), ('deadline', ()))
        self.assertEqual((score.status, score.hit, score.top_ids,
                          score.production_deadline_met), ('complete', True, ('a',), False))
        self.assertFalse(hasattr(score, 'items'))

    async def test_benchmark_timeout_is_miss(self):
        result = await benchmark(SyntheticProcessBackend(delay_s=10), 'q', ['a'], timeout=.05)
        self.assertEqual((result.status, result.hit, result.top_ids), ('deadline', False, ()))

    async def test_wire_and_request_limits_fail_closed(self):
        for payload in (b'[]', b'{"items":[] ,"extra":true}',
                        b'{"items":[["a",1]]}', b'{"items":[["",""]]}',
                        b'{"items":[["a",""],["b",""],["c",""],["d",""]]}'):
            with self.assertRaises((ValueError, TypeError)):
                _decode(payload)
        for items in ((('a', 'x' * 2049),), (('x' * 257, ''),),
                      (('a', ''),) * 4):
            with self.assertRaises(ValueError):
                SyntheticProcessBackend(items)
        for timeout in (float('nan'), float('inf'), 31):
            with self.assertRaises(ValueError):
                await benchmark(SyntheticProcessBackend(), 'q', ['a'], timeout=timeout)
        with self.assertRaises(ValueError):
            await retrieve_turn(SyntheticProcessBackend(), 'q', consequential=False, timeout=3)
        with self.assertRaises(ValueError):
            await retrieve_turn(SyntheticProcessBackend(), 'q' * 4097, consequential=True)


if __name__ == '__main__':
    unittest.main()
