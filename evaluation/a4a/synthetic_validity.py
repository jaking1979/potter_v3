"""Offline synthetic L17 validity summary; not an acceptance gate.

Inputs are preclassified by a caller. This does not prove prespecification,
realignment, independent grading, blinding, provenance, or correct labels.
The agreed preparation protocol is not a ratified acceptance rule.
"""
from .wilson import wilson_95


def summarize(planned_ids, classifications):
    """Use every planned case once; affected means missing/duplicate/unalignable.

    More than 10% affected cases prevents calculation of a score. The caller
    must accurately determine case status after protected realignment. Cases
    not classified are rejected, not silently excluded from the denominator.
    """
    if type(planned_ids) is not list or not 20 <= len(planned_ids) <= 10000:
        raise ValueError('expected 20 to 10000 planned case IDs')
    if any(type(x) is not str or not x or len(x) > 200 for x in planned_ids):
        raise ValueError('invalid case ID')
    if len(set(planned_ids)) != len(planned_ids):
        raise ValueError('duplicate planned case ID')
    if type(classifications) is not dict or set(classifications) != set(planned_ids):
        raise ValueError('classifications must cover exactly the planned IDs')
    allowed = {'agree', 'disagree', 'missing', 'duplicate', 'unalignable'}
    if any(type(x) is not str or x not in allowed for x in classifications.values()):
        raise ValueError('unknown classification')
    affected = {k: classifications[k] for k in planned_ids
                if classifications[k] in {'missing', 'duplicate', 'unalignable'}}
    n = len(planned_ids)
    result = {'planned_cases': n, 'affected': affected,
              'independence_assumption': 'distinct cases are assumed independent'}
    # Integer arithmetic preserves the strict >10% boundary.
    if len(affected) * 10 > n:
        result['status'] = 'invalid_rerun'
        return result
    agreements = sum(classifications[k] == 'agree' for k in planned_ids)
    result.update(status='descriptive_only', agreements=agreements,
                  interval_95=wilson_95(agreements, n))
    return result
