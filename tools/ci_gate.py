"""Run the unit tests as a child process and accept only a clean, complete run.

Accepted only when ALL hold: exit status 0; exactly one "Ran N tests" summary
with N > 0; and the output ends with that summary, a blank line, then "OK".
That rejects skips ("OK (skipped=...)"), expected failures, unexpected
successes, zero tests, a child that exits before its summary, and a hard exit
that prints a forged tail after the real one (two summaries).

Limits: the tests and their output are trusted; this is a CI hygiene gate, not
protection against hostile test code.
"""
from __future__ import annotations

import re
import subprocess
import sys

SUMMARY = re.compile(r"^Ran [1-9][0-9]* tests? in [0-9.]+s$", re.MULTILINE)
SUMMARY_LINE = re.compile(r"^Ran [1-9][0-9]* tests? in [0-9.]+s$")
DEFAULT_TIMEOUT_S = 900


def accepted(returncode: int, stderr: str) -> tuple[bool, str]:
    if returncode != 0:
        return False, f"exit status {returncode}"
    if len(SUMMARY.findall(stderr)) != 1:
        return False, "expected exactly one positive 'Ran N tests' summary"
    lines = stderr.splitlines()
    if not (len(lines) >= 3 and SUMMARY_LINE.fullmatch(lines[-3]) and lines[-2:] == ["", "OK"]):
        return False, "output does not end with the summary, a blank line and 'OK' (skips or other outcomes?)"
    return True, "clean"


def main(argv: list[str], timeout: float = DEFAULT_TIMEOUT_S) -> int:
    start_dir = argv[1] if len(argv) > 1 else "tests"
    try:
        child = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", start_dir, "-v"],
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=timeout)
    except subprocess.TimeoutExpired:
        print("ci_gate: test run timed out", file=sys.stderr)
        return 1
    sys.stdout.write(child.stdout)
    sys.stderr.write(child.stderr)
    ok, reason = accepted(child.returncode, child.stderr)
    print(f"ci_gate: {'accepted' if ok else 'REFUSED'}: {reason}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
