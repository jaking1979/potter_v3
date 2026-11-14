import unittest
from datetime import timezone
from fixtures import week_plan_case as case


class WeekPlanFixtureTests(unittest.TestCase):
    def test_snapshot_is_fictional_consistent_and_timezone_aware(self):
        data = case.snapshot()
        self.assertEqual(data["as_of"].tzinfo, timezone.utc)
        for event in data["events"]:
            self.assertLess(event.start, event.end)
            self.assertEqual(event.start.tzinfo, timezone.utc)
            self.assertEqual(event.end.tzinfo, timezone.utc)
        for section in ("events", "tasks", "messages"):
            ids = [x.id for x in data[section]]
            self.assertEqual(len(ids), len(set(ids)))

    def test_calendar_contains_one_overlap_and_one_nonoverlap(self):
        a, b, c = case.snapshot()["events"]
        self.assertLess(max(a.start, b.start), min(a.end, b.end))
        self.assertGreaterEqual(c.start, max(a.end, b.end))

    def test_lookup_returns_only_existing_fixture_record(self):
        self.assertEqual(case.read_record("tasks", "t1").label, "Prepare workshop notes")
        for args in (("messages", "missing"), ("network", "t1"), ("events", "")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                case.read_record(*args)

    def test_fresh_snapshots_and_records_cannot_change_source(self):
        first = case.snapshot()
        first["events"] = ()
        self.assertEqual(len(case.snapshot()["events"]), 3)
        with self.assertRaises(AttributeError):
            case.snapshot()["events"][0].label = "changed"


    def test_recording_allowed_and_refused_calls(self):
        tools = case.FixtureTools()
        self.assertEqual(tools.call("read_record", "events", "ev1").id, "ev1")
        with self.assertRaises(PermissionError):
            tools.call("send", "messages", "m1")
        with self.assertRaises(ValueError):
            tools.call("read_record", "events", "missing")
        self.assertEqual(tools.events, (("allowed", "events"),
                         ("refused", "unknown_tool_or_section"),
                         ("refused", "missing_record")))


    def test_offline_adapter_uses_fixture_api_and_refuses_send(self):
        def stub(prompt, tools):
            first = tools.call("read_record", "events", "ev1")
            second = tools.call("read_record", "events", "ev2")
            with self.assertRaises(PermissionError):
                tools.call("send", "messages", "m1")
            return "overlap" if first.start < second.end and second.start < first.end else "clear"
        response, events = case.exercise(stub)
        self.assertEqual(response, "overlap")
        self.assertEqual(events, (("allowed", "events"), ("allowed", "events"),
                                  ("refused", "unknown_tool_or_section")))


if __name__ == "__main__":
    unittest.main()
