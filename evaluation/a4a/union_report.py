"""Synthetic multi-dimension summary; caller classifications are unverified.

Applicability (which planned cases a dimension applies to) must come from the
independently locked plan, never from grader output. Exercise invalidation is
the union of affected cases over ALL planned cases. Each dimension is scored
over its own applicable planned cases; a dimension below the minimum N is
reported descriptively and never as a pass. This is offline preparation, not a
ratified grading rule.
"""
from evaluation.a4a.synthetic_validity import _summarize_dimension
from evaluation.a4a.wilson import wilson_95

MIN_N = 20
_AFFECTED = {'missing', 'duplicate', 'unalignable'}
_ALLOWED = {'agree', 'disagree'} | _AFFECTED
MISMATCH = 'applicability_mismatch'


def _validate_planned(planned_ids):
    if type(planned_ids) is not list or not MIN_N <= len(planned_ids) <= 10000:
        raise ValueError('expected 20 to 10000 planned case IDs')
    if any(type(x) is not str or not x or len(x) > 200 for x in planned_ids):
        raise ValueError('invalid case ID')
    if len(set(planned_ids)) != len(planned_ids):
        raise ValueError('duplicate planned case ID')


def _validate_applicability(planned_ids, dimensions, applicability):
    if applicability is None:
        return {name: list(planned_ids) for name in dimensions}
    if type(applicability) is not dict or set(applicability) != set(dimensions):
        raise ValueError('applicability must name exactly the dimensions')
    planned = set(planned_ids)
    out = {}
    for name, ids in applicability.items():
        if type(ids) is not list or not ids:
            raise ValueError('applicability must be a nonempty list per dimension')
        if any(type(x) is not str for x in ids) or len(set(ids)) != len(ids):
            raise ValueError('duplicate or invalid applicable case ID')
        if not set(ids) <= planned:
            raise ValueError('applicable case ID is not planned')
        out[name] = [x for x in planned_ids if x in set(ids)]   # plan order
    return out


def summarize_dimensions(planned_ids, dimensions, applicability=None):
    """Summarize several dimensions over one locked plan.

    ``applicability`` maps each dimension to the planned IDs it applies to
    (default: all planned IDs). Classifying an inapplicable planned case, or
    omitting an applicable one, makes that case affected (``MISMATCH``). IDs
    that are not planned at all are rejected.
    """
    if type(dimensions) is not dict or not dimensions or len(dimensions) > 100:
        raise ValueError('expected bounded nonempty dimensions')
    if any(type(k) is not str or not k or len(k) > 200 for k in dimensions):
        raise ValueError('invalid dimension')
    _validate_planned(planned_ids)
    applies = _validate_applicability(planned_ids, dimensions, applicability)
    if applicability is None:
        # Unchanged legacy contract: every dimension classifies every planned ID.
        for ratings in dimensions.values():
            _summarize_dimension(planned_ids, ratings)
    planned = set(planned_ids)
    reasons_by_dim = {}
    for name, ratings in dimensions.items():
        if type(ratings) is not dict:
            raise ValueError('classifications must be a dict')
        if not set(ratings) <= planned:
            raise ValueError('classification for an unplanned case ID')
        if any(type(v) is not str or v not in _ALLOWED for v in ratings.values()):
            raise ValueError('unknown classification')
        applicable = set(applies[name])
        reasons = {}
        for case in planned_ids:
            if case in applicable and case not in ratings:
                reasons[case] = MISMATCH
            elif case not in applicable and case in ratings:
                reasons[case] = MISMATCH
            elif case in applicable and ratings[case] in _AFFECTED:
                reasons[case] = ratings[case]
        reasons_by_dim[name] = reasons
    affected = set().union(*(set(r) for r in reasons_by_dim.values()))
    reasons = {case: {name: r[case] for name, r in reasons_by_dim.items() if case in r}
               for case in sorted(affected)}
    if len(affected) * 10 > len(planned_ids):
        return {'status': 'invalid_rerun', 'affected_ids': sorted(affected),
                'affected_reasons': reasons, 'planned_cases': len(planned_ids)}
    individual = {}
    for name, ratings in dimensions.items():
        ids = applies[name]
        forced = sorted(set(ids) & affected)
        agreements = sum(1 for case in ids
                         if case not in affected and ratings.get(case) == 'agree')
        n = len(ids)
        individual[name] = {
            'status': 'descriptive_only', 'planned_cases': n,
            'not_applicable': len(planned_ids) - n,
            'below_min_n': n < MIN_N, 'agreements': agreements,
            'interval_95': wilson_95(agreements, n), 'forced_disagree': forced,
            'independence_assumption': 'distinct cases are assumed independent'}
    return {'status': 'descriptive_only', 'affected_ids': sorted(affected),
            'affected_reasons': reasons, 'planned_cases': len(planned_ids),
            'dimensions': individual}
