import copy
import unittest

from evaluation.a4a.exercise_commitment import exercise_digest
from evaluation.a4a.exercise_report import summarize_exercise
from evaluation.a4a.test_case_agreement import record


class ExerciseReportTest(unittest.TestCase):
    def setUp(self):
        self.plan = {f'case-{i}': i >= 20 for i in range(27)}
        self.fixtures = {key: ('private ' + key).encode() for key in self.plan}
        self.digest = exercise_digest(self.plan, self.fixtures)
        self.first = [record(k, v) for k, v in self.plan.items()]
        self.second = copy.deepcopy(self.first)

    def test_report_contains_only_digest_and_preserves_validity(self):
        result = summarize_exercise(self.first, self.second, self.plan, self.fixtures, self.digest)
        self.assertEqual(result['exercise_digest'], self.digest)
        self.assertEqual(result['status'], 'descriptive_only')
        self.assertNotIn('private case', repr(result))
        invalid = summarize_exercise(self.first, self.second[3:], self.plan, self.fixtures, self.digest)
        self.assertEqual(invalid['status'], 'invalid_rerun')
        self.assertNotIn('dimensions', invalid)

    def test_changed_plan_or_fixtures_rejected_before_report(self):
        plan = dict(self.plan)
        plan['case-0'] = True
        fixtures = dict(self.fixtures)
        fixtures['case-0'] = b'modified'
        for p, f, d in ((plan, self.fixtures, self.digest),
                        (self.plan, fixtures, self.digest),
                        (self.plan, self.fixtures, 'bad')):
            with self.subTest(p=p, digest=d):
                with self.assertRaises(ValueError):
                    summarize_exercise(self.first, self.second, p, f, d)

    def test_same_snapshot_used_after_caller_mutates_plan(self):
        from unittest.mock import patch
        import evaluation.a4a.exercise_report as mod
        real = mod.verify_exercise
        def verify_then_mutate(plan, data, digest):
            real(plan, data, digest)
            self.plan['case-0'] = True
            self.fixtures['case-0'] = b'changed'
        with patch.object(mod, 'verify_exercise', side_effect=verify_then_mutate):
            result = summarize_exercise(self.first, self.second, self.plan, self.fixtures, self.digest)
        self.assertEqual(result['exercise_digest'], self.digest)
        self.assertEqual(result['dimensions']['follow_through']['planned_cases'], 7)
