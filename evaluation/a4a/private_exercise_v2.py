"""Synthetic v2 exercise commitment with domain-separated keyed plan and fixtures.

Use a fresh high-entropy key per exercise. This is not an authenticated freeze,
and does not change the legacy report or permit real fixture use.
"""
import hashlib
import hmac
import json
import re

from evaluation.a4a.exercise_commitment import _RULES
from evaluation.a4a.private_fixture_commitment import private_fixture_tag
from evaluation.a4a.private_plan_commitment import private_plan_tag

_SCHEMA = 'potter.a4a-private-exercise-commitment/v2'


def private_exercise_digest_v2(expected_cases, fixtures, secret):
    if type(expected_cases) is not dict or type(fixtures) is not dict:
        raise ValueError('invalid exercise')
    plan, data = expected_cases.copy(), fixtures.copy()
    if plan.keys() != data.keys():
        raise ValueError('fixture coverage mismatch')
    payload = {'schema': _SCHEMA, 'rules': _RULES,
               'private_plan': private_plan_tag(plan, secret),
               'private_fixtures': private_fixture_tag(data, secret)}
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def verify_private_exercise_v2(expected_cases, fixtures, secret, expected_digest):
    if type(expected_digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
        raise ValueError('invalid expected digest')
    if not hmac.compare_digest(private_exercise_digest_v2(expected_cases, fixtures, secret),
                               expected_digest):
        raise ValueError('exercise digest mismatch')
    return True
