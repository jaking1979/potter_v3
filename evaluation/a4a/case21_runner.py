"""Run a fictional case with an explicitly trusted in-process callback.

This is not a sandbox, model evaluation, grading, a privacy boundary or a
baseline. A callback can bypass or forge fixture events and access live tools.
No external provider or real data is loaded by this module.
"""
from dataclasses import dataclass
from fixtures import case21_exercise as case


def _valid_event(event):
    # This validates only the recorded format, not authenticity.
    if not isinstance(event, tuple) or len(event) != 3:
        return False
    result, operation, field = event
    if result == "truncated":
        return operation == "event_limit" and type(field) is int and field > 0
    if result == "refused":
        return operation in ("read", "list_fields", "send", "write", "other") and field is None
    if result != "allowed":
        return False
    if operation == "list_fields":
        return field is None
    return operation == "read" and isinstance(field, str) and field in case.FIELDS


def _recorded_reads(events):
    return tuple(event[2] for event in events
                 if _valid_event(event) and event[0] == "allowed" and event[1] == "read")


@dataclass(frozen=True)
class ExerciseResult:
    scenario: str
    status: str
    output: str | None
    events: tuple

    @property
    def events_dropped(self):
        return sum(event[2] for event in self.events
                   if _valid_event(event) and event[0] == "truncated")

    @property
    def read_count(self):
        """Recorded reads, or None if the event log is incomplete or malformed."""
        if self.events_dropped or any(not _valid_event(event) for event in self.events):
            return None
        return len(_recorded_reads(self.events))

    @property
    def fields_read(self):
        """Recorded fields, or None for incomplete or malformed event evidence."""
        if self.events_dropped or any(not _valid_event(event) for event in self.events):
            return None
        return tuple(sorted(set(_recorded_reads(self.events))))


def run_case21(scenario, callback):
    """Run a fictional scenario with a trusted callback; do not award a score.

    A mediated refusal, malformed event or truncated log is inconclusive.
    A callback may bypass or forge this in-process log; a recorded read does
    not establish isolation or absence of effects. There is no timeout.
    """
    if scenario not in ("stale", "recent", "missing"):
        raise ValueError("unknown synthetic scenario")
    if not callable(callback):
        raise TypeError("callback must be callable")
    try:
        output, events = case.exercise(scenario, callback)
    except case.ExerciseFailure as exc:
        return ExerciseResult(scenario, "callback_failed", None, exc.events)
    if any(not _valid_event(event) for event in events):
        status = "inconclusive_malformed_events"
    elif any(event[0] == "truncated" for event in events):
        status = "inconclusive_truncated"
    elif any(event[0] == "refused" for event in events):
        status = "inconclusive_refusal"
    elif not _recorded_reads(events):
        status = "inconclusive_no_fixture_read"
    else:
        status = "completed_ungraded"
    return ExerciseResult(scenario, status, output, events)
