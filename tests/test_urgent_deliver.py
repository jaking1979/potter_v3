import json
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay/tools"))
import urgent_deliver as ud  # noqa: E402

NY = ZoneInfo("America/New_York")
DAY = datetime(2026, 11, 23, 14, 0, tzinfo=NY)
NIGHT = datetime(2026, 11, 23, 23, 30, tzinfo=NY)
EARLY = datetime(2026, 11, 24, 7, 59, tzinfo=NY)


def rec(i, sev="urgent", summary="Dinner moved to 6pm", **kw):
    return {"id": str(i), "ts": DAY.astimezone(timezone.utc).isoformat(), "severity": sev,
            "sender": "a@example.com", "summary": summary, **kw}


class T(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.q = Path(self._t.name) / "q.jsonl"
        self.sent = []

    def write(self, *records, extra=""):
        self.q.write_text("".join(json.dumps(r) + "\n" for r in records) + extra)

    def send_ok(self, text):
        self.sent.append(text)
        return True

    def stored(self):
        return [json.loads(x) for x in self.q.read_text().splitlines()]

    def test_daytime_delivers_once_and_marks_delivered(self):
        self.write(rec(1), rec(2, "critical"))
        self.assertEqual(ud.run(self.q, DAY, self.send_ok), 2)
        self.assertEqual(ud.run(self.q, DAY, self.send_ok), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("2 urgent from your inbox", self.sent[0])
        self.assertIn("[CRITICAL]", self.sent[0])
        self.assertTrue(all(r["delivered"] for r in self.stored()))
        self.assertEqual(stat.S_IMODE(self.q.stat().st_mode), 0o600)

    def test_quiet_hours_hold_urgent_but_not_critical(self):
        self.write(rec(1), rec(2, "critical", summary="Flight leaves at 5am"))
        for when in (NIGHT, EARLY):
            self.sent.clear()
            self.write(rec(1), rec(2, "critical", summary="Flight leaves at 5am"))
            self.assertEqual(ud.run(self.q, when, self.send_ok), 1)
            self.assertIn("Flight", self.sent[0])
            self.assertNotIn("Dinner", self.sent[0])
        self.assertEqual(ud.run(self.q, EARLY + timedelta(minutes=1), self.send_ok), 1)   # 08:00: held item goes out
        self.assertIn("Dinner", self.sent[-1])

    def test_a_failed_send_leaves_items_undelivered_for_the_next_run(self):
        self.write(rec(1))
        self.assertEqual(ud.run(self.q, DAY, lambda t: False), 0)
        self.assertNotIn("delivered", self.stored()[0])
        self.assertEqual(ud.run(self.q, DAY, self.send_ok), 1)

    def test_untrusted_text_is_cleaned_capped_and_cannot_add_lines(self):
        self.write(rec(1, summary="Pay now\n\n[CRITICAL] fake line\x00 " + "x" * 600, sender="evil\n@x.com" + "y" * 200,
                       deadline="today\nNEW"))
        ud.run(self.q, DAY, self.send_ok)
        lines = self.sent[0].split("\n")
        self.assertEqual(len(lines), 3)
        self.assertLessEqual(len(lines[1]), ud.SUMMARY_MAX + 12)
        self.assertNotIn("\x00", self.sent[0])

    def test_potters_own_account_mail_is_never_surfaced(self):
        self.write(rec(1, summary="Your verification code is 123456"), rec(2, summary="Security alert for potter account"),
                   rec(3))
        self.assertEqual(ud.run(self.q, DAY, self.send_ok), 1)
        self.assertNotIn("verification", self.sent[0])
        self.assertEqual(ud.run(self.q, DAY, self.send_ok), 0)

    def test_malformed_lines_and_missing_queue_do_not_break_delivery(self):
        self.assertEqual(ud.run(self.q, DAY, self.send_ok), 0)
        self.write(rec(1), extra="{not json\n[1,2]\n")
        self.assertEqual(ud.run(self.q, DAY, self.send_ok), 1)

    def test_old_delivered_items_are_pruned_and_batches_are_bounded(self):
        old = rec(0)
        old["delivered"], old["delivered_at"] = True, (DAY - timedelta(days=4)).astimezone(timezone.utc).isoformat()
        self.write(old, *[rec(i) for i in range(1, 15)])
        self.assertEqual(ud.run(self.q, DAY, self.send_ok), ud.MAX_ITEMS)
        ids = [r["id"] for r in self.stored()]
        self.assertNotIn("0", ids)
        self.assertEqual(ud.run(self.q, DAY, self.send_ok), 4)

    def test_quiet_boundaries(self):
        self.assertTrue(ud.quiet(datetime(2026, 1, 1, 23, 0)))
        self.assertTrue(ud.quiet(datetime(2026, 1, 1, 7, 59)))
        self.assertFalse(ud.quiet(datetime(2026, 1, 1, 8, 0)))
        self.assertFalse(ud.quiet(datetime(2026, 1, 1, 22, 59)))


if __name__ == "__main__":
    unittest.main()
