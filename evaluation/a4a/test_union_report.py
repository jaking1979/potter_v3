import unittest
from evaluation.a4a.union_report import summarize_dimensions

class UnionTests(unittest.TestCase):
    def test_union_invalidates_all(self):
        ids = [str(x) for x in range(20)]
        a = dict.fromkeys(ids, 'agree'); b = dict(a)
        a['0'] = a['1'] = 'missing'; b['2'] = 'unalignable'
        result = summarize_dimensions(ids, {'a': a, 'b': b})
        self.assertEqual(result['status'], 'invalid_rerun')
        self.assertNotIn('dimensions', result)
    def test_union_deduplicates_and_boundary(self):
        ids = [str(x) for x in range(20)]
        a = dict.fromkeys(ids, 'agree'); a['0'] = a['1'] = 'missing'
        r = summarize_dimensions(ids, {'a': a, 'b': dict(a)})
        self.assertEqual(r['status'], 'descriptive_only')
        self.assertEqual(r['affected_ids'], ['0', '1'])
    def test_reject_incomplete_dimension(self):
        ids = [str(x) for x in range(20)]
        with self.assertRaises(ValueError): summarize_dimensions(ids, {'a': {}})

    def test_affected_case_cannot_agree_in_other_dimension(self):
        ids = [str(x) for x in range(20)]
        a = dict.fromkeys(ids, 'agree'); b = dict(a)
        a['0'] = 'unalignable'
        result = summarize_dimensions(ids, {'done': a, 'trust': b})
        self.assertEqual(result['dimensions']['done']['agreements'], 19)
        self.assertEqual(result['dimensions']['trust']['agreements'], 19)
        self.assertEqual(result['dimensions']['trust']['forced_disagree'], ['0'])
        self.assertNotIn('affected', result['dimensions']['trust'])
        self.assertEqual(result['affected_reasons'], {'0': {'done': 'unalignable'}})

    def test_invalid_retains_reasons_by_dimension(self):
        ids = [str(x) for x in range(20)]
        a = dict.fromkeys(ids, 'agree'); b = dict(a)
        for k in ids[:3]: a[k] = 'missing'
        b['0'] = 'duplicate'
        result = summarize_dimensions(ids, {'a': a, 'b': b})
        self.assertEqual(result['status'], 'invalid_rerun')
        self.assertEqual(result['affected_reasons']['0'], {'a': 'missing', 'b': 'duplicate'})



    def test_dimension_label_validation(self):
        ids = [str(x) for x in range(20)]
        cases = dict.fromkeys(ids, 'agree')
        for dims in ({}, {'': cases}, {1: cases}, {'ok': {**cases, 'extra': 'agree'}}):
            with self.subTest(dims=dims), self.assertRaises(ValueError):
                summarize_dimensions(ids, dims)

if __name__ == '__main__': unittest.main()
