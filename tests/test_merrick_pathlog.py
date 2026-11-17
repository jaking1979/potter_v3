import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick import pathlog  # noqa: E402

T = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)


class Record(unittest.TestCase):
    def test_a_record_has_the_agreed_fields(self):
        r = pathlog.record("set_temperature", "fast", 411.6, "ok", request="set heat to 68", source="table", now=T)
        self.assertEqual({k: r[k] for k in ("ts", "intent", "path", "ms", "outcome", "source")},
                         {"ts": "2026-11-17T12:00:00Z", "intent": "set_temperature", "path": "fast", "ms": 412,
                          "outcome": "ok", "source": "table"})
        self.assertTrue(r["id"])

    def test_a_fallback_is_always_logged_as_intent_fallback(self):
        r = pathlog.record("anything", "fallback", 3, "escalated", reason="needs_reasoning")
        self.assertEqual((r["intent"], r["path"], r["reason"]), ("fallback", "fallback", "needs_reasoning"))
        with self.assertRaises(ValueError):
            pathlog.record("x", "slow", 1, "ok")

    def test_long_requests_are_cut_and_flattened_to_one_line(self):
        r = pathlog.record("x", "fast", 1, "ok", request="a\nb " + "z" * 500)
        self.assertEqual(len(r["request"]), pathlog.MAX_REQUEST_CHARS)
        self.assertNotIn("\n", r["request"])


class AppendRead(unittest.TestCase):
    def test_each_request_appends_exactly_one_json_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "path-log.jsonl"
            for i in range(3):
                self.assertTrue(pathlog.append(p, pathlog.record("house_status", "fast", i, "ok")))
            lines = p.read_text().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(all(json.loads(x)["path"] == "fast" for x in lines))
            self.assertEqual(len(pathlog.read(p)), 3)

    def test_a_torn_line_is_skipped_and_a_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.jsonl"
            p.write_text('{"path": "fast", "intent": "x", "ms": 1}\n{"path": "fa\n[1]\n')
            self.assertEqual(len(pathlog.read(p)), 1)
            self.assertEqual(pathlog.read(Path(d) / "none.jsonl"), [])

    def test_an_unwritable_log_returns_false_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "file"
            blocker.write_text("x")
            self.assertFalse(pathlog.append(blocker / "log.jsonl", {"a": 1}))


class Report(unittest.TestCase):
    def recs(self):
        out = [pathlog.record("set_temperature", "fast", ms, "ok", request="set heat to 68", source="table", now=T)
               for ms in (300, 400, 500)]
        out.append(pathlog.record("turn_off", "fast", 900, "unverified", request="turn off heat", now=T))
        for i, (text, reason) in enumerate([("why is it cold downstairs", "needs_reasoning"),
                                            ("set heat to 68 at 5pm", "scheduled_or_conditional"),
                                            ("Set heat to 70 at 6pm", "scheduled_or_conditional"),
                                            ("set heat to 66 at 9pm", "scheduled_or_conditional")]):
            out.append(pathlog.record("", "fallback", 5, "escalated", request=text, reason=reason,
                                      request_id=f"fb{i}", now=T))
        out.append(pathlog.fallback_end("fb0", 95_000, "budget_hit", now=T))
        out.append(pathlog.fallback_end("fb1", 20_000, "done", now=T))
        return out

    def test_counts_rates_timings_and_reasons(self):
        rep = pathlog.report(self.recs())
        self.assertEqual((rep["total"], rep["fast"], rep["fallback"], rep["fallback_rate"]), (8, 4, 4, 0.5))
        self.assertEqual(rep["by_intent"], {"set_temperature": 3, "turn_off": 1})
        self.assertEqual(rep["fast_outcomes"], {"ok": 3, "unverified": 1})
        self.assertEqual(rep["fallback_reasons"], {"scheduled_or_conditional": 3, "needs_reasoning": 1})
        self.assertEqual(rep["fallback_outcomes"], {"budget_hit": 1, "done": 1})
        self.assertEqual(rep["fallback_unfinished"], 2)
        self.assertEqual(rep["fast_ms"]["median"], 500)
        self.assertEqual(rep["fallback_ms"]["p95"], 95_000)

    def test_miss_examples_group_the_same_request_with_different_numbers(self):
        ex = pathlog.report(self.recs())["miss_examples"]
        self.assertEqual((ex[0]["count"], ex[0]["reason"], ex[0]["request"]),
                         (3, "scheduled_or_conditional", "set heat to 68 at 5pm"))
        self.assertEqual(ex[1]["count"], 1)
        self.assertEqual(len(pathlog.report(self.recs(), examples=1)["miss_examples"]), 1)

    def test_since_filters_and_an_empty_log_reports_cleanly(self):
        self.assertEqual(pathlog.report(self.recs(), since="2026-11-18T00:00:00Z")["total"], 0)
        empty = pathlog.report([])
        self.assertEqual((empty["total"], empty["fallback_rate"], empty["miss_examples"]), (0, None, []))
        self.assertIn("no requests", pathlog.render(empty))

    def test_render_names_the_miss_rate_and_the_examples(self):
        text = pathlog.render(pathlog.report(self.recs() + ["junk", None]))
        self.assertIn("8 (4 fast, 4 fallback, 50% missed", text)
        self.assertIn("- 3x [scheduled_or_conditional] set heat to 68 at 5pm", text)
        self.assertIn("2 never reported an end", text)


if __name__ == "__main__":
    unittest.main()
