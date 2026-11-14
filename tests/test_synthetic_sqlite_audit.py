"""Exercise the offline audit hook and real SQLite sink together."""
import tempfile
import unittest
from pathlib import Path

from tracing.sqlite_sink import SqliteTraceSink, TraceStoreConflict
from tracing.synthetic_sqlite_audit import sqlite_policy_audit

TASK = "12345678-1234-4234-8234-123456789abc"
TRACE = "abcdefab-cdef-4def-8def-abcdefabcdef"
DECISION = "dec-" + "a" * 32
FIRST = "2026-11-06T15:00:00Z"
LATER = "2026-11-06T16:00:00Z"


class PolicySinkWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "trace.db"
        self.sink = SqliteTraceSink.bootstrap(self.path, environment="synthetic")

    def emit(self, audit, *, task, event, when):
        with audit.bind(task_id=task, trace_id=TRACE, timestamp=when):
            audit.hook(event)

    def test_opt_in_retries_linked_and_unlinked_preserve_first_timestamp(self):
        audit = sqlite_policy_audit(self.sink, time_only_retries=True)
        linked = {"task_id": TASK, "correlation": "request",
                  "decision_id": DECISION, "outcome": "allow"}
        denial = {"task_id": None, "correlation": "missing",
                  "decision_id": "dec-" + "b" * 32, "outcome": "deny"}
        for when in (FIRST, LATER):
            self.emit(audit, task=TASK, event=linked, when=when)
            self.emit(audit, task=None, event=denial, when=when)
        self.assertEqual(len(self.sink.records()), 2)
        self.assertTrue(all(FIRST in row[1] and LATER not in row[1] for row in self.sink.records()))

    def test_strict_default_rejects_later_timestamp(self):
        audit = sqlite_policy_audit(self.sink)
        denial = {"task_id": None, "correlation": "missing",
                  "decision_id": DECISION, "outcome": "deny"}
        self.emit(audit, task=None, event=denial, when=FIRST)
        with self.assertRaises(TraceStoreConflict):
            self.emit(audit, task=None, event=denial, when=LATER)
        self.assertEqual(len(self.sink.records()), 1)

    def test_changed_outcome_conflicts_and_sink_loss_propagates(self):
        audit = sqlite_policy_audit(self.sink, time_only_retries=True)
        linked = {"task_id": TASK, "correlation": "request",
                  "decision_id": DECISION, "outcome": "allow"}
        self.emit(audit, task=TASK, event=linked, when=FIRST)
        with self.assertRaises(TraceStoreConflict):
            self.emit(audit, task=TASK, event={**linked, "outcome": "deny"}, when=LATER)
        self.path.unlink()
        with self.assertRaises(Exception):
            self.emit(audit, task=TASK, event=linked, when=LATER)
        self.assertFalse(self.path.exists())

    def test_explicit_boolean_and_offline_sink_required(self):
        with self.assertRaises(TypeError):
            sqlite_policy_audit(self.sink, time_only_retries=1)
        with self.assertRaises(TypeError):
            sqlite_policy_audit(lambda *_: None, time_only_retries=True)


if __name__ == "__main__":
    unittest.main()
