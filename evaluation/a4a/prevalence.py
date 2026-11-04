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


def synthetic_usable_prevalence(first, second, expected_cases):
    """Experimental usable-case counts; not an exercise or pass verdict.

    Matched, valid cases are counted; affected cases are displayed alongside the
    planned N. When affected cases exceed 10% or none remain, prevalence is
    unavailable. Caller must independently verify mapping and exercise validity.
    """
    from case_agreement import count_case_agreement, affected_case_summary

    counts = count_case_agreement(first, second, expected_cases)
    affected = affected_case_summary(counts, expected_cases)
    excluded = set(affected['affected_case_ids'])
    usable = set(expected_cases) - excluded
    values = None
    if usable and not affected['invalid_over_ten_percent']:
        filtered = [[row for row in records if row['case_id'] in usable]
                    for records in (first, second)]
        values = category_prevalence(*filtered)
    return {'planned_cases': len(expected_cases), 'usable_cases': len(usable),
            'affected_case_ids': affected['affected_case_ids'], 'prevalence': values}
