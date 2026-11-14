"""Run a fictional case with an explicitly trusted in-process callback.

This is not a sandbox, model evaluation, grading, a privacy boundary or a
baseline. A callback can bypass or forge fixture events and access live tools.
No external provider or real data is loaded by this module.
"""
from dataclasses import dataclass
from fixtures import case21_exercise as case


@dataclass(frozen=True)
class ExerciseResult:
    scenario: str
    status: str
    output: str | None
    events: tuple

    @property
    def read_count(self):
        """Number of recorded fixture reads, not proof of isolation."""
        return sum(1 for event in self.events
                   if len(event) == 3 and event[0] == "allowed"
                   and event[1] == "read" and event[2] in case.FIELDS)

    @property
    def fields_read(self):
        """Unique recorded field names; in-process event logs can be forged."""
        return tuple(sorted({event[2] for event in self.events
                            if len(event) == 3 and event[0] == "allowed"
                            and event[1] == "read" and event[2] in case.FIELDS}))


def run_case21(scenario, callback):
    """Run one fictional scenario; do not award a score.

    A mediated refusal or truncated log is visibly inconclusive; the caller
    must not interpret a completed callback as proof of zero side effects.\n    This in-process callback has no timeout; a mediated read is not isolation.
    """
    if scenario not in ("stale", "recent", "missing"):
        raise ValueError("unknown synthetic scenario")
    if not callable(callback):
        raise TypeError("callback must be callable")
    try:
        output, events = case.exercise(scenario, callback)
    except case.ExerciseFailure as exc:
        return ExerciseResult(scenario, "callback_failed", None, exc.events)
    if any(event[0] == "truncated" for event in events):
        status = "inconclusive_truncated"
    elif any(event[0] == "refused" for event in events):
        status = "inconclusive_refusal"
    elif not any(event[0] == "allowed" and event[1] == "read" for event in events):
        status = "inconclusive_no_fixture_read"
    else:
        status = "completed_ungraded"
    return ExerciseResult(scenario, status, output, events)
