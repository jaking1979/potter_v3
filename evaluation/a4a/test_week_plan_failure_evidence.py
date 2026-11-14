"""Fixture failure evidence; in-process only, not an isolation boundary."""
import unittest
import traceback
from fixtures import week_plan_case as case


class FailureEvidence(unittest.TestCase):
    def test_uncaught_refusal_is_available_to_caller(self):
        def adapter(prompt, tools):
            tools.call("get_as_of")
            tools.call("send")
        with self.assertRaises(case.ExerciseFailure) as raised:
            case.exercise(adapter)
        self.assertEqual(raised.exception.events, (
            ("allowed", "get_as_of", None, None),
            ("refused", "send", None, "invalid_call"),
        ))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_unexpected_failure_retains_calls_without_copying_message(self):
        def adapter(prompt, tools):
            tools.call("get_as_of")
            raise RuntimeError("private adapter detail")
        with self.assertRaises(case.ExerciseFailure) as raised:
            case.exercise(adapter)
        self.assertEqual(len(raised.exception.events), 1)
        self.assertNotIn("private adapter detail", str(raised.exception))
        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertNotIn("private adapter detail", rendered)

    def test_success_still_returns_existing_tuple(self):
        def adapter(prompt, tools):
            tools.call("get_as_of")
            return "draft"
        output, events = case.exercise(adapter)
        self.assertEqual(output, "draft")
        self.assertEqual(events, (("allowed", "get_as_of", None, None),))
