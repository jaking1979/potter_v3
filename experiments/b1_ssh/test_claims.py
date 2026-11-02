import unittest

from claims import SCHEMA, attestation_bytes


def fixture():
    expected = {
        'commit': 'a' * 40,
        'tree': 'b' * 40,
        'manifest': 'c' * 64,
        'evidence': 'd' * 64,
    }
    claims = {
        **expected,
        'schema': SCHEMA,
        'role': 'reviewer',
        'verdict': 'approve',
        'nonce': 'e' * 64,
        'expires_at': 101,
    }
    return claims, expected


class AttestationEncodingTests(unittest.TestCase):
    def test_encoding_is_order_independent(self):
        claims, expected = fixture()
        self.assertEqual(
            attestation_bytes(claims, expected, 100, 60),
            attestation_bytes(dict(reversed(list(claims.items()))), expected, 100, 60),
        )

    def test_each_candidate_change_is_refused(self):
        claims, expected = fixture()
        for key in expected:
            wrong = dict(expected)
            wrong[key] = 'f' * len(expected[key])
            with self.subTest(key=key), self.assertRaises(ValueError):
                attestation_bytes(claims, wrong, 100, 60)

    def test_expiry_has_consumer_bound_lifetime(self):
        claims, expected = fixture()
        self.assertEqual(
            attestation_bytes(claims, expected, 100, 1),
            attestation_bytes(claims, expected, 100, 1),
        )
        for expiry in (100, 99, 102, True, '101'):
            with self.subTest(expiry=expiry), self.assertRaises(ValueError):
                attestation_bytes({**claims, 'expires_at': expiry}, expected, 100, 1)
        for lifetime in (0, -1, True, '1'):
            with self.subTest(lifetime=lifetime), self.assertRaises(ValueError):
                attestation_bytes(claims, expected, 100, lifetime)

    def test_schema_and_change_request_are_signed_values(self):
        claims, expected = fixture()
        change_request = {**claims, 'verdict': 'request_changes'}
        self.assertIn(b'"verdict":"request_changes"', attestation_bytes(
            change_request, expected, 100, 60,
        ))
        for update in (
            {'schema': 'potter.attestation/v1'},
            {'role': 'other'},
            {'verdict': 'reject'},
            {'nonce': 'xyz'},
            {'commit': 'A' * 40},
            {'extra': 'x'},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                attestation_bytes({**claims, **update}, expected, 100, 60)
        with self.assertRaises(ValueError):
            attestation_bytes(claims, {}, 100, 60)


if __name__ == '__main__':
    unittest.main()
