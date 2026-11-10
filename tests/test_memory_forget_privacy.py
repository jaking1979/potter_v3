import hashlib
import json
import unittest

from memory import MemoryStore, Conflict


class ForgetPrivacyTests(unittest.TestCase):
    def test_all_revisions_remove_content_and_burn_retry_ids(self):
        store = MemoryStore(lambda op, proof, payload: proof == ('test', op))
        key = 'health.synthetic-diagnosis'
        source = 'synthetic:medical-message'
        first = dict(request_id='first', key=key, value='synthetic condition A',
                     origin='external', source_ref=source)
        second = dict(request_id='second', key=key, value='synthetic condition B',
                      origin='external', source_ref=source)
        item = store.stage(**first)
        store.stage(**second)
        store.forget(item.id, item.version, ('test', 'forget'))

        # The complete SQLite logical contents must not carry either fact,
        # provenance, key, or the old unkeyed payload confirmation hash.
        dump = '\n'.join(store._db.iterdump())
        for secret in (key, source, first['value'], second['value']):
            self.assertNotIn(secret, dump)
        for request in (first, second):
            payload = dict(key=request['key'], value=request['value'],
                           origin=request['origin'], source_ref=request['source_ref'],
                           sensitivity='normal', confidence='unknown')
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True,
                separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            self.assertNotIn(digest, dump)
            errors = []
            for value in (request['value'], 'incorrect guess'):
                with self.assertRaises(Conflict) as caught:
                    store.stage(**{**request, 'value': value})
                errors.append(str(caught.exception))
            self.assertEqual(errors, ['request ID unavailable'] * 2)


if __name__ == '__main__':
    unittest.main()
