"""Synthetic keyed fixture commitment experiment, never a fixture freeze.

Only a single aggregate tag is emitted, not case IDs or per-fixture hashes.
The caller must supply a randomly generated secret through a protected channel,
keep it out of reports and protect the expected tag separately. This module
cannot establish key custody, source identity, exercise provenance, isolation,
or what an evaluator saw. Do not use real fixture data with this experiment.
"""
import hashlib
import hmac
import re

_DOMAIN = b'potter.a4a-private-fixtures/v1\x00'
_MAX_FIXTURE = 1024 * 1024
_MAX_TOTAL = 20 * 1024 * 1024


def _part(mac, value):
    mac.update(len(value).to_bytes(8, 'big'))
    mac.update(value)


def private_fixture_tag(fixtures, secret):
    """Return an HMAC-SHA256 tag over a bounded snapshot of exact ID/bytes pairs."""
    if type(secret) is not bytes or not 32 <= len(secret) <= 64:
        raise ValueError('invalid commitment key')
    if type(fixtures) is not dict or not 1 <= len(fixtures) <= 10000:
        raise ValueError('invalid fixtures')
    entries = fixtures.copy()
    rows = []
    total = 0
    for case_id, raw in entries.items():
        if (type(case_id) is not str or not case_id.strip() or len(case_id) > 200
                or type(raw) is not bytes or len(raw) > _MAX_FIXTURE):
            raise ValueError('invalid fixtures')
        try:
            encoded_id = case_id.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise ValueError('invalid fixtures') from exc
        total += len(raw)
        if total > _MAX_TOTAL:
            raise ValueError('invalid fixtures')
        rows.append((encoded_id, raw))
    mac = hmac.new(secret, _DOMAIN, hashlib.sha256)
    mac.update(len(rows).to_bytes(8, 'big'))
    for case_id, raw in sorted(rows):
        _part(mac, case_id)
        _part(mac, raw)
    return mac.hexdigest()


def verify_private_fixtures(fixtures, secret, expected_tag):
    if type(expected_tag) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_tag):
        raise ValueError('invalid expected tag')
    if not hmac.compare_digest(private_fixture_tag(fixtures, secret), expected_tag):
        raise ValueError('fixture tag mismatch')
    return True
