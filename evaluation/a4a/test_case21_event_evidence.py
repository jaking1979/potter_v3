"""Failure semantics for fictional in-process event evidence."""
import unittest
from case21_runner import run_case21

class EventEvidenceTests(unittest.TestCase):
    def test_malformed_events_never_crash_or_look_completed(self):
        for bad in (None, 5, ("allowed", "read", ["x"]),
                    ("allowed", "read", "unknown")):
            with self.subTest(bad=bad):
                def callback(prompt, tools):
                    tools._events.append(bad)
                    return "draft"
                result = run_case21("stale", callback)
                self.assertEqual(result.status, "inconclusive_malformed_events")
                self.assertIsNone(result.read_count)
                self.assertIsNone(result.fields_read)

    def test_truncated_log_does_not_present_counts_as_complete(self):
        def callback(prompt, tools):
            for _ in range(1001):
                tools.call("list_fields")
            tools.call("read", "last_monitor_success")
            return "draft"
        result = run_case21("stale", callback)
        self.assertEqual(result.status, "inconclusive_truncated")
        self.assertEqual(result.events_dropped, 2)
        self.assertIsNone(result.read_count)
        self.assertIsNone(result.fields_read)

    def test_valid_read_counts_are_consistent(self):
        def callback(prompt, tools):
            tools.call("read", "evaluation_time")
            tools.call("read", "evaluation_time")
            return "draft"
        result = run_case21("recent", callback)
        self.assertEqual(result.status, "completed_ungraded")
        self.assertEqual(result.read_count, 2)
        self.assertEqual(result.fields_read, ("evaluation_time",))
        self.assertEqual(result.events_dropped, 0)

    def test_refusal_is_not_counted_as_read(self):
        def callback(prompt, tools):
            try:
                tools.call("read", "not_real")
            except PermissionError:
                pass
            return "draft"
        result = run_case21("stale", callback)
        self.assertEqual(result.status, "inconclusive_refusal")
        self.assertEqual(result.read_count, 0)
