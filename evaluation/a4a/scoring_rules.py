"""Shared constants for the experimental, descriptive-only A4a scorer.

These do not attest to source integrity or resolve the proposed L17 rule.
"""
DIMENSIONS = ('done', 'reexplaining', 'speed', 'trust', 'follow_through')
FOLLOW_THROUGH = 'follow_through'
FOLLOW_THROUGH_RULE = 'responsibility-only'
INVALID_MULTIPLIER = 10
CLASSIFICATIONS = ('agree', 'disagree', 'missing', 'duplicate', 'unalignable')
AFFECTED = frozenset(('missing', 'duplicate', 'unalignable'))


def is_applicable(dimension, responsibility):
    return dimension != FOLLOW_THROUGH or responsibility
