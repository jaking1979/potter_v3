"""Synthetic applicability-aware summary. Not a grading gate or proof of a locked plan."""
from evaluation.a4a.wilson import wilson_95

_AFFECTED = frozenset(('missing', 'duplicate', 'unalignable'))
_ALLOWED = _AFFECTED | frozenset(('agree', 'disagree'))

def summarize_applicability(planned_ids, applicability, classifications):
    """Plan is supplied independently of classifications; no inferred applicability.

    Invalid classifications are rejected. This helper does not verify locking,
    blinding, alignment, provenance, grader independence, or an acceptance gate.
    """
    def ids(value, *, minimum):
        if type(value) is not list or not minimum <= len(value) <= 10000:
            raise ValueError('invalid planned IDs')
        if any(type(x) is not str or not x or len(x) > 200 for x in value):
            raise ValueError('invalid case ID')
        if len(set(value)) != len(value):
            raise ValueError('duplicate planned case ID')
        return set(value)

    planned = ids(planned_ids, minimum=20)
    if type(applicability) is not dict or not applicability or len(applicability) > 100:
        raise ValueError('invalid applicability plan')
    if any(type(k) is not str or not k or len(k) > 200 for k in applicability):
        raise ValueError('invalid dimension')
    if type(classifications) is not dict or set(classifications) != set(applicability):
        raise ValueError('classifications must cover planned dimensions')
    affected_reasons = {}
    for dimension, selected in applicability.items():
        applicable = ids(selected, minimum=1)
        if not applicable <= planned:
            raise ValueError('unplanned applicable case')
        ratings = classifications[dimension]
        if type(ratings) is not dict or set(ratings) != applicable:
            raise ValueError('classifications must cover exactly applicable IDs')
        for case, label in ratings.items():
            if type(label) is not str or label not in _ALLOWED:
                raise ValueError('invalid classification')
            if label in _AFFECTED:
                affected_reasons.setdefault(case, {})[dimension] = label
    affected = set(affected_reasons)
    report = {'affected_ids': sorted(affected), 'affected_reasons': affected_reasons,
              'planned_cases': len(planned_ids)}
    if len(affected) * 10 > len(planned_ids):
        report['status'] = 'invalid_rerun'
        return report
    results = {}
    for dimension, selected in applicability.items():
        forced = sorted(affected.intersection(selected))
        agreements = sum(classifications[dimension][case] == 'agree' and case not in affected
                         for case in selected)
        n = len(selected)
        results[dimension] = {'status': 'descriptive_only', 'planned_cases': n,
                              'agreements': agreements, 'forced_disagree': forced,
                              'interval_95': wilson_95(agreements, n),
                              'independence_assumption': 'distinct cases are assumed independent'}
    report.update(status='descriptive_only', dimensions=results)
    return report
