"""Characterization only: actual E2/C1/C2/C6, synthetic DBs and fake provider.

The stranded-approval behavior is a known defect, not an acceptance criterion.
Renaming the closed synthetic sink preserves its existing chain for restoration.
"""
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

import resp_world as rw
from tracing.c1_shape_adapter import C1ShapeAdapter
from tracing.synthetic_sqlite_audit import sqlite_policy_audit
from tracing.sqlite_sink import SqliteTraceSink


class ApprovedOutageProbe(unittest.TestCase):
    def ready(self, tmp):
        w = rw.World(tmp, definitions=[rw.bill_watch()])
        sink = SqliteTraceSink.bootstrap(Path(tmp) / "trace.db", environment="synthetic")
        w.engine._trace = C1ShapeAdapter(
            sqlite_policy_audit(sink, time_only_retries=True),
            environment="synthetic",
            clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
        w.runner.on_event("bill.due", {"id": "probe", "amount": 5})
        waiting = w.runner.run_once()
        self.assertEqual(waiting.outcome, "waiting_approval")
        self.assertEqual(w.say(f"/approve {waiting.detail}").outcome, "approved")
        w.runner.poll_approvals()
        return w, sink, waiting

    def test_outage_exhaustion_strands_consumed_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, sink, waiting = self.ready(tmp)
            backup = sink.path.with_suffix(".backup")
            sink.path.rename(backup)
            self.assertEqual([w.runner.run_once().outcome for _ in range(3)],
                             ["retry", "retry", "escalated"])
            self.assertEqual(w.ext.calls("pay"), 0)
            self.assertEqual(w.runner.tasks.get(waiting.task_id).state, "escalated")
            self.assertEqual([e.status for e in w.runner._journal.unresolved()], ["pending"])
            backup.rename(sink.path)
            self.assertEqual(w.runner.poll_approvals(), [])
            self.assertIsNone(w.runner.run_once())
            self.assertEqual(w.runner.recover(), [])

    def test_short_outage_resumes_and_inspects_real_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, sink, waiting = self.ready(tmp)
            attempted = []
            hook = w.engine._trace
            def observe(record):
                attempted.append(dict(record))
                return hook(record)
            w.engine._trace = observe
            backup = sink.path.with_suffix(".backup")
            sink.path.rename(backup)
            self.assertEqual(w.runner.run_once().outcome, "retry")
            self.assertEqual(w.ext.calls("pay"), 0)
            backup.rename(sink.path)
            self.assertEqual(w.runner.run_once().outcome, "done")
            self.assertEqual(w.ext.calls("pay"), 1)
            rows = [json.loads(row[1]) for row in sink.records()]
            approved = [r for r in attempted if r["reason_code"] == "ok_approved"]
            reconciled = [r for r in attempted if r["reason_code"] == "reconcile_pending_intent"]
            self.assertEqual(len(approved), 1)
            self.assertEqual(len(reconciled), 1)
            subjects = {r.get("potter.subject.id") for r in rows}
            self.assertNotIn(approved[0]["decision_id"], subjects)
            self.assertIn(reconciled[0]["decision_id"], subjects)
