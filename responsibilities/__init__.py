"""E2: responsibility runner (T16). Staging-only (T22): connectors are injected;
only offline fakes exist in this repository."""
from .definitions import (DefinitionError, Event, Responsibility, Schedule, Step,
                          StepContext)
from .runner import (OPS_JOB, Connector, KillSwitch, ResponsibilityRunner, RunResult,
                     kill_switch_for, receipt_link)

__all__ = [n for n in dir() if not n.startswith("_")]
