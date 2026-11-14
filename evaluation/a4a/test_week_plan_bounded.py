"""Bounded fixture evidence; not a sandbox or trust boundary."""
import unittest
from fixtures import week_plan_case as case


class BoundedEvidence(unittest.TestCase):
    def test_refusal_flood_keeps_prefix_and_reports_drops(self):
        tools = case.FixtureTools()
        tools.MAX_EVENTS = 3
        tools.call("get_as_of")
        for _ in range(5):
            with self.assertRaises(PermissionError):
                tools.call("send")
        self.assertEqual(len(tools.events), 4)
        self.assertEqual(tools.events[0][1], "get_as_of")
        self.assertEqual(tools.events[1][1], "send")
        self.assertEqual(tools.events[-1], ("truncated", None, None, 3))

    def test_failed_exercise_discloses_truncation(self):
        def agent(prompt, tools):
            tools.MAX_EVENTS = 1
            for _ in range(3):
                try:
                    tools.call("send")
                except PermissionError:
                    pass
            raise RuntimeError("private")
        with self.assertRaises(case.ExerciseFailure) as raised:
            case.exercise(agent)
        self.assertEqual(raised.exception.events[-1], ("truncated", None, None, 2))
        self.assertNotIn("private", str(raised.exception))

    def test_default_limit_is_bounded(self):
        tools = case.FixtureTools()
        for _ in range(1010):
            try:
                tools.call("send")
            except PermissionError:
                pass
        self.assertEqual(len(tools.events), 1001)
        self.assertEqual(tools.events[-1], ("truncated", None, None, 10))
