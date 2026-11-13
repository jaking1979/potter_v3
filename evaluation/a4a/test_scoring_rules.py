"""The offline commitment and scorer must share their declared rule data."""
import unittest

from evaluation.a4a.applicability_report import _ALLOWED
from evaluation.a4a.exercise_commitment import _RULES
from evaluation.a4a.scoring_rules import (
    CLASSIFICATIONS, DIMENSIONS, FOLLOW_THROUGH, FOLLOW_THROUGH_RULE,
    INVALID_MULTIPLIER, is_applicable,
)


class ScoringRulesTest(unittest.TestCase):
    def test_descriptor_matches_shared_data(self):
        self.assertEqual(_RULES['dimensions'], list(DIMENSIONS))
        self.assertEqual(_RULES['classifications'], list(CLASSIFICATIONS))
        self.assertEqual(_ALLOWED, frozenset(CLASSIFICATIONS))
        self.assertEqual(_RULES['follow_through'], FOLLOW_THROUGH_RULE)
        self.assertEqual(_RULES['invalid_when'],
                         f'affected-times-{INVALID_MULTIPLIER}-greater-than-planned')

    def test_follow_through_only_for_responsibility(self):
        for dim in DIMENSIONS:
            self.assertTrue(is_applicable(dim, True))
            self.assertEqual(is_applicable(dim, False), dim != FOLLOW_THROUGH)
