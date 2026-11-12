"""Experimental synthetic report bound to a supplied exercise commitment.

This is not an authenticated lock or proof of model input, provenance,
blinding, independent grading, or side-effect isolation.
"""
from evaluation.a4a.exercise_commitment import exercise_digest, verify_exercise
from evaluation.a4a.grader_pair_report import summarize_grader_pairs


def summarize_exercise(first, second, expected_cases, fixtures, expected_digest):
    if type(expected_cases) is not dict or type(fixtures) is not dict:
        raise ValueError('invalid exercise')
    # Copy once, then verify and report against the same snapshot. Fixture
    # values are bytes and case values are bool, validated by verify_exercise.
    plan, data = expected_cases.copy(), fixtures.copy()
    verify_exercise(plan, data, expected_digest)
    report = summarize_grader_pairs(first, second, plan)
    report['exercise_digest'] = exercise_digest(plan, data)
    return report
