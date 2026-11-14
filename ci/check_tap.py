"""Fail closed on absent tests, skipped/todo tests, cancellation, or malformed TAP summary.

Offline CI helper; a green result is not a signed review or release evidence.
"""
import re
import sys

KEYS = ("tests", "pass", "fail", "cancelled", "skipped", "todo")
PATTERN = re.compile(r"^# (tests|pass|fail|cancelled|skipped|todo) ([0-9]+)$")


def check(output, minimum=1):
    if type(minimum) is not int or minimum < 1:
        return False
    lines = output.splitlines()
    found = {}
    for line in lines:
        match = PATTERN.fullmatch(line)
        if not match and re.match(r"^# (?:tests|pass|fail|cancelled|skipped|todo)(?:\s|$)", line):
            return False
        if match:
            key, value = match.groups()
            if key in found:
                return False
            found[key] = int(value)
    return (set(found) == set(KEYS) and found['tests'] >= minimum
            and found['pass'] == found['tests']
            and all(found[key] == 0 for key in ('fail', 'cancelled', 'skipped', 'todo')))


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: check_tap.py TAP_FILE MINIMUM')
    with open(sys.argv[1], encoding='utf-8') as stream:
        try:
            minimum = int(sys.argv[2])
        except ValueError:
            raise SystemExit('minimum must be a positive integer')
        good = check(stream.read(), minimum)
    if not good:
        raise SystemExit('Node TAP coverage absent, incomplete, skipped or malformed')
