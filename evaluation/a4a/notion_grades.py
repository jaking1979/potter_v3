"""Offline adapter for the draft A4a Notion grading fields.

Accepts explicit, preselected categorical fields only. It does not access Notion,
read outputs, prove blinding, or establish independent grading or authorization.
"""
from evaluation.a4a.blind_grading import validate_grades

DIMENSIONS = {
    'Done': 'done', 'Re-explaining': 'reexplaining',
    'Fast enough': 'speed', 'Trust': 'trust',
}

def convert(row):
    if not isinstance(row, dict):
        raise ValueError('row must be an object')
    kind = row.get('Scenario type')
    if kind not in ('One-shot', 'Responsibility'):
        raise ValueError('scenario type required')
    dims = dict(DIMENSIONS)
    if kind == 'Responsibility':
        dims['Follow-through'] = 'follow_through'
    required = {'Task', 'Scenario type'} | {f'{label} · {key}' for label in ('A', 'B') for key in dims}
    if set(row) != required:
        raise ValueError('missing or unexpected fields; select grading fields only')
    outputs = {}
    for label in ('A', 'B'):
        outputs[label] = {}
        for source, dest in dims.items():
            value = row[f'{label} · {source}']
            if not isinstance(value, str):
                raise ValueError('missing categorical answer')
            outputs[label][dest] = value.lower()
    result = {'case_id': row['Task'], 'responsibility': kind == 'Responsibility', 'outputs': outputs}
    validate_grades(result)
    return result
