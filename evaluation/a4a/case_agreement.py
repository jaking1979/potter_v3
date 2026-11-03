"""Synthetic offline L17 count helper; not a grading or acceptance gate.

Inputs must already be aligned by a separately protected, verified mapping. This
helper cannot establish blinding, mapping provenance, case independence or the
integrity of what a grader saw; it does not calculate pass/fail thresholds.
"""
from blind_grading import validate_grades


def count_case_agreement(first, second, expected_cases):
    """Count both-output agreement per dimension over prespecified case IDs.

    Missing, malformed, duplicate and mismatched records are explicitly marked
    affected and count as disagreement, rather than silently disappearing.
    Follow-through applies only to responsibility cases supplied in the plan.
    """
    if not isinstance(expected_cases, dict) or not expected_cases:
        raise ValueError('expected case plan required')
    if any(not isinstance(k, str) or not k.strip() or type(v) is not bool
           for k, v in expected_cases.items()):
        raise ValueError('invalid case plan')
    if not isinstance(first, list) or not isinstance(second, list):
        raise ValueError('passes must be lists')
    passes = []
    for records in (first, second):
        indexed = {}
        for row in records:
            if not isinstance(row, dict) or row.get('case_id') not in expected_cases:
                raise ValueError('unexpected or unidentifiable case')
            indexed.setdefault(row['case_id'], []).append(row)
        passes.append(indexed)
    result = {key: {'agreements': 0, 'denominator': 0, 'affected': []}
              for key in ('done', 'reexplaining', 'speed', 'trust', 'follow_through')}
    for case_id, responsibility in expected_cases.items():
        dims = ['done', 'reexplaining', 'speed', 'trust']
        if responsibility:
            dims.append('follow_through')
        entries = [index.get(case_id, []) for index in passes]
        issue = None
        if any(len(rows) != 1 for rows in entries):
            issue = 'missing or duplicate record'
        else:
            try:
                for rows in entries:
                    validate_grades(rows[0])
                    if rows[0]['responsibility'] != responsibility:
                        raise ValueError('responsibility mismatch')
            except (ValueError, TypeError, KeyError):
                issue = 'invalid record or responsibility mismatch'
        for dim in dims:
            item = result[dim]
            item['denominator'] += 1
            if issue:
                item['affected'].append({'case_id': case_id, 'reason': issue})
            elif all(entries[0][0]['outputs'][label][dim] == entries[1][0]['outputs'][label][dim]
                     for label in ('A', 'B')):
                item['agreements'] += 1
    return result
