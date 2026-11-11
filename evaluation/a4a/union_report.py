"""Synthetic multi-dimension summary; caller classifications are unverified."""
from evaluation.a4a.synthetic_validity import _summarize_dimension

def summarize_dimensions(planned_ids, dimensions):
    if type(dimensions) is not dict or not dimensions or len(dimensions) > 100:
        raise ValueError('expected bounded nonempty dimensions')
    if any(type(k) is not str or not k or len(k) > 200 for k in dimensions):
        raise ValueError('invalid dimension')
    individual = {k: _summarize_dimension(planned_ids, v) for k, v in dimensions.items()}
    affected = set().union(*(set(v['affected']) for v in individual.values()))
    reasons = {case: {dimension: result['affected'][case]
                      for dimension, result in individual.items()
                      if case in result['affected']}
               for case in sorted(affected)}
    if len(affected) * 10 > len(planned_ids):
        return {'status': 'invalid_rerun', 'affected_ids': sorted(affected),
                'affected_reasons': reasons}
    # A case-level alignment failure invalidates all its dimension ratings.
    adjusted = {name: {case: ('disagree' if case in affected else value)
                       for case, value in ratings.items()}
                for name, ratings in dimensions.items()}
    individual = {name: _summarize_dimension(planned_ids, ratings)
                  for name, ratings in adjusted.items()}
    for result in individual.values():
        result.pop('affected', None)
        result['forced_disagree'] = sorted(affected)
    return {'status': 'descriptive_only', 'affected_ids': sorted(affected),
            'affected_reasons': reasons, 'dimensions': individual}


