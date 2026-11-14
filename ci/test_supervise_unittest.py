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
            "import unittest
class T(unittest.TestCase):
 def test_x(self): pass
": 0,
            "import unittest
class T(unittest.TestCase):
 def test_x(self): self.fail()
": 1,
            "import unittest
class T(unittest.TestCase):
 @unittest.skip('x')
 def test_x(self): pass
": 1,
            "import unittest
class T(unittest.TestCase):
 @unittest.expectedFailure
 def test_x(self): self.fail()
": 1,
            "import os, unittest
class T(unittest.TestCase):
 def test_x(self): os._exit(0)
": 1,
            "import atexit, os, unittest
atexit.register(os._exit, 0)
class T(unittest.TestCase):
 def test_x(self): self.fail()
": 1,
            "import unittest, sys
class T(unittest.TestCase):
 def test_x(self): print(\'Ran 1 test in 0.001s\', file=sys.stderr)
": 1,
            "": 1,
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(self.run_source(source).returncode, expected)

if __name__ == "__main__":
    unittest.main()
