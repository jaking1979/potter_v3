import unittest
from evaluation.a4a.applicability_report import summarize_applicability

class ApplicabilityTests(unittest.TestCase):
    def setUp(self):
        self.ids = [f'case-{i}' for i in range(27)]
        self.plan = {'done': self.ids[:], 'follow': self.ids[20:]}
        self.scores = {k: dict.fromkeys(v, 'agree') for k, v in self.plan.items()}

    def test_one_shot_mismatch_excluded_from_follow_denominator(self):
        self.scores['done'][self.ids[0]] = 'unalignable'
        r = summarize_applicability(self.ids, self.plan, self.scores)
        self.assertEqual(r['dimensions']['done']['agreements'], 26)
        self.assertEqual((r['dimensions']['follow']['planned_cases'],
                          r['dimensions']['follow']['agreements']), (7, 7))
        self.assertEqual(r['dimensions']['follow']['forced_disagree'], [])
        self.assertEqual(r['affected_reasons'][self.ids[0]], {'done':'unalignable'})
        self.assertEqual(r['dimensions']['follow']['status'], 'descriptive_only')

    def test_responsibility_mismatch_forces_all_applicable_dimensions(self):
        case = self.ids[20]
        self.scores['follow'][case] = 'duplicate'
        r = summarize_applicability(self.ids, self.plan, self.scores)
        self.assertEqual(r['dimensions']['done']['agreements'], 26)
        self.assertEqual(r['dimensions']['follow']['agreements'], 6)
        self.assertEqual(r['dimensions']['done']['forced_disagree'], [case])

    def test_union_threshold_and_reason_preservation(self):
        a, b, c = self.ids[0], self.ids[20], self.ids[21]
        self.scores['done'][a] = 'missing'
        self.scores['follow'][b] = 'unalignable'
        r = summarize_applicability(self.ids, self.plan, self.scores)
        self.assertEqual(r['status'], 'descriptive_only')
        self.assertEqual(len(r['affected_ids']), 2)
        self.scores['follow'][c] = 'missing'
        self.scores['done'][c] = 'duplicate'
        r = summarize_applicability(self.ids, self.plan, self.scores)
        self.assertEqual(r['status'], 'invalid_rerun')
        self.assertNotIn('dimensions', r)
        self.assertEqual(r['affected_reasons'][c], {'done':'duplicate', 'follow':'missing'})

    def test_classification_plan_mismatch_both_directions_and_invalid_plan(self):
        for plan, scores, ids in (
            (self.plan, {'done': self.scores['done'], 'follow': {}}, self.ids),
            (self.plan, {'done': self.scores['done'], 'follow': {**self.scores['follow'], self.ids[0]:'agree'}}, self.ids),
            ({**self.plan, 'follow': self.plan['follow'] + [self.ids[20]]}, self.scores, self.ids),
            ({**self.plan, 'follow': ['outsider']}, self.scores, self.ids),
            (self.plan, self.scores, self.ids[:-1] + [self.ids[0]]),
            (self.plan, {'done': self.scores['done']}, self.ids)):
            with self.subTest(plan=plan, scores=scores), self.assertRaises(ValueError):
                summarize_applicability(ids, plan, scores)

if __name__ == '__main__':
    unittest.main()
