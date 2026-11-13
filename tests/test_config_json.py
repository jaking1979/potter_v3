import json
import unittest

from model_broker.broker import Candidate
from model_broker.config_json import MAX_CONFIG_BYTES, decode_candidates


class DecodeCandidates(unittest.TestCase):
    def setUp(self):
        self.item = dict(model="m", provider="p", approved=True,
                         validated_tasks=["general"], privacy_classes=["public"],
                         expected_usd_micros=1, worst_usd_micros=2)

    def test_valid_records_are_immutable_candidates(self):
        value = decode_candidates(json.dumps([self.item]).encode())
        self.assertIs(type(value), tuple)
        self.assertEqual(value[0], Candidate.from_json_config(self.item))

    def test_lone_surrogates_refused_in_all_candidate_labels(self):
        for field in ('model', 'provider', 'validated_tasks', 'privacy_classes'):
            bad = dict(self.item, **{field: ['\ud800'] if field.endswith('s') else '\ud800'})
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, '^malformed_configuration$'):
                    decode_candidates(json.dumps([bad]).encode('utf-8'))
        self.assertEqual(decode_candidates(json.dumps([dict(self.item, model='café')]).encode())[0].model, 'café')

    def test_invalid_bytes_and_limits_are_refused_without_echo(self):
        for raw in (b"x" * (MAX_CONFIG_BYTES + 1), b"\xff", b"NaN", b"{", "[]", bytearray(b"[]")):
            with self.subTest(raw_type=type(raw)):
                with self.assertRaisesRegex(ValueError, "^malformed_configuration$"):
                    decode_candidates(raw)

    def test_duplicate_keys_and_oversized_collections_are_refused(self):
        for raw in (b'[{"model":"a","model":"b"}]', b"[" + b"{}," * 1024 + b"{}]", b"{}"):
            with self.assertRaisesRegex(ValueError, "^malformed_configuration$"):
                decode_candidates(raw)

    def test_malformed_second_candidate_refuses_entire_set(self):
        bad = dict(self.item, privacy_classes=["public", "public"])
        with self.assertRaisesRegex(ValueError, "^malformed_configuration$"):
            decode_candidates(json.dumps([self.item, bad]).encode())
