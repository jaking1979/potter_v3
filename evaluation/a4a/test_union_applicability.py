"""Synthetic 27-case plan (20 one-shot + 7 responsibility); not a grading gate."""
import unittest
from evaluation.a4a.union_report import MISMATCH, summarize_dimensions

ONE_SHOT = ['o%02d' % i for i in range(20)]
RESP = ['r%d' % i for i in range(7)]
PLANNED = ONE_SHOT + RESP
DIMS = ('done', 'reexplaining', 'speed', 'trust')


def plan(**overrides):
    dims = {d: dict.fromkeys(PLANNED, 'agree') for d in DIMS}
    dims['follow_through'] = dict.fromkeys(RESP, 'agree')
    dims.update(overrides)
    applies = {d: list(PLANNED) for d in DIMS}
    applies['follow_through'] = list(RESP)
    return dims, applies


class ApplicabilityTests(unittest.TestCase):
    def test_denominators_27_and_7_no_pass_claim(self):
        dims, applies = plan()
        r = summarize_dimensions(PLANNED, dims, applies)
        self.assertEqual(r['status'], 'descriptive_only')
        ft = r['dimensions']['follow_through']
        self.assertEqual((ft['planned_cases'], ft['not_applicable'], ft['agreements']), (7, 20, 7))
        self.assertTrue(ft['below_min_n'])
        self.assertEqual(ft['status'], 'descriptive_only')
        self.assertEqual(ft['interval_95'][1], 1.0)
        for d in DIMS:
            self.assertEqual((r['dimensions'][d]['planned_cases'],
                              r['dimensions'][d]['not_applicable']), (27, 0))
            self.assertFalse(r['dimensions'][d]['below_min_n'])
        self.assertNotIn('pass', repr(r).lower())

    def test_one_shot_classified_for_follow_through_is_mismatch(self):
        dims, applies = plan()
        dims['follow_through'] = {**dims['follow_through'], 'o00': 'agree'}
        r = summarize_dimensions(PLANNED, dims, applies)
        self.assertEqual(r['affected_reasons'], {'o00': {'follow_through': MISMATCH}})
        self.assertEqual(r['dimensions']['done']['forced_disagree'], ['o00'])
        self.assertEqual(r['dimensions']['done']['agreements'], 26)
        ft = r['dimensions']['follow_through']
        self.assertEqual((ft['planned_cases'], ft['agreements'], ft['forced_disagree']), (7, 7, []))

    def test_responsibility_missing_from_follow_through_is_mismatch(self):
        dims, applies = plan()
        del dims['follow_through']['r3']
        r = summarize_dimensions(PLANNED, dims, applies)
        self.assertEqual(r['affected_reasons'], {'r3': {'follow_through': MISMATCH}})
        ft = r['dimensions']['follow_through']
        self.assertEqual((ft['planned_cases'], ft['agreements'], ft['forced_disagree']), (7, 6, ['r3']))
        self.assertEqual(r['dimensions']['trust']['agreements'], 26)

    def test_missing_classification_in_applicable_dimension(self):
        dims, applies = plan()
        del dims['speed']['o05']
        dims['trust']['r1'] = 'missing'
        r = summarize_dimensions(PLANNED, dims, applies)
        self.assertEqual(r['affected_reasons'],
                         {'o05': {'speed': MISMATCH}, 'r1': {'trust': 'missing'}})
        self.assertEqual(r['dimensions']['follow_through']['forced_disagree'], ['r1'])
        self.assertEqual(r['dimensions']['follow_through']['agreements'], 6)

    def test_union_threshold_over_27(self):
        dims, applies = plan()
        dims['done']['o01'] = 'unalignable'
        dims['speed']['o02'] = 'duplicate'          # 2 of 27 affected: allowed
        self.assertEqual(summarize_dimensions(PLANNED, dims, applies)['status'],
                         'descriptive_only')
        del dims['follow_through']['r6']            # 3 of 27 > 10%: invalid
        r = summarize_dimensions(PLANNED, dims, applies)
        self.assertEqual(r['status'], 'invalid_rerun')
        self.assertNotIn('dimensions', r)
        self.assertEqual(r['affected_ids'], ['o01', 'o02', 'r6'])

    def test_reject_unplanned_and_bad_applicability(self):
        dims, applies = plan()
        bad = {**dims, 'trust': {**dims['trust'], 'zz': 'agree'}}
        with self.assertRaises(ValueError):
            summarize_dimensions(PLANNED, bad, applies)
        for a in ({**applies, 'follow_through': RESP + ['zz']},
                  {**applies, 'follow_through': RESP + ['r0']},
                  {**applies, 'follow_through': []},
                  {d: v for d, v in applies.items() if d != 'trust'}):
            with self.subTest(a=a), self.assertRaises(ValueError):
                summarize_dimensions(PLANNED, dims, a)
        with self.assertRaises(ValueError):
            summarize_dimensions(PLANNED + ['o00'], dims, applies)
        with self.assertRaises(ValueError):
            summarize_dimensions(PLANNED, {**dims, 'done': {**dims['done'], 'o00': 'yes'}}, applies)

    def test_applicability_order_does_not_matter(self):
        dims, applies = plan()
        applies['follow_through'] = list(reversed(RESP))
        r = summarize_dimensions(PLANNED, dims, applies)
        self.assertEqual(r['dimensions']['follow_through']['planned_cases'], 7)


if __name__ == '__main__': unittest.main()
