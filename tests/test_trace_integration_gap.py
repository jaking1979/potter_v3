"""Offline real-component probe: E2/C2/C1 -> C6 task linkage.

Uses E2's existing SQLite-backed World and fake provider; no live services.
This demonstrates a missing integration seam, not C6 acceptance.
"""
import tempfile
import unittest

import resp_world as rw
from tracing.trace import DecisionTrace


def c6_from_hook(record):
    # The actual C1 trace-hook record is passed without a caller-supplied task
    # association. C6 must reject it rather than invent a task relationship.
    return DecisionTrace(
        task_id=record.get("task_id"),
        trace_id="abcdefab-cdef-4def-8def-abcdefabcdef",
        component="policy",
        outcome=record["outcome"],
        decision_digest="a" * 64,
        environment="synthetic",
        timestamp="2026-11-13T20:00:00Z",
        subject_id=record["decision_id"],
    )


class TraceLinkIntegrationProbe(unittest.TestCase):
    def test_real_runner_policy_hook_has_no_task_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = rw.World(tmp, definitions=[rw.batch(n=1)])
            captured = []
            world.engine._trace = captured.append
            tasks = world.runner.tick()
            self.assertEqual(len(tasks), 1)
            result = world.runner.run_once()
            self.assertEqual(result.outcome, "done")
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["outcome"], "allow")
            self.assertNotIn("task_id", captured[0])
            with self.assertRaises(ValueError):
                c6_from_hook(captured[0])
            self.assertEqual(world.runner.tasks.get(tasks[0].id).state, "done")

    def test_c6_rejecting_missing_task_prevents_fake_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = rw.World(tmp, definitions=[rw.batch(n=1)])
            captured = []
            def trace(record):
                captured.append(record)
                c6_from_hook(record)
            world.engine._trace = trace
            world.runner.tick()
            world.runner.run_once()
            self.assertEqual(len(captured), 1)
            self.assertEqual(world.ext.calls(), 0)


if __name__ == "__main__":
    unittest.main()
