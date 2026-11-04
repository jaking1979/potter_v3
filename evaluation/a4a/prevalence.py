"""Synthetic prevalence counts; descriptive only, never a grading gate.

Inputs are already aligned complete grade records. This cannot establish the
protected mapping, independence, blinding, or exercise validity.
"""
from blind_grading import DIMENSIONS, validate_grades


def category_prevalence(first, second):
    """Report per-pass and pooled counts, using both outputs of each case."""
    if not isinstance(first, list) or not isinstance(second, list) or not first:
        raise ValueError('nonempty passes required')
    indexed = []
    for records in (first, second):
        rows = {}
        for record in records:
            validate_grades(record)
            key = record['case_id']
            if key in rows:
                raise ValueError('duplicate case')
            rows[key] = record
        indexed.append(rows)
    if set(indexed[0]) != set(indexed[1]):
        raise ValueError('case sets differ')
    if any(indexed[0][key]['responsibility'] != indexed[1][key]['responsibility']
           for key in indexed[0]):
        raise ValueError('responsibility mismatch')
    categories = dict(DIMENSIONS)
    categories['follow_through'] = {'yes', 'partly', 'no'}
    result = {}
    for dimension, options in categories.items():
        passes = []
        for rows in indexed:
            counts = dict.fromkeys(sorted(options), 0)
            for record in rows.values():
                if dimension == 'follow_through' and not record['responsibility']:
                    continue
                for label in ('A', 'B'):
                    counts[record['outputs'][label][dimension]] += 1
            passes.append(counts)
        pooled = {choice: passes[0][choice] + passes[1][choice] for choice in sorted(options)}
        n = sum(pooled.values())
        result[dimension] = {
            'pass_1': passes[0], 'pass_2': passes[1], 'pooled': pooled,
            'pooled_total': n,
            'over_ninety_percent': n > 0 and any(count * 10 > n * 9 for count in pooled.values()),
        }
    return result
