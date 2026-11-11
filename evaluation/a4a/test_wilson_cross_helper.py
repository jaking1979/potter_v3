"""Synthetic cross-helper comparison, not a grading or acceptance gate."""
import unittest
from evaluation.a4a.wilson import wilson_95 as fixed
from evaluation.a4a.wilson_interval import wilson_95 as legacy

class CrossHelperAgreement(unittest.TestCase):
    def test_positive_counts_agree_to_reporting_precision(self):
        for n in (1, 7, 20, 27, 100):
            for k in range(n + 1):
                with self.subTest(k=k, n=n):
                    for a, b in zip(fixed(k, n), legacy(k, n)):
                        self.assertAlmostEqual(a, b, places=14)
    def test_empty_denominator_has_incompatible_contracts(self):
        self.assertIsNone(fixed(0, 0))
        with self.assertRaises(ValueError):
            legacy(0, 0)

    def test_exact_endpoints(self):
        for n in (1, 7, 20, 27, 100, 1000):
            self.assertEqual(legacy(0, n)[0], 0.0)
            self.assertEqual(legacy(n, n)[1], 1.0)

if __name__ == '__main__': unittest.main()
