"""Observed-field reporting for the trusted callback fixture, not a sandbox."""
import unittest

from case21_runner import run_case21


class ObservedFields(unittest.TestCase):
    def test_reads_are_distinct_from_listing_and_repeated_reads(self):
        def callback(prompt, tools):
            tools.call("list_fields")
            tools.call("read", "last_heartbeat")
            tools.call("read", "evaluation_time")
            tools.call("read", "last_heartbeat")
            return "draft"
        result = run_case21("stale", callback)
        self.assertEqual(result.read_count, 3)
        self.assertEqual(result.fields_read, ("evaluation_time", "last_heartbeat"))

    def test_no_reads_remain_empty(self):
        result = run_case21("recent", lambda prompt, tools: "draft")
        self.assertEqual(result.read_count, 0)
        self.assertEqual(result.fields_read, ())

    def test_failure_reports_only_prior_recorded_reads(self):
        def callback(prompt, tools):
            tools.call("read", "evaluation_time")
            tools.call("send")
        result = run_case21("missing", callback)
        self.assertEqual(result.status, "callback_failed")
        self.assertEqual(result.read_count, 1)
        self.assertEqual(result.fields_read, ("evaluation_time",))


if __name__ == "__main__":
    unittest.main()
