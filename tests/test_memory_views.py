import threading
import unittest
from memory import MemoryStore, Invalid
from memory.views import LocalView


class ViewsTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(lambda op, proof, payload: proof == op)
        self.addCleanup(self.store.close)
        self.view = LocalView(self.store)

    def stage(self, value, request='a', key='k', origin='external'):
        return self.store.stage(request_id=request, key=key, value=value,
            origin=origin, source_ref='synthetic', direct_proof='direct_statement')

    def promote(self, item):
        return self.store.promote(item.id, item.version, 'promote')

    def test_superseded_index_never_returns_old_value(self):
        self.promote(self.stage('old'))
        self.view.rebuild(['k'], now=1)
        b = self.promote(self.stage('new', 'b'))
        result = self.view.retrieve('k', consequential=True, now=2)
        self.assertEqual(result.retrieval.active, (b,))
        self.assertTrue(result.index_stale)
        self.assertEqual(result.source, 'canonical_fallback')

    def test_forget_removes_values_from_reads_before_rebuild(self):
        a = self.promote(self.stage('private'))
        self.view.rebuild(['k'], now=1)
        self.store.forget(a.id, a.version, 'forget')
        for tier in (False, True):
            result = self.view.retrieve('k', consequential=tier, now=2)
            self.assertFalse(result.retrieval.active + result.retrieval.provisional)
            self.assertTrue(result.index_stale)
        self.assertNotIn('private', repr(self.view._entries))

    def test_failed_rebuild_retains_complete_prior_generation(self):
        self.stage('one')
        self.view.rebuild(['k'], now=1)
        original = self.store.snapshot
        def fail(keys):
            raise RuntimeError('synthetic rebuild failure')
        self.store.snapshot = fail
        with self.assertRaises(RuntimeError):
            self.view.rebuild(['k', 'other'], now=2)
        self.store.snapshot = original
        result = self.view.retrieve('k', consequential=False, now=3)
        self.assertEqual(result.generation, 1)
        self.assertTrue(result.index_stale)
        self.assertNotIn('other', self.view._entries)
        self.assertEqual(self.view.rebuild(['k', 'other'], now=4), 2)
        self.assertFalse(self.view.retrieve('k', consequential=False, now=4).index_stale)

    def test_reader_sees_old_generation_until_complete_publication(self):
        self.stage('one')
        self.view.rebuild(['k'], now=1)
        entered, resume = threading.Event(), threading.Event()
        original = self.store.snapshot
        def pause(keys):
            snapshot = original(keys)
            entered.set()
            if not resume.wait(3):
                raise RuntimeError('test timed out')
            return snapshot
        self.store.snapshot = pause
        errors = []
        def work():
            try:
                self.view.rebuild(['k', 'other'], now=2)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=work)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            result = self.view.retrieve('k', consequential=False, now=2)
            self.assertEqual(result.generation, 1)
            self.assertNotIn('other', self.view._entries)
        finally:
            resume.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(self.view.retrieve('k', consequential=False, now=2).generation, 2)

    def test_untrusted_staging_stays_excluded_on_index_hit_and_miss(self):
        item = self.stage('injection')
        for built in (False, True):
            if built:
                self.view.rebuild(['k'], now=1)
            result = self.view.retrieve('k', consequential=True, now=2)
            self.assertFalse(result.retrieval.provisional)
            self.assertEqual(result.retrieval.excluded_ids, (item.id,))

    def test_outage_never_serves_cached_content(self):
        self.stage('one')
        self.view.rebuild(['k'], now=1)
        self.store.close()
        with self.assertRaises(Invalid):
            self.view.retrieve('k', consequential=True, now=2)

    def test_clock_staleness_boundaries_and_invalid_inputs(self):
        self.stage('one')
        self.view.rebuild(['k'], now=100)
        self.assertFalse(self.view.retrieve('k', consequential=False, now=86500).index_stale)
        for now in (99, 86500.001):
            self.assertTrue(self.view.retrieve('k', consequential=False, now=now).index_stale)
        for now in (True, float('nan'), float('inf'), -1, '1', 10**1000):
            with self.subTest(now=now), self.assertRaises(Invalid):
                self.view.rebuild(['k'], now=now)

    def test_bounded_snapshot_and_detached_results(self):
        self.stage('one')
        snap = self.store.snapshot(['k'])
        snap.clear()
        self.assertEqual(self.store.retrieve('k', consequential=False).provisional[0].value, 'one')
        for keys in ('k', ['k', 'k'], ['k'] * 1001, [None]):
            with self.subTest(keys=keys), self.assertRaises(Invalid):
                self.store.snapshot(keys)
        with self.assertRaises(Invalid):
            self.view.retrieve('k', consequential=1, now=1)


if __name__ == '__main__':
    unittest.main()
