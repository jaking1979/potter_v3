"""Synthetic keyed plan commitment. Not an authenticated plan freeze.

Use a fresh cryptographically random secret per exercise and never publish it.
The caller supplies and protects the expected tag. No real fixture or plan data.
"""
import hashlib
import hmac
import json
import re

_DOMAIN = b'potter.a4a-private-plan/v1\x00'


def private_plan_tag(expected_cases, secret):
    if type(secret) is not bytes or not 32 <= len(secret) <= 64:
        raise ValueError('invalid commitment key')
    if type(expected_cases) is not dict or not 20 <= len(expected_cases) <= 10000:
        raise ValueError('invalid plan')
    plan = expected_cases.copy()
    if not 20 <= len(plan) <= 10000:
        raise ValueError('invalid plan')
    if any(type(case) is not str or not case.strip() or len(case) > 200 or
           type(flag) is not bool for case, flag in plan.items()):
        raise ValueError('invalid plan')
    payload = {'cases': [[case, plan[case]] for case in sorted(plan)]}
    encoded = json.dumps(payload, ensure_ascii=True, separators=(',', ':'),
                         sort_keys=True, allow_nan=False).encode('ascii')
    return hmac.new(secret, _DOMAIN + encoded, hashlib.sha256).hexdigest()


def verify_private_plan(expected_cases, secret, expected_tag):
    if type(expected_tag) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_tag):
        raise ValueError('invalid expected tag')
    if not hmac.compare_digest(private_plan_tag(expected_cases, secret), expected_tag):
        raise ValueError('plan tag mismatch')
    return True
