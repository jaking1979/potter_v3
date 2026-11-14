"""Exercise the offline walkthrough as a real command, without a model."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

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
                self.assertEqual(report["read_count"], len(report["fields_read"]))
                self.assertEqual(report["events_dropped"], 0)
                outputs[scenario] = report["observations"]

        self.assertIsNotNone(outputs["stale"]["last_monitor_success"])
        self.assertIsNotNone(outputs["recent"]["last_monitor_success"])
        self.assertNotEqual(outputs["stale"]["last_monitor_success"],
                            outputs["recent"]["last_monitor_success"])
        self.assertIsNone(outputs["missing"]["last_monitor_success"])
        self.assertFalse(outputs["missing"]["scheduler_history_complete"])

    def test_invalid_scenario_rejected(self):
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "live"],
            text=True, capture_output=True, timeout=10,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main()
