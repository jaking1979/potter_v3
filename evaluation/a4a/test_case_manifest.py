import unittest
from case_manifest import validate_synthetic_plan


class SyntheticPlanTests(unittest.TestCase):
    def test_fixed_synthetic_counts(self):
        rows = [{'case_id': f'case-{i}', 'version': 'v1',
                 'kind': 'one-shot' if i < 20 else 'responsibility'}
                for i in range(27)]
        result = validate_synthetic_plan(rows)
        self.assertEqual((result['cases'], result['one_shot'], result['responsibility']), (27, 20, 7))
        self.assertNotIn('case-0', repr(result))

    def test_duplicate_id_even_with_changed_version_refused(self):
        row = {'case_id': 'case-1', 'version': 'v1', 'kind': 'one-shot'}
        with self.assertRaises(ValueError):
            validate_synthetic_plan([row, dict(row, version='v2')])

    def test_malformed_records_refused(self):
        row = {'case_id': 'case-1', 'version': 'v1', 'kind': 'one-shot'}
        for bad in ([], [dict(row, case_id='../a')], [dict(row, version='')],
                    [dict(row, kind='other')], [dict(row, extra='sensitive')],
                    [dict(row, case_id=True)], [None]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_synthetic_plan(bad)


if __name__ == '__main__':
    unittest.main()
