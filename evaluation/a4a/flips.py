"""Count explicit yes/no flips for one synthetic pre-aligned case.

This cannot prove mapping, blinding, exercise validity, or the absence of flips
in excluded records. It never renders a pass/fail verdict.
"""
from evaluation.a4a.blind_grading import validate_grades
from evaluation.a4a.scoring_rules import is_applicable


def two_step_flips(first, second):
    """List yes/no reversals per output for a valid, already-aligned pair."""
    validate_grades(first)
    validate_grades(second)
    if (first['case_id'] != second['case_id']
            or first['responsibility'] != second['responsibility']):
        raise ValueError('case or responsibility mismatch')
    dimensions = [d for d in ('done', 'trust', 'follow_through')
                  if is_applicable(d, first['responsibility'])]
    flips = []
    for label in ('A', 'B'):
        for dimension in dimensions:
            before = first['outputs'][label][dimension]
            after = second['outputs'][label][dimension]
            if {before, after} == {'yes', 'no'}:
                flips.append({'case_id': first['case_id'], 'output': label,
                              'dimension': dimension, 'first': before, 'second': after})
    return flips
