import unittest
from wilson_interval import wilson_95


class WilsonIntervalTests(unittest.TestCase):
    def test_known_95_percent_intervals(self):
        for successes, total, expected in (
            (0, 10, (0.0, 0.2775328)),
            (5, 10, (0.2365931, 0.7634069)),
            (10, 10, (0.7224672, 1.0)),
        ):
            with self.subTest(successes=successes):
                low, high = wilson_95(successes, total)
                self.assertAlmostEqual(low, expected[0], places=6)
                self.assertAlmostEqual(high, expected[1], places=6)

    def test_invalid_empty_and_boolean_counts_rejected(self):
        for counts in ((0, 0), (-1, 3), (4, 3), (True, 3), (1, True), (1.0, 3)):
            with self.subTest(counts=counts):
                with self.assertRaises(ValueError):
                    wilson_95(*counts)
