"""Offline A4a grade comparison from preselected Notion-style fields.

Pair identifiers are supplied externally; matching values are not proof that
graders saw the same material, that grading was blind, or that graders were
independent. No Notion calls or output text are read or stored by this module.
"""
import re
from notion_grades import convert

PAIR = re.compile(r'[0-9a-f]{64}\Z')

def compare(first, first_pair, second, second_pair):
    if (not isinstance(first_pair, str) or not PAIR.fullmatch(first_pair)
        or not isinstance(second_pair, str) or not PAIR.fullmatch(second_pair)
        or first_pair != second_pair):
        raise ValueError('pair identity mismatch')
    a, b = convert(first), convert(second)
    if a['case_id'] != b['case_id'] or a['responsibility'] != b['responsibility']:
        raise ValueError('case mismatch')
    agreements = 0
    differences = []
    for label in ('A', 'B'):
        for dim, answer in a['outputs'][label].items():
            other = b['outputs'][label][dim]
            if answer == other:
                agreements += 1
            else:
                differences.append({'label': label, 'dimension': dim, 'first': answer, 'second': other})
    return {'case_id': a['case_id'], 'comparisons': agreements + len(differences),
            'agreements': agreements, 'disagreements': differences}
