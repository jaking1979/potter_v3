import unittest

from memory import MemoryStore, Denied, Invalid


class ReviewTests(unittest.TestCase):
    def setUp(self):
        def verify(op, proof, payload):
            return proof == (op, payload.get('snapshot_digest'))
        self.store = MemoryStore(verify)
        self.addCleanup(self.store.close)

    def stage(self, request, value):
        return self.store.stage(request_id=request, key='synthetic', value=value,
                                origin='external', source_ref='synthetic')

    def proof(self, item, op='promote', batch=False):
        packet = self.store.review_snapshot(item.id, item.version, operation=op, batch=batch)
        return op, packet['digest']

    def promote(self, item):
        return self.store.promote(item.id, item.version, self.proof(item))

    def test_exact_proposal_promotes_and_wrong_operation_refused(self):
        item = self.stage('a', 'one')
        with self.assertRaises(Denied):
            self.store.promote(item.id, item.version, ('forget', self.proof(item)[1]))
        self.assertEqual(self.promote(item).status, 'active')

    def test_proof_cannot_move_to_another_same_version_candidate(self):
        a = self.stage('a', 'one')
        b = self.stage('b', 'two')
        with self.assertRaises(Denied):
            self.store.promote(b.id, b.version, self.proof(a))

    def test_changed_content_without_version_change_invalidates_proof(self):
        item = self.stage('a', 'one')
        proof = self.proof(item)
        # Simulate compromised/stale local data; this is not real authentication.
        self.store._db.execute('UPDATE item SET value=? WHERE id=?', ('changed', item.id))
        with self.assertRaises(Denied):
            self.store.promote(item.id, item.version, proof)

    def test_undo_binds_the_value_and_version_to_be_restored(self):
        a = self.promote(self.stage('a', 'one'))
        b = self.promote(self.stage('b', 'two'))
        proof = self.proof(b, 'undo')
        self.store._db.execute('UPDATE item SET value=? WHERE id=?', ('changed-old', a.id))
        with self.assertRaises(Denied):
            self.store.undo(b.id, b.version, proof)

    def test_forget_requires_reapproval_if_deletion_set_grows(self):
        a = self.promote(self.stage('a', 'one'))
        proof = self.proof(a, 'forget')
        self.stage('b', 'two')
        with self.assertRaises(Denied):
            self.store.forget(a.id, a.version, proof)
        self.store.forget(a.id, a.version, self.proof(a, 'forget'))
        self.assertFalse(self.store.retrieve('synthetic', consequential=False).provisional)

    def test_proposal_is_detached_and_batch_flag_is_bound(self):
        item = self.stage('a', 'one')
        packet = self.store.review_snapshot(item.id, item.version, operation='promote')
        packet['snapshot']['item']['value'] = 'caller edit'
        self.assertEqual(self.store._get(item.id).value, 'one')
        self.assertNotEqual(packet['digest'], self.proof(item, batch=True)[1])

    def test_boolean_versions_and_ambiguous_review_modes_refused(self):
        item = self.stage('a', 'one')
        for version in (True, False, 0, '1', 1.0):
            with self.subTest(version=version), self.assertRaises(Invalid):
                self.store.promote(item.id, version, None)
        for operation, batch in (('unknown', False), ('forget', True), ('promote', 1)):
            with self.subTest(operation=operation, batch=batch), self.assertRaises(Invalid):
                self.store.review_snapshot(item.id, item.version, operation=operation, batch=batch)


if __name__ == '__main__':
    unittest.main()
