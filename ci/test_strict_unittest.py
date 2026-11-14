import io
import unittest

from run_strict_unittest import acceptable


class StrictResult(unittest.TestCase):
    def run_case(self, method):
        case = type('Fixture', (unittest.TestCase,), {'test_value': method})
        result = unittest.TextTestRunner(stream=io.StringIO()).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(case))
        return acceptable(result)

    def test_pass(self):
        self.assertTrue(self.run_case(lambda self: None))

    def test_expected_failure_is_rejected_despite_unittest_success(self):
        @unittest.expectedFailure
        def failing(self):
            self.fail('unimplemented')
        self.assertFalse(self.run_case(failing))

    def test_skip_and_failure_are_rejected(self):
        @unittest.skip('not exercised')
        def skipped(self):
            pass
        self.assertFalse(self.run_case(skipped))
        self.assertFalse(self.run_case(lambda self: self.fail('fail')))

    def test_empty_result_is_rejected(self):
        self.assertFalse(acceptable(unittest.TestResult()))
