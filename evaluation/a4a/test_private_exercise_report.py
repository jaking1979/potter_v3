import copy
import unittest
from unittest.mock import patch

from evaluation.a4a.private_exercise_v2 import private_exercise_digest_v2
from evaluation.a4a.private_exercise_report import summarize_private_exercise


def record(case, responsibility):
    answers = {'done': 'yes', 'reexplaining': 'none', 'speed': 'yes', 'trust': 'yes'}
    if responsibility:
        answers['follow_through'] = 'yes'
    return {'case_id': case, 'responsibility': responsibility,
            'outputs': {'A': dict(answers), 'B': dict(answers)}}


class PrivateReportTest(unittest.TestCase):
    def setUp(self):
        self.plan = {f'case-{i}': i >= 20 for i in range(27)}
        self.fixtures = {case: ('synthetic ' + case).encode() for case in self.plan}
        self.key = b'k' * 32  # deterministic synthetic test key only
        self.digest = private_exercise_digest_v2(self.plan, self.fixtures, self.key)
        self.first = [record(case, responsibility) for case, responsibility in self.plan.items()]
        self.second = copy.deepcopy(self.first)

    def test_real_grading_bridge_and_only_keyed_digest(self):
        result = summarize_private_exercise(self.first, self.second, self.plan,
                                            self.fixtures, self.key, self.digest)
        self.assertEqual(result['private_exercise_digest'], self.digest)
        self.assertEqual(result['status'], 'descriptive_only')
        self.assertNotIn('exercise_digest', result)
        self.assertNotIn('synthetic case', repr(result))
        self.assertNotIn(repr(self.key), repr(result))

    def test_wrong_key_changed_fixture_and_plan_fail_before_grading(self):
        changed_plan, changed_fixtures = self.plan.copy(), self.fixtures.copy()
        changed_plan['case-0'] = True
        changed_fixtures['case-0'] = b'changed'
        import evaluation.a4a.private_exercise_report as mod
        with patch.object(mod, 'summarize_grader_pairs') as grading:
            for plan, data, key in ((changed_plan, self.fixtures, self.key),
                                    (self.plan, changed_fixtures, self.key),
                                    (self.plan, self.fixtures, b'x' * 32)):
                with self.assertRaises(ValueError):
                    summarize_private_exercise(self.first, self.second, plan, data, key, self.digest)
            grading.assert_not_called()

    def test_verified_snapshot_is_the_graded_snapshot(self):
        import evaluation.a4a.private_exercise_report as mod
        real = mod.verify_private_exercise_v2
        def verify_then_mutate(plan, data, key, digest):
            real(plan, data, key, digest)
            self.plan['case-0'] = True
            self.fixtures['case-0'] = b'changed'
        with patch.object(mod, 'verify_private_exercise_v2', side_effect=verify_then_mutate):
            result = summarize_private_exercise(self.first, self.second, self.plan,
                                                self.fixtures, self.key, self.digest)
        self.assertEqual(result['dimensions']['follow_through']['planned_cases'], 7)
        self.assertEqual(result['private_exercise_digest'], self.digest)


if __name__ == '__main__':
    unittest.main()
