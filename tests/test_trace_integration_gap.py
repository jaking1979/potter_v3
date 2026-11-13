"""Offline E2/C1/C6 correlation tests; fake provider and local DB only."""
import tempfile
import unittest
import uuid

import resp_world as rw
from policy import Caller, PolicyRequest
from policy._contracts import approval as c0
from tracing.trace import DecisionTrace


def c6_from_hook(record):
    return DecisionTrace(
        task_id=record.get("task_id"),
        trace_id="abcdefab-cdef-4def-8def-abcdefabcdef",
        component="policy", outcome=record["outcome"],
        decision_digest="a" * 64, environment="synthetic",
        timestamp="2026-11-13T20:00:00Z", subject_id=record["decision_id"])


class TraceLinkIntegrationProbe(unittest.TestCase):
    def test_real_runner_correlates_policy_decision_to_claimed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = rw.World(tmp, definitions=[rw.batch(n=1)])
            captured = []
            world.engine._trace = captured.append
            task = world.runner.tick()[0]
            result = world.runner.run_once()
            self.assertEqual(result.outcome, "done")
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["task_id"], task.id)
            self.assertEqual(c6_from_hook(captured[0]).task_id, task.id)

    def test_hook_failure_prevents_fake_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = rw.World(tmp, definitions=[rw.batch(n=1)])
            def bad_hook(record):
                c6_from_hook(record)
                raise OSError("synthetic sink failure")
            world.engine._trace = bad_hook
            world.runner.tick()
            world.runner.run_once()
            self.assertEqual(world.ext.calls(), 0)

    def test_c1_refuses_swapped_or_malformed_task_correlation(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = rw.World(tmp, definitions=[rw.batch(n=1)])
            a = str(uuid.uuid4())
            b = str(uuid.uuid4())
            action = c0.BoundAction(a, "notes.append", "append", "test", {})
            request = PolicyRequest(Caller("agent:potter"), "notes.append", action)
            for candidate in (b, "not-a-uuid", a.upper()):
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    world.engine.decide(request, trace_task_id=candidate)

    def test_missing_opt_in_remains_unlinked(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = rw.World(tmp, definitions=[rw.batch(n=1)])
            captured = []
            world.engine._trace = captured.append
            task = str(uuid.uuid4())
            action = c0.BoundAction(task, "notes.append", "append", "test", {})
            world.engine.decide(PolicyRequest(Caller("agent:potter"), "notes.append", action))
            self.assertNotIn("task_id", captured[0])


if __name__ == "__main__":
    unittest.main()
