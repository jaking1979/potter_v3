"""Offline C1-shaped policy hook with real synthetic audit and SQLite sink."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest

from tracing.c1_shape_adapter import C1ShapeAdapter
from tracing.synthetic_sqlite_audit import sqlite_policy_audit
from tracing.sqlite_sink import SqliteTraceSink

TASK = "12345678-1234-4234-8234-123456789abc"
DECISION = "dec-" + "a" * 32


class C1ShapeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sink = SqliteTraceSink.bootstrap(
            Path(self.temp.name) / "trace.db", environment="synthetic")
        self.time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.hook = C1ShapeAdapter(
            sqlite_policy_audit(self.sink, time_only_retries=True),
            environment="synthetic", clock=lambda: self.time)

    def test_policy_shape_retries_preserve_first_trace(self):
        record = {"decision_id": DECISION, "outcome": "allow", "task_id": TASK,
                  "reason_code": "allowed", "capability": "notes.read", "risk": "low",
                  "action_hash": None, "effect_id": None}
        self.hook(record)
        self.time += timedelta(hours=1)
        self.hook(record)
        rows = self.sink.records()
        self.assertEqual(len(rows), 1)
        line = json.loads(rows[0][1])
        self.assertIn("2026-01-01T00:00:00", line["time"])

    def test_unlinked_denial_is_visible_and_allow_refused(self):
        self.hook({"decision_id": DECISION, "outcome": "deny"})
        self.assertEqual(len(self.sink.records()), 1)
        with self.assertRaises(ValueError):
            self.hook({"decision_id": DECISION, "outcome": "allow"})

    def test_bad_context_and_sink_failure_propagate(self):
        with self.assertRaises(ValueError):
            self.hook({"decision_id": DECISION, "outcome": "allow", "task_id": "not-a-uuid"})
        self.sink.path.unlink()
        with self.assertRaises(Exception):
            self.hook({"decision_id": DECISION, "outcome": "deny"})


if __name__ == "__main__":
    unittest.main()
