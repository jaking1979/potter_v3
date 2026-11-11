"""Synthetic plan-digest binding for the experimental grader-pair report.

A digest is an identifier, not evidence of an independently locked plan, grader
blinding, identity, provenance, or effect isolation. The caller must obtain the
expected digest from a separate trusted process before running the exercise.
"""
import hashlib
import hmac
import json
import re

from evaluation.a4a.grader_pair_report import summarize_grader_pairs

_SCHEMA = 'potter.a4a-case-plan/v1'


def plan_digest(expected_cases):
    """Hash a bounded, deterministic snapshot of case IDs and responsibility flags."""
    if type(expected_cases) is not dict or not 20 <= len(expected_cases) <= 10000:
        raise ValueError('invalid plan')
    if any(type(case) is not str or not case.strip() or len(case) > 200 or
           type(responsibility) is not bool
           for case, responsibility in expected_cases.items()):
        raise ValueError('invalid plan')
    plan = {case: expected_cases[case] for case in sorted(expected_cases)}
    payload = {'schema': _SCHEMA, 'cases': [[case, flag] for case, flag in plan.items()]}
    encoded = json.dumps(payload, ensure_ascii=True, separators=(',', ':'),
                         sort_keys=True, allow_nan=False).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def summarize_bound_pairs(first, second, expected_cases, expected_digest):
    """Refuse an unexpected plan before reporting synthetic grades.

    This local comparison is only useful if the caller obtained and protected
    the expected digest independently. It does not sign or authenticate inputs.
    """
    if type(expected_digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
        raise ValueError('invalid expected digest')
    # Work on a fresh ordinary dict so subsequent caller mutations cannot alter
    # applicability between digesting and summary calculation.
    if type(expected_cases) is not dict:
        raise ValueError('invalid plan')
    plan = expected_cases.copy()
    digest = plan_digest(plan)
    if not hmac.compare_digest(digest, expected_digest):
        raise ValueError('plan digest mismatch')
    result = summarize_grader_pairs(first, second, plan)
    result['plan_schema'] = _SCHEMA
    result['plan_digest'] = digest
    return result
