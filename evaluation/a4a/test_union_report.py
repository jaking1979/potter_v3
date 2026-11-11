import unittest
from .union_report import summarize_dimensions

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

if __name__ == '__main__': unittest.main()
