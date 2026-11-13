"""Synthetic keyed exercise commitment; not an authenticated exercise freeze.

Use a fresh cryptographically random secret for each experiment. Never publish
the secret or unkeyed fixture digests. This helper is not wired to real grading.
"""
import hashlib
import hmac
import json
import re

from evaluation.a4a.exercise_commitment import _RULES
from evaluation.a4a.plan_binding import plan_digest
from evaluation.a4a.private_fixture_commitment import private_fixture_tag

_SCHEMA = 'potter.a4a-private-exercise-commitment/v1'


def private_exercise_digest(expected_cases, fixtures, secret):
    if type(expected_cases) is not dict or type(fixtures) is not dict:
        raise ValueError('invalid exercise')
    plan, data = expected_cases.copy(), fixtures.copy()
    if plan.keys() != data.keys():
        raise ValueError('fixture coverage mismatch')
    payload = {'schema': _SCHEMA, 'rules': _RULES, 'plan': plan_digest(plan),
               'private_fixtures': private_fixture_tag(data, secret)}
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def verify_private_exercise(expected_cases, fixtures, secret, expected_digest):
    if type(expected_digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
        raise ValueError('invalid expected digest')
    if not hmac.compare_digest(private_exercise_digest(expected_cases, fixtures, secret),
                               expected_digest):
        raise ValueError('exercise digest mismatch')
    return True
