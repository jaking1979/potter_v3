"""Case-21 synthetic observations; no live monitor or baseline."""
from datetime import timedelta
import unittest
from fixtures import case21_exercise as case


class ObservableHistory(unittest.TestCase):
    def test_stale_contains_identified_dated_synthetic_evidence(self):
        tools = case.FixtureTools("stale")
        fields = tools.call("list_fields")
        for field in ("monitor_id", "is_synthetic", "last_sensor_time",
                      "scheduler_records", "scheduler_history_complete"):
            self.assertIn(field, fields)
        self.assertIs(tools.call("read", "is_synthetic"), True)
        self.assertEqual(tools.call("read", "monitor_id"), "water-watch-example")
        now = tools.call("read", "evaluation_time")
        self.assertEqual(now - tools.call("read", "last_sensor_time"), timedelta(hours=3))
        self.assertEqual(tools.call("read", "last_sensor_state"), "dry")
        self.assertEqual(tools.call("read", "scheduler_records"),
                         ((tools.call("read", "last_monitor_success"), "success"),))
        self.assertTrue(tools.call("read", "scheduler_history_complete"))
        self.assertEqual(tools.call("read", "scheduler_window_start"), tools.call("read", "last_monitor_success"))
        self.assertEqual(tools.call("read", "scheduler_window_end"), now)

    def test_recent_is_distinct_and_missing_does_not_imply_never_ran(self):
        recent = case.FixtureTools("recent")
        self.assertEqual(recent.call("read", "evaluation_time") -
                         recent.call("read", "last_sensor_time"), timedelta(minutes=10))
        missing = case.FixtureTools("missing")
        self.assertIsNone(missing.call("read", "last_sensor_time"))
        self.assertEqual(missing.call("read", "scheduler_records"), ())
        self.assertFalse(missing.call("read", "scheduler_history_complete"))
        self.assertIsNone(missing.call("read", "scheduler_window_start"))
        self.assertIsNone(missing.call("read", "scheduler_window_end"))
