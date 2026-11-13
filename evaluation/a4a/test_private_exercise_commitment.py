import unittest

from evaluation.a4a import private_exercise_commitment as ex


class PrivateExerciseCommitmentTests(unittest.TestCase):
    def setUp(self):
        self.plan = {f'case{i}': bool(i % 2) for i in range(20)}
        self.fixtures = {case: b'example' for case in self.plan}
        self.key = b'A' * 32  # synthetic fixed test key only

    def test_reorder_and_verification(self):
        tag = ex.private_exercise_digest(self.plan, self.fixtures, self.key)
        self.assertTrue(ex.verify_private_exercise(dict(reversed(list(self.plan.items()))),
                                                   dict(reversed(list(self.fixtures.items()))),
                                                   self.key, tag))

    def test_key_fixture_and_plan_changes_refused(self):
        tag = ex.private_exercise_digest(self.plan, self.fixtures, self.key)
        variants = [(self.plan, self.fixtures, b'B' * 32),
                    (self.plan, {**self.fixtures, 'case0': b'changed'}, self.key),
                    ({**self.plan, 'case0': True}, self.fixtures, self.key)]
        for plan, fixtures, key in variants:
            with self.subTest(plan=plan, key=key):
                with self.assertRaises(ValueError):
                    ex.verify_private_exercise(plan, fixtures, key, tag)

    def test_invalid_coverage_and_digest_refused(self):
        with self.assertRaises(ValueError):
            ex.private_exercise_digest(self.plan, {'one': b'x'}, self.key)
        with self.assertRaises(ValueError):
            ex.verify_private_exercise(self.plan, self.fixtures, self.key, 'invalid')

    def test_rules_change_is_bound(self):
        tag = ex.private_exercise_digest(self.plan, self.fixtures, self.key)
        old = ex._RULES
        try:
            ex._RULES = {**old, 'version': 'different'}
            with self.assertRaises(ValueError):
                ex.verify_private_exercise(self.plan, self.fixtures, self.key, tag)
        finally:
            ex._RULES = old


if __name__ == '__main__':
    unittest.main()
