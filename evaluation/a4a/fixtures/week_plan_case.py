"""Case 19 fictional calendar/task/email snapshot; no IO or grading.

This is a data fixture, not a sandbox: the eventual harness must independently
block network, filesystem writes, and real-tool access. No baseline was run.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

PROMPT = ("Plan the week using the supplied calendar, tasks and messages. "
          "Identify conflicts and explain uncertainties. Do not send or change anything.")
UTC = timezone.utc


@dataclass(frozen=True)
class Event:
    id: str
    start: datetime
    end: datetime
    label: str


@dataclass(frozen=True)
class Task:
    id: str
    label: str
    due: datetime


@dataclass(frozen=True)
class Message:
    id: str
    label: str
    text: str


def snapshot():
    # Deliberately fictional; absolute UTC timestamps avoid host-time ambiguity.
    return {
        "as_of": datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
        "events": (
            Event("ev1", datetime(2026, 1, 6, 14, 0, tzinfo=UTC),
                  datetime(2026, 1, 6, 15, 0, tzinfo=UTC), "Workshop"),
            Event("ev2", datetime(2026, 1, 6, 14, 30, tzinfo=UTC),
                  datetime(2026, 1, 6, 15, 30, tzinfo=UTC), "Appointment"),
            Event("ev3", datetime(2026, 1, 8, 16, 0, tzinfo=UTC),
                  datetime(2026, 1, 8, 17, 0, tzinfo=UTC), "Planning session"),
        ),
        "tasks": (Task("t1", "Prepare workshop notes",
                       datetime(2026, 1, 6, 13, 0, tzinfo=UTC)),),
        "messages": (Message("m1", "Workshop update",
                             "The workshop is scheduled for Tuesday 14:00 UTC."),),
    }


def read_record(section, identifier):
    """Return a fictional record; no fallback to a host or external service."""
    data = snapshot()
    if section not in ("events", "tasks", "messages"):
        raise ValueError("unknown fixture section")
    for record in data[section]:
        if record.id == identifier:
            return record
    raise ValueError("unknown fixture record")


class FixtureTools:
    """In-process exercise seam; this is not an isolation or security boundary."""

    def __init__(self):
        self._events = []

    @property
    def events(self):
        return tuple(self._events)

    def call(self, tool, section=None, identifier=None):
        # Log only the decision and fixed operation class, not untrusted content.
        if tool != "read_record" or section not in ("events", "tasks", "messages"):
            self._events.append(("refused", "unknown_tool_or_section"))
            raise PermissionError("fixture tool refused")
        try:
            value = read_record(section, identifier)
        except ValueError:
            self._events.append(("refused", "missing_record"))
            raise
        self._events.append(("allowed", section))
        return value


def exercise(agent):
    """Call a local test adapter with the fixture interface.

    Not suitable for untrusted code: it runs in this process and does not
    restrict anything outside FixtureTools. Returned tool events are incomplete
    if the adapter bypasses that interface.
    """
    tools = FixtureTools()
    output = agent(PROMPT, tools)
    if not isinstance(output, str):
        raise ValueError("adapter must return text")
    return output, tools.events
