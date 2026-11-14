"""Offline strict unittest runner: skips and expected failures are not passes.

This trusts the in-process unittest runner and tests. It is not a release gate.
"""
import sys
import unittest


def acceptable(result):
    return (result.testsRun > 0 and result.wasSuccessful()
            and not result.skipped and not result.expectedFailures)


def main(start_dir):
    suite = unittest.defaultTestLoader.discover(start_dir, pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not acceptable(result):
        print('required unittest coverage failed, skipped, expected-failed or absent', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: run_strict_unittest.py START_DIR')
    raise SystemExit(main(sys.argv[1]))
