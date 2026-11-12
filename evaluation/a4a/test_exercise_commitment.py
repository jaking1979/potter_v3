import unittest
from evaluation.a4a.exercise_commitment import exercise_digest, verify_exercise


class ExerciseCommitmentTests(unittest.TestCase):
    def setUp(self):
        self.plan = {f'case-{i}': i < 7 for i in range(20)}
        self.fixtures = {key: key.encode() for key in self.plan}
        self.digest = exercise_digest(self.plan, self.fixtures)

    def test_reordered_inputs_are_equivalent(self):
        self.assertTrue(verify_exercise(dict(reversed(list(self.plan.items()))),
                        dict(reversed(list(self.fixtures.items()))), self.digest))

    def test_missing_extra_or_changed_fixture_refused(self):
        for mode in ('missing', 'extra', 'changed'):
            data = self.fixtures.copy()
            if mode == 'missing': del data['case-0']
            if mode == 'extra': data['new-case'] = b'extra'
            if mode == 'changed': data['case-0'] = b'different'
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                verify_exercise(self.plan, data, self.digest)

    def test_changed_responsibility_refused(self):
        plan = self.plan.copy()
        plan['case-0'] = False
        with self.assertRaises(ValueError):
            verify_exercise(plan, self.fixtures, self.digest)

    def test_changed_rule_version_refuses_existing_digest(self):
        from unittest.mock import patch
        import evaluation.a4a.exercise_commitment as mod
        other = dict(mod._RULES, version='synthetic-grader-pair/v2')
        with patch.object(mod, '_RULES', other):
            self.assertNotEqual(exercise_digest(self.plan, self.fixtures), self.digest)
            with self.assertRaises(ValueError):
                verify_exercise(self.plan, self.fixtures, self.digest)

    def test_invalid_inputs_refused(self):
        for plan, data, digest in ((self.plan, self.fixtures, 'x'*64),
                                   (self.plan, None, self.digest),
                                   ({'a': True}, {'a': b'a'}, self.digest)):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                verify_exercise(plan, data, digest)


if __name__ == '__main__':
    unittest.main()
