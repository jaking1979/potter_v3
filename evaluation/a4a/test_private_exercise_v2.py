import unittest

from evaluation.a4a.private_exercise_v2 import private_exercise_digest_v2, verify_private_exercise_v2
from evaluation.a4a.private_exercise_commitment import private_exercise_digest


class PrivateExerciseV2(unittest.TestCase):
    def setUp(self):
        self.plan = {f'case-{i:02}': i % 2 == 0 for i in range(20)}
        self.fixtures = {case: b'synthetic' for case in self.plan}
        self.key = b'k' * 32

    def digest(self, plan=None, fixtures=None, key=None):
        return private_exercise_digest_v2(self.plan if plan is None else plan,
                                          self.fixtures if fixtures is None else fixtures,
                                          self.key if key is None else key)

    def test_order_and_schema_separation(self):
        expected = self.digest()
        self.assertEqual(expected, self.digest(dict(reversed(list(self.plan.items()))),
                                               dict(reversed(list(self.fixtures.items())))))
        self.assertNotEqual(expected, private_exercise_digest(self.plan, self.fixtures, self.key))
        self.assertTrue(verify_private_exercise_v2(self.plan, self.fixtures, self.key, expected))

    def test_plan_fixture_key_and_coverage(self):
        expected = self.digest()
        altered = dict(self.plan)
        altered['case-00'] = not altered['case-00']
        data = dict(self.fixtures)
        data['case-00'] = b'other'
        self.assertNotEqual(expected, self.digest(altered))
        self.assertNotEqual(expected, self.digest(fixtures=data))
        self.assertNotEqual(expected, self.digest(key=b'z' * 32))
        with self.assertRaises(ValueError):
            self.digest(fixtures={})
        with self.assertRaises(ValueError):
            verify_private_exercise_v2(altered, self.fixtures, self.key, expected)

    def test_invalid_unicode_is_refused_by_fixture_boundary(self):
        plan = dict(self.plan)
        data = dict(self.fixtures)
        plan['case-\ud800'] = True
        data['case-\ud800'] = b'synthetic'
        with self.assertRaises(ValueError):
            self.digest(plan, data)

    def test_invalid_digest_and_key(self):
        with self.assertRaises(ValueError):
            self.digest(key=b'bad')
        with self.assertRaises(ValueError):
            verify_private_exercise_v2(self.plan, self.fixtures, self.key, 'not-a-hash')


if __name__ == '__main__':
    unittest.main()
