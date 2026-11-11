import unittest
from evaluation.a4a.synthetic_validity import _summarize_dimension


class SyntheticValidityTests(unittest.TestCase):
    def setUp(self):
        self.ids = [f'case-{n}' for n in range(20)]
        self.cases = {x: 'agree' for x in self.ids}

    def test_affected_are_unique_disagreements_in_fixed_denominator(self):
        self.cases[self.ids[0]] = 'missing'
        self.cases[self.ids[1]] = 'unalignable'
        r = _summarize_dimension(self.ids, self.cases)
        self.assertEqual((r['status'], r['agreements'], r['planned_cases']),
                         ('descriptive_only', 18, 20))
        self.assertEqual(len(r['affected']), 2)
        self.assertIsNotNone(r['interval_95'])

    def test_over_ten_percent_is_not_scored(self):
        for case in self.ids[:3]:
            self.cases[case] = 'duplicate'
        r = _summarize_dimension(self.ids, self.cases)
        self.assertEqual(r['status'], 'invalid_rerun')
        self.assertNotIn('agreements', r)
        self.assertNotIn('interval_95', r)

    def test_missing_extra_duplicate_or_bad_input_fails_closed(self):
        for ids, cases in ((self.ids[:19], self.cases),
                           (self.ids[:-1] + [self.ids[0]], self.cases),
                           (self.ids, {k: v for k, v in self.cases.items() if k != self.ids[0]}),
                           (self.ids, {**self.cases, 'extra': 'agree'}),
                           (self.ids, {**self.cases, self.ids[0]: 'unknown'})):
            with self.assertRaises(ValueError):
                _summarize_dimension(ids, cases)

    def test_exact_ten_percent_for_odd_planned_count(self):
        ids = self.ids + ['case-20']
        cases = {x: 'agree' for x in ids}
        cases[ids[0]] = cases[ids[1]] = 'missing'
        self.assertEqual(_summarize_dimension(ids, cases)['status'], 'descriptive_only')
        cases[ids[2]] = 'missing'
        self.assertEqual(_summarize_dimension(ids, cases)['status'], 'invalid_rerun')


if __name__ == '__main__':
    unittest.main()
