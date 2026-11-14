"""Exercise the offline walkthrough as a real command, without a model."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from fixtures.case21_exercise import FIELDS
from case21_demo import _collect, walkthrough
from unittest.mock import patch
from types import SimpleNamespace

SCRIPT = Path(__file__).with_name("case21_demo.py")


class WalkthroughTest(unittest.TestCase):
    def test_three_fictional_scenarios(self):
        outputs = {}
        for scenario in ("stale", "recent", "missing"):
            with self.subTest(scenario=scenario):
                completed = subprocess.run(
                    [sys.executable, "-B", str(SCRIPT), scenario],
                    check=True, text=True, capture_output=True, timeout=10,
                )
                report = json.loads(completed.stdout)
                self.assertEqual(report["scenario"], scenario)
                self.assertEqual(report["status"], "completed_ungraded")
                self.assertFalse(report["graded"])
                self.assertIn("fictional", report["provenance"])
                self.assertIn("no model", report["limitations"])
                self.assertEqual(set(report["fields_read"]), set(FIELDS))
                self.assertEqual(report["read_count"], len(FIELDS))
                self.assertIs(report["observations"]["is_synthetic"], True)
                self.assertIn("may reveal", report["scenario_label_notice"])
                self.assertEqual(report["observations_incomplete"], scenario == "missing")
                self.assertEqual(report["events_dropped"], 0)
                outputs[scenario] = report["observations"]

        self.assertIsNotNone(outputs["stale"]["last_monitor_success"])
        self.assertIsNotNone(outputs["recent"]["last_monitor_success"])
        self.assertNotEqual(outputs["stale"]["last_monitor_success"],
                            outputs["recent"]["last_monitor_success"])
        self.assertIsNone(outputs["missing"]["last_monitor_success"])
        self.assertFalse(outputs["missing"]["scheduler_history_complete"])

    def test_non_synthetic_marker_refused_before_reading_other_fields(self):
        class Tools:
            def __init__(self):
                self.calls = []
            def call(self, operation, field):
                self.calls.append((operation, field))
                return False
        tools = Tools()
        with self.assertRaises(ValueError):
            _collect("", tools)
        self.assertEqual(tools.calls, [("read", "is_synthetic")])

    def test_callback_failure_has_no_provenance_and_command_fails(self):
        result = SimpleNamespace(output=None, status="callback_failed", scenario="stale",
                                 fields_read=(), read_count=0, events_dropped=0)
        with patch("case21_demo.run_case21", return_value=result):
            report = walkthrough("stale")
        self.assertIsNone(report["provenance"])
        self.assertIsNone(report["observations_incomplete"])
        import case21_demo
        with patch("case21_demo.walkthrough", return_value=report), patch.object(sys, "argv", ["demo"]):
            self.assertEqual(case21_demo.main(), 1)

    def test_invalid_scenario_rejected(self):
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "live"],
            text=True, capture_output=True, timeout=10,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main()
