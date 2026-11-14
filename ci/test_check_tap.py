import unittest
from check_tap import check

GOOD = '# tests 3\n# suites 0\n# pass 3\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n'

class TapCoverage(unittest.TestCase):
    def test_good(self):
        self.assertTrue(check(GOOD))

    def test_bad_summaries_fail_closed(self):
        for bad in (GOOD.replace('# tests 3', '# tests 0'),
                    GOOD.replace('# pass 3', '# pass 2'),
                    GOOD.replace('# skipped 0', '# skipped 1'),
                    GOOD.replace('# todo 0', '# todo 1'),
                    GOOD.replace('# fail 0', '# fail 1'),
                    GOOD.replace('# cancelled 0', '# cancelled 1'),
                    GOOD.replace('# skipped 0\n', ''),
                    GOOD + '# skipped 0\n',
                    GOOD + '# skipped malformed\n',
                    GOOD.replace('# tests 3', '# tests nope')):
            with self.subTest(bad=bad):
                self.assertFalse(check(bad))

if __name__ == '__main__':
    unittest.main()
