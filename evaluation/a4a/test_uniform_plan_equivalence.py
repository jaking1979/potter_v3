"""Cross-check synthetic uniform-plan reports; no real grading or acceptance."""
import unittest

from evaluation.a4a.applicability_report import summarize_applicability
from evaluation.a4a.union_report import summarize_dimensions


class UniformPlanEquivalence(unittest.TestCase):
    def test_uniform_plan_matches_union_across_affected_boundaries(self):
        for n in (20, 27):
            ids = [f'case-{i}' for i in range(n)]
            for affected_count in (0, 1, 2, 3):
                scores = {d: dict.fromkeys(ids, 'agree') for d in ('done', 'trust')}
                scores['done'][ids[-1]] = 'disagree'
                labels = ('missing', 'duplicate', 'unalignable')
                for i in range(affected_count):
                    scores[('done', 'trust')[i % 2]][ids[i]] = labels[i % len(labels)]
                if affected_count:
                    scores['trust'][ids[0]] = 'unalignable'
                with self.subTest(n=n, affected=affected_count):
                    old = summarize_dimensions(ids, scores)
                    new = summarize_applicability(ids, {d: ids[:] for d in scores}, scores)
                    for field in ('status', 'affected_ids', 'affected_reasons'):
                        self.assertEqual(old[field], new[field])
                    if old['status'] == 'invalid_rerun':
                        self.assertNotIn('dimensions', new)
                        continue
                    for d in scores:
                        self.assertEqual(old['dimensions'][d], new['dimensions'][d])
                        self.assertEqual(new['dimensions'][d]['status'], 'descriptive_only')

    def test_both_reject_nonidentical_or_unplanned_classifications(self):
        ids = [f'case-{i}' for i in range(20)]
        for broken in ({'done': {}}, {'done': {**dict.fromkeys(ids, 'agree'), 'extra': 'agree'}}):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    summarize_dimensions(ids, broken)
                with self.assertRaises(ValueError):
                    summarize_applicability(ids, {'done': ids}, broken)


if __name__ == '__main__':
    unittest.main()
