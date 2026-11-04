import copy
import unittest
from prevalence import category_prevalence


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
