import unittest
from fixtures import week_plan_case as case

class WeekPlanDiscoveryTests(unittest.TestCase):
    def test_agent_discovers_then_reads_fixture_without_internal_ids(self):
        def agent(prompt, tools):
            now = tools.call("get_as_of")
            names = tools.call("list_records", "events")
            events = [tools.call("read_record", "events", identifier) for identifier, _ in names]
            self.assertEqual(now, case.snapshot()["as_of"])
            self.assertEqual(len(events), 3)
            return "overlap" if events[0].start < events[1].end else "clear"
        output, log = case.exercise(agent)
        self.assertEqual(output, "overlap")
        self.assertEqual(log[0], ("allowed", "get_as_of", None, None))
        self.assertEqual(log[1], ("allowed", "list_records", "events", None))
        self.assertEqual(log[2], ("allowed", "read_record", "events", "ev1"))

    def test_refusals_do_not_log_untrusted_text(self):
        tools = case.FixtureTools()
        for args in (("send", "messages", "m1"), ("read_record", "network", "private"), ("attacker-secret", None, None)):
            with self.assertRaises(PermissionError):
                tools.call(*args)
        self.assertEqual([e[1] for e in tools.events], ["send", "read_record", "other"])
        self.assertNotIn("attacker-secret", repr(tools.events))
        self.assertNotIn("private", repr(tools.events))

    def test_missing_read_and_output_type_fail(self):
        tools = case.FixtureTools()
        with self.assertRaises(ValueError):
            tools.call("read_record", "events", "missing")
        self.assertEqual(tools.events[-1], ("refused", "read_record", "events", "missing_record"))
        with self.assertRaises(ValueError):
            case.exercise(lambda prompt, api: 42)
