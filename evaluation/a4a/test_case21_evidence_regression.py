"""Synthetic case-21 refusal and completeness regressions."""
import contextlib
import io
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import case21_demo as demo
from fixtures import case21_exercise as fixture

class DemoEvidenceTests(unittest.TestCase):
    def test_real_fixture_marker_refusal_and_cli_exit(self):
        original = fixture.tool_read
        def read(snapshot, field):
            return False if field == "is_synthetic" else original(snapshot, field)
        with patch.object(fixture, "tool_read", side_effect=read):
            report = demo.walkthrough("stale")
            self.assertEqual(report["status"], "refused_non_synthetic")
            self.assertIsNone(report["provenance"])
            self.assertIsNone(report["observations_incomplete"])
            self.assertEqual(report["fields_read"], ("is_synthetic",))
            with patch.object(sys, "argv", ["demo"]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(demo.main(), 1)

    def test_incomplete_inputs_individually(self):
        good = demo.walkthrough("stale")["observations"]
        self.assertIsNotNone(good)
        required = ("evaluation_time", "monitor_cadence", "last_monitor_success",
                    "last_sensor_state", "last_sensor_time", "scheduler_window_start",
                    "scheduler_window_end", "scheduler_records")
        for field in required + ("scheduler_history_complete",):
            with self.subTest(field=field):
                value = dict(good)
                value[field] = False if field == "scheduler_history_complete" else None
                result = SimpleNamespace(output=json.dumps(value), status="completed_ungraded",
                    scenario="stale", fields_read=(), read_count=0, events_dropped=0)
                with patch.object(demo, "run_case21", return_value=result):
                    self.assertTrue(demo.walkthrough("stale")["observations_incomplete"])
        result = SimpleNamespace(output=None, status="callback_failed", scenario="stale",
                                 fields_read=(), read_count=0, events_dropped=0)
        with patch.object(demo, "run_case21", return_value=result):
            self.assertIsNone(demo.walkthrough("stale")["observations_incomplete"])

if __name__ == "__main__":
    unittest.main()
