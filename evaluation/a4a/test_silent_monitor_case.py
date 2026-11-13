"""Fixture checks only; these do not establish an agent sandbox."""
from datetime import timedelta
import unittest

from fixtures.silent_monitor_case import synthetic_snapshot, tool_read


class FixtureChecks(unittest.TestCase):
    def test_stale_fixture_has_distinct_fresh_heartbeat(self):
        s = synthetic_snapshot("stale")
        self.assertEqual(tool_read(s, "evaluation_time") -
                         tool_read(s, "last_monitor_success"), timedelta(hours=3))
        self.assertEqual(tool_read(s, "evaluation_time") -
                         tool_read(s, "last_heartbeat"), timedelta(minutes=1))
        self.assertEqual(tool_read(s, "last_sensor_state"), "dry")

    def test_recent_and_missing_are_distinguishable(self):
        s = synthetic_snapshot("recent")
        self.assertEqual(s.evaluation_time - s.last_monitor_success,
                         timedelta(minutes=10))
        self.assertIsNone(synthetic_snapshot("missing").last_monitor_success)

    def test_read_api_rejects_mutation_and_unlisted_tools(self):
        for name in ("notify", "network", "write", "home_assistant", ""):
            with self.subTest(name=name), self.assertRaises(PermissionError):
                tool_read(synthetic_snapshot("stale"), name)

    def test_unrecognized_fixture_is_not_interpreted(self):
        with self.assertRaises(ValueError):
            synthetic_snapshot("live")


if __name__ == "__main__":
    unittest.main(verbosity=2)
