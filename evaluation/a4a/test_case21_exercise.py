import unittest
from fixtures import case21_exercise as case


class ExerciseTests(unittest.TestCase):
    def test_discovery_and_real_fixture_controls(self):
        def adapter(prompt, tools):
            names = tools.call("list_fields")
            self.assertIn("last_monitor_success", names)
            age = tools.call("read", "evaluation_time") - tools.call("read", "last_monitor_success")
            return str(age)
        stale, stale_events = case.exercise("stale", adapter)
        recent, _ = case.exercise("recent", adapter)
        self.assertEqual(stale, "3:00:00")
        self.assertEqual(recent, "0:10:00")
        self.assertEqual(stale_events[0], ("allowed", "list_fields", None))

    def test_missing_remains_unknown(self):
        def adapter(prompt, tools):
            return str(tools.call("read", "last_monitor_success"))
        self.assertEqual(case.exercise("missing", adapter)[0], "None")

    def test_refusal_and_failure_preserves_bounded_evidence(self):
        def adapter(prompt, tools):
            tools.call("send", "private-attacker-text")
        with self.assertRaises(case.ExerciseFailure) as raised:
            case.exercise("stale", adapter)
        self.assertEqual(raised.exception.events, (("refused", "send", None),))
        self.assertNotIn("private-attacker-text", str(raised.exception.events))

    def test_bounded_log_makes_truncation_visible(self):
        tools = case.FixtureTools("stale")
        for _ in range(case.MAX_EVENTS + 5):
            try:
                tools.call("write")
            except PermissionError:
                pass
        self.assertEqual(len(tools.events), case.MAX_EVENTS + 1)
        self.assertEqual(tools.events[-1], ("truncated", "event_limit", 5))

    def test_bad_variant_and_output_fail_closed(self):
        with self.assertRaises(ValueError):
            case.exercise("live", lambda prompt, tools: "text")
        with self.assertRaises(case.ExerciseFailure):
            case.exercise("stale", lambda prompt, tools: None)
