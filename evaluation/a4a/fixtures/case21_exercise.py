"""Offline case-21 exercise adapter. Not an isolation or security boundary.

The callback runs in this process and may access anything available to it.
Use only trusted synthetic callbacks; no baseline or production calls.
"""
from fixtures.silent_monitor_case import PROMPT, synthetic_snapshot, tool_read

FIELDS = ("evaluation_time", "monitor_cadence", "last_monitor_success",
          "last_heartbeat", "last_sensor_state")
MAX_EVENTS = 1000


class FixtureTools:
    """Expose only fixed synthetic records through this fixture interface."""
    def __init__(self, variant):
        self._snapshot = synthetic_snapshot(variant)
        self._events = []
        self._dropped = 0

    def _record(self, event):
        if len(self._events) < MAX_EVENTS:
            self._events.append(event)
        else:
            self._dropped += 1

    @property
    def events(self):
        events = tuple(self._events)
        if self._dropped:
            events += (("truncated", "event_limit", self._dropped),)
        return events

    def call(self, operation, field=None):
        if operation == "list_fields" and field is None:
            self._record(("allowed", "list_fields", None))
            return FIELDS
        if operation == "read" and field in FIELDS:
            result = tool_read(self._snapshot, field)
            self._record(("allowed", "read", field))
            return result
        # Never log arbitrary untrusted operation or field text.
        label = operation if operation in ("read", "list_fields", "send", "write") else "other"
        self._record(("refused", label, None))
        raise PermissionError("fixture operation refused")


class ExerciseFailure(ValueError):
    """Preserve mediated events when the callback fails, not bypass evidence."""
    def __init__(self, events):
        super().__init__("fixture adapter failed")
        self.events = events


def exercise(variant, adapter):
    """Run a trusted synthetic callback; this does not constrain side effects."""
    tools = FixtureTools(variant)
    try:
        output = adapter(PROMPT, tools)
        if not isinstance(output, str):
            raise ValueError("adapter must return text")
    except Exception:
        raise ExerciseFailure(tools.events) from None
    return output, tools.events
