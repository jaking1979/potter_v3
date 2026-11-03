import copy
import unittest
from case_agreement import count_case_agreement, affected_case_summary


def record(case, responsibility=False):
    answers = {'done': 'yes', 'reexplaining': 'none', 'speed': 'yes', 'trust': 'yes'}
    if responsibility:
        answers['follow_through'] = 'yes'
    return {'case_id': case, 'responsibility': responsibility,
            'outputs': {'A': answers.copy(), 'B': answers.copy()}}


class CaseAgreementTest(unittest.TestCase):
    def test_invalidity_counts_distinct_cases_and_strict_boundary(self):
        plan = {'case-' + str(i): i == 0 for i in range(20)}
        first = [record(case, responsibility) for case, responsibility in plan.items()]
        for missing, invalid in ((2, False), (3, True)):
            with self.subTest(missing=missing):
                counts = count_case_agreement(first, first[missing:], plan)
                summary = affected_case_summary(counts, plan)
                self.assertEqual(summary['affected_count'], missing)
                self.assertEqual(summary['planned_cases'], 20)
                self.assertEqual(summary['invalid_over_ten_percent'], invalid)

    def test_both_outputs_must_agree_and_responsibility_denominator(self):
        first = [record('one'), record('two', True)]
        second = copy.deepcopy(first)
        second[0]['outputs']['B']['done'] = 'no'
        result = count_case_agreement(first, second, {'one': False, 'two': True})
        self.assertEqual((result['done']['agreements'], result['done']['denominator']), (1, 2))
        self.assertEqual((result['trust']['agreements'], result['trust']['denominator']), (2, 2))
        self.assertEqual((result['follow_through']['agreements'], result['follow_through']['denominator']), (1, 1))

    def test_missing_duplicate_and_invalid_are_counted(self):
        a = [record('missing'), record('duplicate'), record('invalid')]
        b = [record('duplicate'), record('duplicate'), record('invalid')]
        b[-1]['outputs']['B']['trust'] = 'forged'
        result = count_case_agreement(a, b, dict.fromkeys(('missing', 'duplicate', 'invalid'), False))
        self.assertEqual(result['trust']['denominator'], 3)
        self.assertEqual(result['trust']['agreements'], 0)
        self.assertEqual(len(result['trust']['affected']), 3)

    def test_plan_responsibility_mismatch_is_affected_in_both_directions(self):
        for planned, recorded in ((True, False), (False, True)):
            with self.subTest(planned=planned, recorded=recorded):
                rows = [record('case', recorded)]
                result = count_case_agreement(rows, copy.deepcopy(rows), {'case': planned})
                self.assertEqual(result['done']['affected'], [
                    {'case_id': 'case', 'reason': 'invalid record or responsibility mismatch'},
                ])

    def test_unexpected_cases_and_bad_plan_fail_closed(self):
        with self.assertRaises(ValueError):
            count_case_agreement([record('other')], [], {'one': False})
        with self.assertRaises(ValueError):
            count_case_agreement([], [], {'one': 1})
        with self.assertRaises(ValueError):
            count_case_agreement([{'case_id': []}], [], {'one': False})
