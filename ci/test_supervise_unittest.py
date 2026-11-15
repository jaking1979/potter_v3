import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("supervise_unittest.py")

class SupervisorTests(unittest.TestCase):
    def run_source(self, source):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test_example.py").write_text(source)
            return subprocess.run([sys.executable, str(SCRIPT), d], capture_output=True, timeout=15)

    def test_outcomes(self):
        cases = {
            "import unittest\nclass T(unittest.TestCase):\n def test_x(self): pass\n": 0,
            "import unittest\nclass T(unittest.TestCase):\n def test_x(self): self.fail()\n": 1,
            "import unittest\nclass T(unittest.TestCase):\n @unittest.skip('x')\n def test_x(self): pass\n": 1,
            "import unittest\nclass T(unittest.TestCase):\n @unittest.expectedFailure\n def test_x(self): self.fail()\n": 1,
            "import os, unittest\nclass T(unittest.TestCase):\n def test_x(self): os._exit(0)\n": 1,
            "import atexit, os, unittest\natexit.register(os._exit, 0)\nclass T(unittest.TestCase):\n def test_x(self): self.fail()\n": 1,
            "import unittest, sys\nclass T(unittest.TestCase):\n def test_x(self): print('\\nRan 1 test in 0.001s', file=sys.stderr)\n": 1,
            "": 1,
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(self.run_source(source).returncode, expected)

    def test_failed_run_followed_by_forged_ok(self):
        source = """import atexit, os, sys, unittest
def end():
 sys.stderr.write("\\nOK\\n")
 sys.stderr.flush()
 os._exit(0)
atexit.register(end)
class T(unittest.TestCase):
 def test_x(self): self.fail()
"""
        self.assertEqual(self.run_source(source).returncode, 1)

if __name__ == "__main__":
    unittest.main()
