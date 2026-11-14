"""Case21 orchestration with fictional fixture and trusted callbacks."""
import unittest
from case21_runner import run_case21


class RunnerTests(unittest.TestCase):
    def test_all_controls_run_without_inventing_scores(self):
        def callback(prompt, tools):
            return str(tools.call("read", "last_monitor_success"))
        for scenario in ("stale", "recent", "missing"):
            with self.subTest(scenario=scenario):
                result = run_case21(scenario, callback)
                self.assertEqual(result.status, "completed_ungraded")
                self.assertEqual(result.events[0][0], "allowed")

    def test_swallowed_refusal_is_inconclusive(self):
        def callback(prompt, tools):
            try:
                tools.call("send")
            except PermissionError:
                pass
            return "draft"
        result = run_case21("stale", callback)
        self.assertEqual(result.status, "inconclusive_refusal")
        self.assertEqual(result.events[0][0], "refused")

    def test_uncaught_failure_preserves_mediated_events(self):
        def callback(prompt, tools):
            tools.call("send")
        result = run_case21("stale", callback)
        self.assertEqual(result.status, "callback_failed")
        self.assertIsNone(result.output)
        self.assertEqual(result.events[0][0], "refused")

    def test_overflow_never_reports_clean_completion(self):
        def callback(prompt, tools):
            for _ in range(1001):
                tools.call("list_fields")
            return "draft"
        result = run_case21("stale", callback)
        self.assertEqual(result.status, "inconclusive_truncated")
        self.assertEqual(result.events[-1][0], "truncated")

    def test_invalid_scenario_fails_before_callback(self):
        called = []
        with self.assertRaises(ValueError):
            run_case21("real", lambda *args: called.append(True))
        self.assertFalse(called)


class MalformedEventShapeTests(unittest.TestCase):
    def test_each_malformed_guard_is_inconclusive(self):
        # Events are forgeable: this pins parser behavior, not authenticity.
        from unittest.mock import patch
        malformed = (
            ("allowed", "read", "evaluation_time", "extra"),
            ("truncated", "event_limit", 0),
            ("truncated", "other", 3),
            ("refused", "read", "evaluation_time"),
            ("allowed", "list_fields", "evaluation_time"),
            ("weird", "read", "evaluation_time"),
        )
        for event in malformed:
            with self.subTest(event=event), patch("case21_runner.case.exercise", return_value=("draft", (event,))):
                result = run_case21("stale", lambda *_: "draft")
                self.assertEqual(result.status, "inconclusive_malformed_events")
                self.assertIsNone(result.read_count)
                self.assertIsNone(result.fields_read)


if __name__ == "__main__":
    unittest.main()
