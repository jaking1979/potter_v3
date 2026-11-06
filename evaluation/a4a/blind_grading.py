"""Offline draft validation for blinded A4a grading; not a benchmark runner."""

DIMENSIONS = {
    'done': {'yes', 'partly', 'no'},
    'reexplaining': {'none', 'some', 'a lot'},
    'speed': {'yes', 'slow', 'too slow'},
    'trust': {'yes', 'somewhat', 'no'},
}


def validate_grades(record):
    """Validate both anonymous labels; never include a system-to-label mapping."""
    if not isinstance(record, dict) or set(record) != {'case_id', 'responsibility', 'outputs'}:
        raise ValueError('expected case_id, responsibility and outputs only')
    if not isinstance(record['case_id'], str) or not record['case_id'].strip():
        raise ValueError('case_id required')
    if type(record['responsibility']) is not bool:
        raise ValueError('responsibility must be boolean')
    outputs = record['outputs']
    if not isinstance(outputs, dict) or set(outputs) != {'A', 'B'}:
        raise ValueError('exactly two blinded outputs required')
    dimensions = dict(DIMENSIONS)
    if record['responsibility']:
        dimensions['follow_through'] = {'yes', 'partly', 'no'}
    for label in ('A', 'B'):
        answers = outputs[label]
        if not isinstance(answers, dict) or set(answers) != set(dimensions):
            raise ValueError('missing or unexpected grade field')
        for field, choices in dimensions.items():
            if not isinstance(answers[field], str) or answers[field] not in choices:
                raise ValueError('invalid answer for ' + field)
    return True
