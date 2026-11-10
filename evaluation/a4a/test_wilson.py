import unittest
from wilson import wilson_95


class WilsonTest(unittest.TestCase):
    def test_known_reference_interval(self):
        lower, upper = wilson_95(8, 10)
        self.assertAlmostEqual(lower, 0.4901624715366418)
        self.assertAlmostEqual(upper, 0.9433178485456247)

    def test_zero_and_all_success_boundary(self):
        low, high = wilson_95(0, 20)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.16112515805281938)
        low2, high2 = wilson_95(20, 20)
        self.assertAlmostEqual(low2, 1 - high)
        self.assertEqual(high2, 1.0)
        self.assertIsNone(wilson_95(0, 0))

    def test_bad_counts_rejected(self):
        for a, n in [(True, 20), (1, True), (-1, 20), (21, 20), (0, -1), (0.0, 20), (1, 0)]:
            with self.subTest(a=a, n=n), self.assertRaises(ValueError):
                wilson_95(a, n)


if __name__ == '__main__':
    unittest.main()
