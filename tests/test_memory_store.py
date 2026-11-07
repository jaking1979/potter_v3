import threading
import unittest

from memory import MemoryStore, Denied, Conflict, Invalid


class MemoryTests(unittest.TestCase):
    def setUp(self):
        # A deliberately fake verifier. No authority or channel is implied.
        self.store = MemoryStore(lambda op, token, payload: token == ('test', op))

    def stage(self, value='one', origin='external', request_id='req', **kwargs):
        return self.store.stage(request_id=request_id, key='preference', value=value,
                                origin=origin, source_ref='synthetic', **kwargs)

    def promote(self, item):
        return self.store.promote(item.id, item.version, ('test', 'promote'))

    def test_untrusted_stays_staged_and_tier_filters_it(self):
        x = self.stage(value='attacker instruction')
        advisory = self.store.retrieve('preference', consequential=False)
        self.assertEqual(advisory.provisional[0].id, x.id)
        consequential = self.store.retrieve('preference', consequential=True)
        self.assertEqual(consequential.provisional, ())
        self.assertEqual(consequential.excluded_ids, (x.id,))
        with self.assertRaises(Denied):
            self.store.promote(x.id, 1, 'forged')

    def test_direct_claim_requires_authentication_and_sensitive_batch_denied(self):
        with self.assertRaises(Denied):
            self.stage(origin='direct')
        x = self.stage(origin='direct', direct_proof=('test', 'direct_statement'),
                       sensitivity='sensitive')
        self.assertEqual(self.store.retrieve('preference', consequential=True).provisional[0].id, x.id)
        with self.assertRaises(Denied):
            self.store.promote(x.id, 1, ('test', 'promote'), batch=True)

    def test_correction_and_undo_respect_versions(self):
        a = self.promote(self.stage())
        b = self.stage('two', request_id='b')
        self.assertEqual(b.contradicts, a.id)
        b = self.promote(b)
        self.assertEqual(self.store.retrieve('preference', consequential=True).active[0].value, 'two')
        restored = self.store.undo(b.id, b.version, ('test', 'undo'))
        self.assertEqual(restored.value, 'one')
        with self.assertRaises(Conflict):
            self.store.undo(b.id, b.version, ('test', 'undo'))

    def test_competing_corrections_cannot_overwrite_unseen_value(self):
        self.promote(self.stage())
        b = self.stage('two', request_id='b')
        c = self.stage('three', request_id='c')
        self.promote(b)
        with self.assertRaises(Conflict):
            self.promote(c)
        self.assertEqual(self.store.retrieve('preference', consequential=False).active[0].value, 'two')

    def test_concurrent_promotion_single_winner(self):
        x = self.stage()
        results = []
        def work():
            try:
                self.promote(x)
                results.append('ok')
            except Conflict:
                results.append('conflict')
        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(results.count('ok'), 1)
        self.assertEqual(results.count('conflict'), 7)

    def test_idempotency_rejects_changed_payload_and_forget_hides_value(self):
        a = self.stage()
        self.assertEqual(self.stage().id, a.id)
        with self.assertRaises(Conflict):
            self.stage('different')
        a = self.promote(a)
        self.store.forget(a.id, a.version, ('test', 'forget'))
        self.assertEqual(self.store.retrieve('preference', consequential=False).active, ())
        with self.assertRaises(Conflict):
            self.store.forget(a.id, a.version, ('test', 'forget'))

    def test_tier_must_be_explicit(self):
        with self.assertRaises(Invalid):
            self.store.retrieve('preference', consequential=None)


if __name__ == '__main__':
    unittest.main()
