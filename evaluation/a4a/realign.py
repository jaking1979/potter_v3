"""Offline grade realignment for synthetic blinded-pair experiments.

The caller must independently authenticate both mappings and restrict their
visibility from graders. This helper checks consistency, not provenance,
blinding, exercise validity or permission to run an evaluation.
"""
import copy
import re
from blind_grading import validate_grades

_IDENTITY = re.compile(r'[0-9a-f]{64}\Z')


def realign_pair(first, second, first_map, second_map):
    """Align second-pass A/B to first-pass A/B using supplied output hashes.

    The mappings must name the same two distinct hashes. No mapping or identity
    is returned with grader records. Outputs are copied rather than aliased.
    """
    validate_grades(first)
    validate_grades(second)
    if first['case_id'] != second['case_id'] or first['responsibility'] != second['responsibility']:
        raise ValueError('case or responsibility mismatch')
    for mapping in (first_map, second_map):
        if type(mapping) is not dict or set(mapping) != {'A', 'B'}:
            raise ValueError('two-label mapping required')
        if any(not isinstance(v, str) or not _IDENTITY.fullmatch(v) for v in mapping.values()):
            raise ValueError('invalid output identity')
        if mapping['A'] == mapping['B']:
            raise ValueError('ambiguous duplicate outputs')
    if set(first_map.values()) != set(second_map.values()):
        raise ValueError('different output pair')
    reverse = {output_id: label for label, output_id in second_map.items()}
    aligned_first = copy.deepcopy(first)
    aligned_second = {'case_id': second['case_id'], 'responsibility': second['responsibility'],
                      'outputs': {label: copy.deepcopy(second['outputs'][reverse[first_map[label]]])
                                  for label in ('A', 'B')}}
    return aligned_first, aligned_second
