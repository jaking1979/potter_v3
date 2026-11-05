"""Validate synthetic case plans, not provenance or benchmark readiness."""
import re

_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\Z')
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')


def validate_synthetic_plan(rows):
    """Reject malformed and duplicate identities; return aggregate counts only.

    This does not prove fixtures exist, are frozen, are real tasks, represent
    approved cases, or are safe. No external services or files are accessed.
    """
    if type(rows) is not list or not rows:
        raise ValueError('nonempty plan required')
    seen = set()
    counts = {'one-shot': 0, 'responsibility': 0}
    for row in rows:
        if type(row) is not dict or set(row) != {'case_id', 'version', 'kind'}:
            raise ValueError('invalid case record')
        case, version, kind = row['case_id'], row['version'], row['kind']
        if type(case) is not str or not _ID.fullmatch(case):
            raise ValueError('invalid case ID')
        if type(version) is not str or not _VERSION.fullmatch(version):
            raise ValueError('invalid version')
        if type(kind) is not str or kind not in counts:
            raise ValueError('invalid case kind')
        if case in seen:
            raise ValueError('duplicate case ID')
        seen.add(case)
        counts[kind] += 1
    return {'cases': len(rows), 'one_shot': counts['one-shot'],
            'responsibility': counts['responsibility'],
            'note': 'Synthetic plan only; source provenance and readiness unverified.'}
