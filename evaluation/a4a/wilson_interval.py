"""Compatibility wrapper preserving rejection of empty agreement counts."""
from evaluation.a4a.wilson import wilson_95 as _canonical


def wilson_95(agreements, total):
    result = _canonical(agreements, total)
    if result is None:
        raise ValueError('total must be positive')
    return result
