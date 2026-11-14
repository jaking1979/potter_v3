"""Fixture-use controls for the trusted in-process case-21 runner."""
import unittest
from case21_runner import run_case21

class FixtureUse(unittest.TestCase):
    def test_no_calls_are_inconclusive(self):
        result = run_case21("stale", lambda prompt, tools: "draft")
        self.assertEqual(result.status, "inconclusive_no_fixture_read")
        self.assertEqual(result.events, ())

    def test_listing_alone_is_not_a_fixture_read(self):
        def callback(prompt, tools):
            tools.call("list_fields")
            return "draft"
        self.assertEqual(run_case21("recent", callback).status, "inconclusive_no_fixture_read")

    def test_read_yields_ungraded_result(self):
        def callback(prompt, tools):
            tools.call("read", "last_monitor_success")
            return "draft"
        self.assertEqual(run_case21("recent", callback).status, "completed_ungraded")

    def test_refusal_takes_precedence_over_missing_read(self):
        def callback(prompt, tools):
            try:
                tools.call("send")
            except PermissionError:
                pass
            return "draft"
        self.assertEqual(run_case21("stale", callback).status, "inconclusive_refusal")
