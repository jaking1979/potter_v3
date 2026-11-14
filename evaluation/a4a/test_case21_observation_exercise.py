"""Synthetic case 21 exercise: fixture-only, no model or sandbox."""
from datetime import timedelta
import unittest
from fixtures import case21_exercise as case


def inspect_snapshot(prompt, tools):
    assert "fictional historical fixture" in prompt
    names = tools.call("list_fields")
    observed = {name: tools.call("read", name) for name in names}
    if observed["last_monitor_success"] is None:
        return "unknown"
    age = observed["evaluation_time"] - observed["last_monitor_success"]
    if age > 2 * observed["monitor_cadence"]:
        return "stale"
    return "recent"


class EndToEndSyntheticExercise(unittest.TestCase):
    def test_provenance_and_distinct_heartbeat_are_observable(self):
        tools = case.FixtureTools("stale")
        self.assertIn("fictional", tools.call("read", "data_source"))
        self.assertIn("separate", tools.call("read", "heartbeat_source"))
        self.assertEqual(tools.call("read", "last_heartbeat"),
                         tools.call("read", "evaluation_time") - timedelta(minutes=1))
        self.assertNotEqual(tools.call("read", "last_heartbeat"),
                            tools.call("read", "last_monitor_success"))

    def test_trusted_callback_can_use_all_controls(self):
        for variant, expected in (("stale", "stale"), ("recent", "recent"), ("missing", "unknown")):
            with self.subTest(variant=variant):
                actual, events = case.exercise(variant, inspect_snapshot)
                self.assertEqual(actual, expected)
                self.assertGreater(len(events), 1)
                self.assertTrue(all(event[0] == "allowed" for event in events))


if __name__ == "__main__":
    unittest.main()
