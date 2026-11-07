import copy
import json
import unittest
from unittest.mock import patch

from test_e4 import fixture, NOW
from experiments.e4.home import SECTIONS
from experiments.e4.transport import (
    load_snapshot, project_json, respond_json, MAX_BYTES,
)


class TransportTests(unittest.TestCase):
    def unavailable(self, payload):
        view = project_json(payload, now=NOW)
        self.assertTrue(all(s['availability'] == 'unavailable'
                            for s in view['sections'].values()))

    def test_all_six_sections_transport_and_render(self):
        payload = json.dumps(fixture()).encode()
        view = project_json(payload, now=NOW)
        self.assertEqual(set(view['sections']), set(SECTIONS))
        self.assertTrue(all(s['availability'] == 'available'
                            for s in view['sections'].values()))
        self.assertEqual(respond_json('GET', '/', payload, now=NOW)[2].count('<h2 '), 6)

    def test_duplicate_keys_rejected_at_every_level(self):
        for payload in (
            b'{"schema":"wrong","schema":"potter.e4a-snapshot/v0"}',
            b'{"x":{"id":"a","id":"b"}}',
            b'{"x":1,"\\u0078":2}',
        ):
            self.assertIsNone(load_snapshot(payload))
            self.unavailable(payload)

    def test_nonfinite_and_extreme_numeric_tokens_refused(self):
        for token in ('NaN', 'Infinity', '-Infinity', '1e10000', '1' * 65):
            payload = ('{"x":'+token+'}').encode()
            self.assertIsNone(load_snapshot(payload))

    def test_invalid_utf8_surrogate_and_malformed_never_reflected(self):
        for payload in (b'\xff', b'{"x":"\\ud800"}', b'{"CANARY_SECRET":',
                        b'{} trailing', b'\xef\xbb\xbf{}', b'[]', b'null'):
            self.unavailable(payload)
            body = respond_json('GET', '/', payload, now=NOW)[2]
            self.assertNotIn('CANARY_SECRET', body)
            self.assertNotIn('Unicode', body)

    def test_depth_bytes_and_string_bounds(self):
        for payload in (b'{"x":'+b'['*10000+b'0'+b']'*10000+b'}',
                        b' ' * (MAX_BYTES + 1),
                        json.dumps({'x':'a'*4097}).encode()):
            self.assertIsNone(load_snapshot(payload))
        # Quotes and escapes inside a string must not count as nesting.
        value = {'x': '[{'*100+'"\\]'}
        self.assertEqual(load_snapshot(json.dumps(value).encode()), value)
        self.assertEqual(load_snapshot(b'{}'+b' '*(MAX_BYTES-2)), {})

    def test_detached_plain_values_and_fresh_failure_state(self):
        payload = json.dumps(fixture()).encode()
        first = load_snapshot(payload)
        first['tasks']['items'].clear()
        self.assertEqual(len(load_snapshot(payload)['tasks']['items']), 1)
        project_json(payload, now=NOW)
        self.unavailable(b'not json')
        for payload in ('{}', bytearray(b'{}'), memoryview(b'{}'), None):
            self.assertIsNone(load_snapshot(payload))

    def test_each_invalid_section_preserves_other_five_sections(self):
        for name in SECTIONS:
            snapshot = fixture()
            snapshot[name]['items'][0]['status'] = 'CANARY_SECRET'
            view = project_json(json.dumps(snapshot).encode(), now=NOW)
            self.assertEqual(view['sections'][name]['availability'], 'unavailable')
            self.assertEqual(sum(s['availability']=='available'
                                 for s in view['sections'].values()), 5)
            self.assertNotIn('CANARY_SECRET', json.dumps(view))

    def test_action_refusal_precedes_parse_or_clock(self):
        with patch('experiments.e4.transport.load_snapshot', side_effect=AssertionError('must not parse')):
            self.assertEqual(respond_json('POST', '/', object(), now=None)[0], 405)
            self.assertEqual(respond_json('GET', '/stop', object(), now=None)[0], 404)

    def test_input_extras_redacted_after_transport(self):
        snapshot = fixture()
        snapshot['secret'] = {'token':'CANARY_SECRET'}
        snapshot['health']['items'][0]['payload'] = 'CANARY_SECRET'
        payload = json.dumps(snapshot).encode()
        for path in ('/', '/api/status'):
            code, headers, body = respond_json('GET', path, payload, now=NOW)
            self.assertEqual(code, 200)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertNotIn('CANARY_SECRET', body)


if __name__ == '__main__':
    unittest.main()
