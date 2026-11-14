"""Offline C1/E2/C2 integration with actual synthetic C6 SQLite sink.

No actual providers, no authentication guarantee, no trace/effect atomicity.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import resp_world as rw
from tracing.c1_shape_adapter import C1ShapeAdapter
from tracing.synthetic_sqlite_audit import sqlite_policy_audit
from tracing.sqlite_sink import SqliteTraceSink


class ActualHookIntegration(unittest.TestCase):
    def make_world(self, tmp):
        world = rw.World(tmp, definitions=[rw.batch(n=1)])
        sink = SqliteTraceSink.bootstrap(Path(tmp) / "synthetic-traces.db", environment="synthetic")
        audit = sqlite_policy_audit(sink, time_only_retries=True)
        hook = C1ShapeAdapter(audit, environment="synthetic",
                              clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
        world.engine._trace = hook
        return world, sink

    def test_real_runner_policy_and_sink_link_claimed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, sink = self.make_world(tmp)
            task = world.runner.tick()[0]
            result = world.runner.run_once()
            self.assertEqual(result.outcome, "done")
            self.assertEqual(world.ext.calls(), 1)
            rows = sink.records()
            self.assertEqual(len(rows), 1)
            record = json.loads(rows[0][1])
            self.assertEqual(record["task.id"], task.id)
            self.assertEqual(record["potter.outcome"], "allow")
            self.assertEqual(record["potter.component"], "policy")

    def test_real_engine_malformed_request_persists_unlinked_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, sink = self.make_world(tmp)
            decision = world.engine.decide(None)
            self.assertEqual(decision.outcome, "deny")
            record = json.loads(sink.records()[0][1])
            self.assertEqual(record["schema"], "potter.trace.uncorrelated-denial/v0")
            self.assertNotIn("task.id", record)

    def test_missing_sink_blocks_fake_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, sink = self.make_world(tmp)
            sink.path.unlink()
            world.runner.tick()
            world.runner.run_once()
            self.assertEqual(world.ext.calls(), 0)
            self.assertFalse(sink.path.exists())


if __name__ == "__main__":
    unittest.main()
