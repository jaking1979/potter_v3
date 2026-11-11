"""Count explicit yes/no flips for one synthetic pre-aligned case.

This cannot prove mapping, blinding, exercise validity, or the absence of flips
in excluded records. It never renders a pass/fail verdict.
"""
from evaluation.a4a.blind_grading import validate_grades


def two_step_flips(first, second):
    """List yes/no reversals per output for a valid, already-aligned pair."""
    validate_grades(first)
    validate_grades(second)
    if (first['case_id'] != second['case_id']
            or first['responsibility'] != second['responsibility']):
        raise ValueError('case or responsibility mismatch')
    dimensions = ['done', 'trust']
    if first['responsibility']:
        dimensions.append('follow_through')
    flips = []
    for label in ('A', 'B'):
        for dimension in dimensions:
            before = first['outputs'][label][dimension]
            after = second['outputs'][label][dimension]
            if {before, after} == {'yes', 'no'}:
                flips.append({'case_id': first['case_id'], 'output': label,
                              'dimension': dimension, 'first': before, 'second': after})
    return flips
