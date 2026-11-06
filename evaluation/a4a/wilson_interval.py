"""Offline Wilson 95% interval helper for synthetic case-agreement counts.

The interval assumes independent case outcomes. It is descriptive only and
cannot establish case independence, the validity of an exercise or a pass.
"""
from math import sqrt

_Z_95 = 1.959963984540054


def wilson_95(agreements, total):
    """Return Wilson score interval for valid case counts; reject empty data."""
    if (type(agreements) is not int or type(total) is not int or total <= 0
            or agreements < 0 or agreements > total):
        raise ValueError('require integer counts with 0 <= agreements <= total and total > 0')
    z2 = _Z_95 ** 2
    proportion = agreements / total
    denominator = 1 + z2 / total
    center = (proportion + z2 / (2 * total)) / denominator
    margin = _Z_95 * sqrt(proportion * (1 - proportion) / total + z2 / (4 * total**2)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)
