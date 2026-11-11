"""Synthetic multi-dimension summary; caller classifications are unverified."""
from .synthetic_validity import summarize

def summarize_dimensions(planned_ids, dimensions):
    if type(dimensions) is not dict or not dimensions or len(dimensions) > 100:
        raise ValueError('expected bounded nonempty dimensions')
    if any(type(k) is not str or not k or len(k) > 200 for k in dimensions):
        raise ValueError('invalid dimension')
    individual = {k: summarize(planned_ids, v) for k, v in dimensions.items()}
    affected = set().union(*(set(v['affected']) for v in individual.values()))
    if len(affected) * 10 > len(planned_ids):
        return {'status': 'invalid_rerun', 'affected_ids': sorted(affected)}
    return {'status': 'descriptive_only', 'affected_ids': sorted(affected),
            'dimensions': individual}
