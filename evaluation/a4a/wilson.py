"""Descriptive Wilson 95% interval for synthetic A4a case agreement.

This helper does not establish mapping, blinding, independence, exercise
validity, or a benchmark result, and it does not apply a threshold or gate.
"""
from math import sqrt

# Two-sided 95% standard normal quantile.
_Z95 = 1.959963984540054


def wilson_95(agreements, planned_cases):
    """Return interval for count / fixed planned denominator, or None for N=0.

    Missing or unalignable cases must already count as disagreements. Callers
    must separately check exercise invalidity and the protocol's minimum N;
    this helper cannot do so.
    """
    if type(agreements) is not int or type(planned_cases) is not int:
        raise ValueError('counts must be integers')
    if planned_cases < 0 or agreements < 0 or agreements > planned_cases:
        raise ValueError('invalid agreement count or denominator')
    if planned_cases == 0:
        return None
    n = planned_cases
    p = agreements / n
    z2 = _Z95 * _Z95
    denominator = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denominator
    margin = _Z95 * sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denominator
    lower, upper = max(0.0, center - margin), min(1.0, center + margin)
    # Preserve exact binomial endpoints for reports and machine consumers.
    return (0.0 if agreements == 0 else lower, 1.0 if agreements == n else upper)
