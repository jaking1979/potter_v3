import copy
import unittest

from evaluation.a4a.plan_binding import plan_digest, summarize_bound_pairs
from evaluation.a4a.test_case_agreement import record


class PlanBindingTests(unittest.TestCase):
    def setUp(self):
        self.plan = {f'case-{i}': i >= 20 for i in range(27)}
        self.first = [record(k, v) for k, v in self.plan.items()]
        self.second = copy.deepcopy(self.first)

    def test_order_independent_and_records_plan_identity(self):
        digest = plan_digest(self.plan)
        reversed_plan = dict(reversed(list(self.plan.items())))
        self.assertEqual(digest, plan_digest(reversed_plan))
        result = summarize_bound_pairs(self.first, self.second, reversed_plan, digest)
        self.assertEqual(result['plan_digest'], digest)
        self.assertEqual(result['plan_schema'], 'potter.a4a-case-plan/v1')
        self.assertEqual(result['status'], 'descriptive_only')
        self.assertEqual(result['dimensions']['follow_through']['planned_cases'], 7)
        self.assertNotIn('pass', result)

    def test_flag_and_id_changes_refused(self):
        digest = plan_digest(self.plan)
        for changed in (dict(self.plan, **{'case-0': True}),
                        {('altered' if k == 'case-0' else k): v for k, v in self.plan.items()}):
            self.assertNotEqual(plan_digest(changed), digest)
            with self.assertRaisesRegex(ValueError, 'plan digest mismatch'):
                summarize_bound_pairs(self.first, self.second, changed, digest)

    def test_invalid_plans_and_digests_refused(self):
        for invalid in ({}, {'x': True}, dict(self.plan, **{'case-0': 1}),
                        dict(self.plan, **{' ': True})):
            with self.assertRaises(ValueError):
                plan_digest(invalid)
        for invalid in ('x' * 64, 'A' * 64, '', None):
            with self.assertRaises(ValueError):
                summarize_bound_pairs(self.first, self.second, self.plan, invalid)

    def test_affected_cases_stay_affected(self):
        result = summarize_bound_pairs(self.first, self.second[3:], self.plan,
                                       plan_digest(self.plan))
        self.assertEqual(result['status'], 'invalid_rerun')
        self.assertEqual(len(result['affected_ids']), 3)
        self.assertNotIn('dimensions', result)


if __name__ == '__main__':
    unittest.main()
