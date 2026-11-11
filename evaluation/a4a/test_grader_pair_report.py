import copy
import unittest

from evaluation.a4a.grader_pair_report import summarize_grader_pairs
from evaluation.a4a.test_case_agreement import record


class GraderPairBridgeTest(unittest.TestCase):
    def setUp(self):
        self.plan = {f'case-{i}': i >= 20 for i in range(27)}
        self.first = [record(k, v) for k, v in self.plan.items()]
        self.second = copy.deepcopy(self.first)

    def test_plan_drives_follow_and_both_outputs_drive_agreement(self):
        self.second[20]['outputs']['B']['done'] = 'no'
        report = summarize_grader_pairs(self.first, self.second, self.plan)
        self.assertEqual(report['status'], 'descriptive_only')
        self.assertEqual(report['dimensions']['done']['agreements'], 26)
        self.assertEqual(report['dimensions']['follow_through']['planned_cases'], 7)
        self.assertEqual(report['dimensions']['follow_through']['agreements'], 7)
        self.assertNotIn('pass', report)

    def test_mismatch_both_directions_and_missing_preserve_reasons(self):
        for index, actual in ((0, True), (20, False)):
            with self.subTest(index=index):
                second = copy.deepcopy(self.second)
                second[index] = record(f'case-{index}', actual)
                result = summarize_grader_pairs(self.first, second, self.plan)
                reason = result['source_reasons'][f'case-{index}']['done']
                self.assertIn('responsibility mismatch', reason)
                self.assertEqual(result['dimensions']['follow_through']['agreements'],
                                 7 if index == 0 else 6)
        result = summarize_grader_pairs(self.first, self.second[1:], self.plan)
        self.assertEqual(result['source_reasons']['case-0']['done'],
                         'missing or duplicate record')
        self.assertEqual(result['dimensions']['follow_through']['agreements'], 7)
        duplicate = self.second + [copy.deepcopy(self.second[20])]
        result = summarize_grader_pairs(self.first, duplicate, self.plan)
        self.assertEqual(result['source_reasons']['case-20']['done'],
                         'missing or duplicate record')
        self.assertEqual(result['dimensions']['follow_through']['agreements'], 6)

    def test_union_invalidation_and_unknown_rejected(self):
        for missing, status in ((2, 'descriptive_only'), (3, 'invalid_rerun')):
            report = summarize_grader_pairs(self.first, self.second[missing:], self.plan)
            self.assertEqual(report['status'], status)
            self.assertEqual(len(report['affected_ids']), missing)
            if missing == 3:
                self.assertNotIn('dimensions', report)
        with self.assertRaises(ValueError):
            summarize_grader_pairs(self.first, self.second + [record('outsider')], self.plan)


if __name__ == '__main__':
    unittest.main()
