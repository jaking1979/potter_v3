"""Synthetic-only tests for keyed fixture commitment."""
import hashlib
import unittest

from evaluation.a4a.private_fixture_commitment import private_fixture_tag, verify_private_fixtures


class PrivateFixtureCommitmentTests(unittest.TestCase):
    def setUp(self):
        self.key = b'k' * 32
        self.data = {'case-a': b'one', 'case-b': b'two'}

    def test_order_stable_and_returns_only_aggregate(self):
        tag = private_fixture_tag(self.data, self.key)
        self.assertEqual(tag, private_fixture_tag(dict(reversed(list(self.data.items()))), self.key))
        self.assertEqual(len(tag), 64)
        self.assertNotEqual(tag, hashlib.sha256(b'one').hexdigest())
        self.assertTrue(verify_private_fixtures(self.data, self.key, tag))

    def test_changed_key_content_id_and_coverage_refused(self):
        tag = private_fixture_tag(self.data, self.key)
        for data, key in ((self.data, b'x' * 32), ({'case-a': b'ONE', 'case-b': b'two'}, self.key),
                          ({'case-A': b'one', 'case-b': b'two'}, self.key),
                          ({'case-a': b'one'}, self.key)):
            with self.subTest(data=data, key=key), self.assertRaises(ValueError):
                verify_private_fixtures(data, key, tag)

    def test_framing_prevents_ambiguous_concatenation(self):
        self.assertNotEqual(private_fixture_tag({'ab': b'c'}, self.key),
                            private_fixture_tag({'a': b'bc'}, self.key))

    def test_invalid_values_fail_closed(self):
        for data, key in (({}, self.key), ({'': b'x'}, self.key), ({'id': bytearray(b'x')}, self.key),
                          ({'id': b'x'}, b'short'), ({'id': b'x' * (1024 * 1024 + 1)}, self.key),
                          ({'\ud800': b'x'}, self.key)):
            with self.subTest(data_type=type(data), keylen=len(key)), self.assertRaises(ValueError):
                private_fixture_tag(data, key)
        with self.assertRaises(ValueError):
            verify_private_fixtures(self.data, self.key, 'bad')


if __name__ == '__main__':
    unittest.main()
