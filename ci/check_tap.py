"""Fail closed on absent tests, skipped/todo tests, cancellation, or malformed TAP summary.

Offline CI helper; a green result is not a signed review or release evidence.
"""
import re
import sys

KEYS = ("tests", "pass", "fail", "cancelled", "skipped", "todo")
PATTERN = re.compile(r"^# (tests|pass|fail|cancelled|skipped|todo) ([0-9]+)$")


def check(output):
    lines = output.splitlines()
    found = {}
    for line in lines:
        match = PATTERN.fullmatch(line)
        if match:
            key, value = match.groups()
            if key in found:
                return False
            found[key] = int(value)
    return (set(found) == set(KEYS) and found['tests'] > 0
            and found['pass'] == found['tests']
            and all(found[key] == 0 for key in ('fail', 'cancelled', 'skipped', 'todo')))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: check_tap.py TAP_FILE')
    with open(sys.argv[1], encoding='utf-8') as stream:
        good = check(stream.read())
    if not good:
        raise SystemExit('Node TAP coverage absent, incomplete, skipped or malformed')
