"""Synthetic private-v2 exercise report, not an authenticated grading gate.

The digest does not reveal unkeyed plan/fixture hashes. The descriptive report
can still disclose case IDs, applicability counts, and grading outcomes; it is
not a privacy-safe publication artifact. Key custody, blinding, provenance,
and proof of model inputs are external and not established here.
"""
from evaluation.a4a.grader_pair_report import summarize_grader_pairs
from evaluation.a4a.private_exercise_v2 import verify_private_exercise_v2


def summarize_private_exercise(first, second, expected_cases, fixtures, secret, expected_digest):
    if type(expected_cases) is not dict or type(fixtures) is not dict:
        raise ValueError('invalid exercise')
    # Both inputs hold immutable bool/bytes values, validated by verifier.
    # Verify and grade the same independent copies, not the caller's objects.
    plan, data = expected_cases.copy(), fixtures.copy()
    verify_private_exercise_v2(plan, data, secret, expected_digest)
    report = summarize_grader_pairs(first, second, plan)
    report['private_exercise_digest'] = expected_digest
    return report
