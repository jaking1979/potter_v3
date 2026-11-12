import unittest
from evaluation.a4a.fixture_commitment import fixture_digest, fixture_manifest, verify_fixtures


class FixtureCommitmentTests(unittest.TestCase):
    def test_order_independent_and_no_raw_bytes(self):
        one = {'case-1': b'secret synthetic bytes', 'case-2': b'other'}
        two = {'case-2': b'other', 'case-1': b'secret synthetic bytes'}
        self.assertEqual(fixture_digest(one), fixture_digest(two))
        self.assertNotIn('secret synthetic bytes', repr(fixture_manifest(one)))
        self.assertTrue(verify_fixtures(two, fixture_digest(one)))

    def test_content_case_and_set_changes_refused(self):
        fixtures = {'a': b'first', 'b': b'second'}
        digest = fixture_digest(fixtures)
        for changed in ({'a': b'First', 'b': b'second'}, {'a': b'first'},
                        {'a': b'first', 'c': b'second'},
                        {'a': b'second', 'b': b'first'}):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError): verify_fixtures(changed, digest)

    def test_invalid_values_and_size_rejected(self):
        for invalid in ({}, {'': b'x'}, {'x': 'text'}, {'x': b'x' * (1024 * 1024 + 1)}):
            with self.subTest(invalid_type=type(invalid)):
                with self.assertRaises(ValueError): fixture_digest(invalid)
        with self.assertRaises(ValueError): verify_fixtures({'a': b'a'}, 'G' * 64)


if __name__ == '__main__':
    unittest.main()
