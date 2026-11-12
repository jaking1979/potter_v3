"""Offline synthetic fixture commitments; not an authenticated freeze.

The caller supplies bytes and a separately protected expected digest. This
does not prove who selected fixtures, what a model saw, no-effects isolation,
or secure storage. Never include clinical or real fixture bytes in a report.
"""
import hashlib
import hmac
import json
import re

_SCHEMA = 'potter.a4a-fixture-commitment/v0'
_MAX_FIXTURE = 1024 * 1024
_MAX_TOTAL = 20 * 1024 * 1024


def fixture_manifest(fixtures):
    """Return content digests keyed by exact case IDs, never raw content."""
    if type(fixtures) is not dict or not 1 <= len(fixtures) <= 10000:
        raise ValueError('invalid fixtures')
    total = 0
    rows = []
    for case_id, raw in fixtures.items():
        if (type(case_id) is not str or not case_id.strip() or len(case_id) > 200
                or type(raw) is not bytes or len(raw) > _MAX_FIXTURE):
            raise ValueError('invalid fixtures')
        total += len(raw)
        if total > _MAX_TOTAL:
            raise ValueError('invalid fixtures')
        rows.append([case_id, hashlib.sha256(raw).hexdigest()])
    return {'schema': _SCHEMA, 'fixtures': sorted(rows)}


def fixture_digest(fixtures):
    encoded = json.dumps(fixture_manifest(fixtures), ensure_ascii=True,
                         sort_keys=True, separators=(',', ':')).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def verify_fixtures(fixtures, expected_digest):
    if type(expected_digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
        raise ValueError('invalid expected digest')
    if not hmac.compare_digest(fixture_digest(fixtures), expected_digest):
        raise ValueError('fixture digest mismatch')
    return True
