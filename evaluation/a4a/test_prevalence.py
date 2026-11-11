import copy
import unittest
from evaluation.a4a.prevalence import category_prevalence, synthetic_usable_prevalence


def record(i, responsibility=False):
    row = {'done': 'yes', 'reexplaining': 'none', 'speed': 'yes', 'trust': 'yes'}
    if responsibility:
        row['follow_through'] = 'yes'
    return {'case_id': str(i), 'responsibility': responsibility,
            'outputs': {'A': row.copy(), 'B': row.copy()}}


class PrevalenceTests(unittest.TestCase):
    def test_passes_pooled_and_strict_ninety_percent(self):
        first = [record(i, i == 0) for i in range(5)]
        second = copy.deepcopy(first)
        second[0]['outputs']['A']['done'] = 'no'
        second[0]['outputs']['B']['done'] = 'no'
        result = category_prevalence(first, second)
        self.assertEqual(result['done']['pooled'], {'no': 2, 'partly': 0, 'yes': 18})
        self.assertEqual(result['done']['pass_1']['yes'], 10)
        self.assertEqual(result['done']['pass_2']['no'], 2)
        self.assertFalse(result['done']['over_ninety_percent'])
        self.assertEqual(result['follow_through']['pooled_total'], 4)
        second[0]['outputs']['B']['done'] = 'yes'
        self.assertTrue(category_prevalence(first, second)['done']['over_ninety_percent'])

    def test_invalid_or_mismatched_passes_refused(self):
        row = record(1)
        for first, second in (([], []), ([row, row], [row]), ([row], [record(2)]),
                              ([row], [record(1, True)]), ([row], [None])):
            with self.subTest(first=first, second=second):
                with self.assertRaises(ValueError):
                    category_prevalence(first, second)

    def test_no_responsibility_does_not_claim_prevalence(self):
        item = category_prevalence([record(1)], [record(1)])['follow_through']
        self.assertEqual(item['pooled_total'], 0)
        self.assertFalse(item['over_ninety_percent'])

    def test_usable_subset_and_fixed_planned_n(self):
        plan = {str(i): i == 0 for i in range(20)}
        first = [record(i, i == 0) for i in range(20)]
        second = copy.deepcopy(first[1:])
        result = synthetic_usable_prevalence(first, second, plan)
        self.assertEqual((result['planned_cases'], result['usable_cases']), (20, 19))
        self.assertEqual(result['affected_case_ids'], ['0'])
        self.assertEqual(result['prevalence']['done']['pooled_total'], 76)
        self.assertEqual(result['prevalence']['follow_through']['pooled_total'], 0)

    def test_invalid_exercise_and_no_usable_cases_have_no_prevalence(self):
        plan = {str(i): False for i in range(20)}
        first = [record(i) for i in range(20)]
        result = synthetic_usable_prevalence(first, first[3:], plan)
        self.assertEqual(result['usable_cases'], 17)
        self.assertIsNone(result['prevalence'])
        result = synthetic_usable_prevalence(first, [], plan)
        self.assertEqual(result['usable_cases'], 0)
        self.assertIsNone(result['prevalence'])

    def test_duplicate_and_malformed_cases_are_excluded(self):
        plan = {str(i): False for i in range(20)}
        first = [record(i) for i in range(20)]
        second = copy.deepcopy(first)
        second.append(copy.deepcopy(second[0]))
        second[1]['outputs']['A']['trust'] = 'forged'
        result = synthetic_usable_prevalence(first, second, plan)
        self.assertEqual(result['affected_case_ids'], ['0', '1'])
        self.assertEqual(result['prevalence']['trust']['pooled_total'], 72)
