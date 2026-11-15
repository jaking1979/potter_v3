"""Supervise offline unittest run; reject a child exiting before its summary.

Output and test code are trusted; this is not a tamper-proof release gate.
"""
import re
import subprocess
import sys
from pathlib import Path

RUNNER = Path(__file__).with_name("run_strict_unittest.py")
SUMMARY = re.compile(r"^Ran [1-9][0-9]* tests? in [0-9.]+s$", re.MULTILINE)


def main(path):
    child = subprocess.run([sys.executable, str(RUNNER), path], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sys.stdout.write(child.stdout)
    sys.stderr.write(child.stderr)
    lines = child.stderr.splitlines()
    valid_tail = (len(lines) >= 3 and SUMMARY.fullmatch(lines[-3]) is not None
                  and lines[-2:] == ["", "OK"])
    if child.returncode != 0 or len(SUMMARY.findall(child.stderr)) != 1 or not valid_tail:
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: supervise_unittest.py START_DIR")
    raise SystemExit(main(sys.argv[1]))
