import copy
import unittest
from report import synthetic_report


def record(i, responsibility=False):
    grade = {'done': 'yes', 'reexplaining': 'none', 'speed': 'yes', 'trust': 'yes'}
    if responsibility:
        grade['follow_through'] = 'yes'
    return {'case_id': str(i), 'responsibility': responsibility,
            'outputs': {'A': grade.copy(), 'B': grade.copy()}}


class SyntheticReportTests(unittest.TestCase):
    def test_fixed_denominator_exclusion_flips_and_interval(self):
        plan = {str(i): i == 0 for i in range(20)}
        first = [record(i, i == 0) for i in range(20)]
        second = copy.deepcopy(first[1:])
        second[0]['outputs']['A']['done'] = 'no'
        result = synthetic_report(first, second, plan)
        self.assertEqual(result['affected_case_ids'], ['0'])
        self.assertEqual(result['agreement']['done']['denominator'], 20)
        self.assertEqual(result['agreement']['done']['agreements'], 18)
        self.assertEqual(result['prevalence']['done']['pooled_total'], 76)
        self.assertEqual(result['agreement']['follow_through']['denominator'], 1)
        self.assertEqual(result['observed_flips'][0]['case_id'], '1')
        self.assertLess(result['agreement']['done']['wilson_95'][0], .9)

    def test_invalid_exercise_suppresses_results(self):
        plan = {str(i): False for i in range(20)}
        first = [record(i) for i in range(20)]
        result = synthetic_report(first, first[3:], plan)
        self.assertTrue(result['invalid_over_ten_percent'])
        for key in ('agreement', 'prevalence', 'observed_flips'):
            self.assertIsNone(result[key])

    def test_no_responsibilities_no_interval(self):
        plan = {'1': False}
        row = [record(1)]
        self.assertIsNone(synthetic_report(row, row, plan)['agreement']['follow_through']['wilson_95'])

    def test_unexpected_case_refused(self):
        with self.assertRaises(ValueError):
            synthetic_report([record(2)], [record(2)], {'1': False})


if __name__ == '__main__':
    unittest.main()
