"""Synthetic L17 descriptive report; never an acceptance or safety gate.

Inputs must already be aligned through a separately protected mapping. This
cannot prove blinding, independence, provenance, timing, or no-effects isolation.
"""
from case_agreement import count_case_agreement, affected_case_summary
from prevalence import synthetic_usable_prevalence
from flips import two_step_flips
from wilson_interval import wilson_95


def synthetic_report(first, second, expected_cases):
    """Summarize fixed-plan synthetic pairs without producing a pass/fail.

    Affected cases count as disagreement in the fixed agreement denominator;
    prevalence and observable flips describe only valid, aligned records. An
    invalid exercise (>10% affected) is not scored. Returned intervals assume
    independent case outcomes, which this helper cannot establish.
    """
    counts = count_case_agreement(first, second, expected_cases)
    affected = affected_case_summary(counts, expected_cases)
    summary = {'planned_cases': affected['planned_cases'],
               'affected_case_ids': affected['affected_case_ids'],
               'invalid_over_ten_percent': affected['invalid_over_ten_percent'],
               'agreement': None, 'prevalence': None, 'observed_flips': None,
               'note': 'Synthetic only; case independence and mapping unverified.'}
    if affected['invalid_over_ten_percent']:
        return summary
    excluded = set(affected['affected_case_ids'])
    usable = set(expected_cases) - excluded
    first_by_id = {r['case_id']: r for r in first if r['case_id'] in usable}
    second_by_id = {r['case_id']: r for r in second if r['case_id'] in usable}
    observed = []
    for case_id in sorted(usable):
        observed.extend(two_step_flips(first_by_id[case_id], second_by_id[case_id]))
    agreement = {}
    for name, value in counts.items():
        n = value['denominator']
        agreement[name] = {'agreements': value['agreements'], 'denominator': n,
                           'wilson_95': wilson_95(value['agreements'], n) if n else None}
    summary['agreement'] = agreement
    summary['prevalence'] = synthetic_usable_prevalence(first, second, expected_cases)['prevalence']
    summary['observed_flips'] = observed
    return summary
