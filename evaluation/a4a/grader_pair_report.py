"""Experimental offline bridge for synthetic grader pairs; not a grading gate.

The caller must independently lock the plan, establish blinding, alignment,
provenance, and no-effects isolation. This function proves none of those.
Applicability and L17 wording remain unresolved; this is not a ratified rule.
"""
from evaluation.a4a.case_agreement import count_case_agreement
from evaluation.a4a.applicability_report import summarize_applicability
from evaluation.a4a.scoring_rules import DIMENSIONS, is_applicable


def summarize_grader_pairs(first, second, expected_cases):
    """Compare two aligned passes using one independently supplied case plan.

    Follow-through applies only where the plan says responsibility=True.
    A missing, duplicate, or invalid record remains affected with its reason;
    the underlying summary forces affected cases to disagree in every
    applicable dimension. Output remains descriptive, never a pass.
    """
    # Validate both complete inputs before indexing or reporting anything.
    count_case_agreement(first, second, expected_cases)
    planned = list(expected_cases)
    dimensions = DIMENSIONS
    applicability = {d: [case for case in planned
                         if is_applicable(d, expected_cases[case])]
                     for d in dimensions}
    applicable_sets = {d: set(cases) for d, cases in applicability.items()}
    grouped = []
    for records in (first, second):
        by_case = {case: [] for case in planned}
        for row in records:
            by_case[row['case_id']].append(row)
        grouped.append(by_case)
    classifications = {d: {} for d in dimensions}
    reasons = {}
    for case in planned:
        one = count_case_agreement(grouped[0][case], grouped[1][case],
                                   {case: expected_cases[case]})
        for dim in dimensions:
            if case not in applicable_sets[dim]:
                continue
            affected = one[dim]['affected']
            if affected:
                classifications[dim][case] = 'unalignable'
                reasons.setdefault(case, {})[dim] = affected[0]['reason']
            else:
                classifications[dim][case] = ('agree' if one[dim]['agreements'] == 1
                                               else 'disagree')
    result = summarize_applicability(planned, applicability, classifications)
    result['source_reasons'] = reasons
    return result
