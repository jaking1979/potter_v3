"""Synthetic plan/fixture coverage commitment, not an authenticated lock.

Callers must independently protect the expected digest. This does not prove
fixture provenance, blinding, what models saw, or absence of side effects.
"""
import hashlib
import hmac
import json
import re

from evaluation.a4a.fixture_commitment import fixture_digest
from evaluation.a4a.plan_binding import plan_digest

_SCHEMA = 'potter.a4a-exercise-commitment/v1'
# This is an experimental descriptor, not an L17 decision or attestation that
# code implements the descriptor. Any rules change requires a version bump;
# protected source/version binding is still required before a real exercise.
_RULES = {'version': 'synthetic-grader-pair/v1',
          'dimensions': ['done', 'reexplaining', 'speed', 'trust', 'follow_through'],
          'follow_through': 'responsibility-only',
          'affected_cases': 'union-across-planned-dimensions',
          'invalid_when': 'affected-times-10-greater-than-planned',
          'classifications': ['agree', 'disagree', 'missing', 'duplicate', 'unalignable'],
          'result': 'descriptive-only'}


def exercise_digest(expected_cases, fixtures):
    if type(expected_cases) is not dict or type(fixtures) is not dict:
        raise ValueError('invalid exercise')
    # Snapshot before validation so one calculation uses one pair of mappings.
    plan, data = expected_cases.copy(), fixtures.copy()
    if plan.keys() != data.keys():
        raise ValueError('fixture coverage mismatch')
    payload = {'schema': _SCHEMA, 'rules': _RULES, 'plan': plan_digest(plan),
               'fixtures': fixture_digest(data)}
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def verify_exercise(expected_cases, fixtures, expected_digest):
    if type(expected_digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
        raise ValueError('invalid expected digest')
    if not hmac.compare_digest(exercise_digest(expected_cases, fixtures), expected_digest):
        raise ValueError('exercise digest mismatch')
    return True
