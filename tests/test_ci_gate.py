"""Tests for tools/ci_gate.py (synthetic child runs)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import ci_gate  # noqa: E402

OK_TAIL = "\n----------------------------------------------------------------------\nRan 3 tests in 0.010s\n\nOK\n"


class Accepted(unittest.TestCase):
    def test_clean_run_accepted(self):
        self.assertEqual(ci_gate.accepted(0, OK_TAIL), (True, "clean"))

    def test_refusals(self):
        cases = {
            "nonzero exit": (1, OK_TAIL),
            "skips": (0, OK_TAIL.replace("OK\n", "OK (skipped=2)\n")),
            "expected failures": (0, OK_TAIL.replace("OK\n", "OK (expected failures=1)\n")),
            "failed": (0, OK_TAIL.replace("OK\n", "FAILED (failures=1)\n")),
            "zero tests": (0, OK_TAIL.replace("Ran 3 tests", "Ran 0 tests")),
            "no summary": (0, "OK\n"),
            "two summaries": (0, OK_TAIL + OK_TAIL),
            "trailing output": (0, OK_TAIL + "late note\n"),
            "appended OK after failure": (0, OK_TAIL.replace("OK\n", "FAILED (failures=1)\n") + "OK\n"),
            "appended blank+OK after failure": (0, OK_TAIL.replace("OK\n", "FAILED (failures=1)\n") + "\nOK\n"),
            "empty": (0, ""),
        }
        for name, (code, text) in cases.items():
            with self.subTest(case=name):
                self.assertFalse(ci_gate.accepted(code, text)[0])


class EndToEnd(unittest.TestCase):
    def run_dir(self, source):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test_x.py").write_text(source)
            return subprocess.run([sys.executable, str(ROOT / "tools/ci_gate.py"), d], capture_output=True, text=True)

    def test_pass_skip_fail_and_hard_exit(self):
        body = "import unittest\nclass T(unittest.TestCase):\n"
        cases = {
            "pass": (body + " def test_a(self): pass\n", 0),
            "fail": (body + " def test_a(self): self.fail()\n", 1),
            "skip": (body + " @unittest.skip('x')\n def test_a(self): pass\n", 1),
            "expected failure": (body + " @unittest.expectedFailure\n def test_a(self): self.fail()\n", 1),
            "hard exit": ("import os, unittest\nclass T(unittest.TestCase):\n def test_a(self): os._exit(0)\n", 1),
            "empty dir": ("", 1),
        }
        for name, (source, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.run_dir(source).returncode, expected)

    def test_timeout_is_a_refusal(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test_x.py").write_text("import time, unittest\nclass T(unittest.TestCase):\n def test_a(self): time.sleep(5)\n")
            self.assertEqual(ci_gate.main(["ci_gate", d], timeout=0.5), 1)


if __name__ == "__main__":
    unittest.main()
