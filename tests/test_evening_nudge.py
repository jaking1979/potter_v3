import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay/tools"))
import evening_nudge as en  # noqa: E402

NY = ZoneInfo("America/New_York")
CAL = "work@example.com"


def at(h, m=0, d=23):
    return datetime(2026, 11, d, h, m, tzinfo=NY)       # Nov 23 2026 is a Monday


def doc(*ends, ok=True, errors=None):
    return {"successful": ok, "data": {"calendars": {CAL: {"busy": [{"start": "x", "end": e} for e in ends],
                                                              **({"errors": errors} if errors else {})}}}}


class Decide(unittest.TestCase):
    def test_no_calendar_means_six_pm(self):
        for busy in (None, False):
            self.assertEqual(en.decide(at(17, 59), busy, {}), "wait")
            self.assertEqual(en.decide(at(18, 0), busy, {}), "send")

    def test_last_session_plus_twenty_minutes_and_a_late_meeting_moves_it(self):
        end = at(16, 30)
        self.assertEqual(en.decide(at(16, 49), end, {}), "wait")
        self.assertEqual(en.decide(at(16, 50), end, {}), "send")
        self.assertEqual(en.decide(at(18, 30), at(19, 0), {}), "wait")      # still in session past 18:00
        self.assertEqual(en.decide(at(19, 20), at(19, 0), {}), "send")

    def test_once_per_day_and_again_the_next_day(self):
        sent = {"date": "2026-11-23", "sent": True}
        self.assertEqual(en.decide(at(20, 0), None, sent), "done")
        self.assertEqual(en.decide(at(18, 0, d=24), None, sent), "send")

    def test_weekends_and_late_evenings_are_skipped(self):
        self.assertEqual(en.decide(at(18, 0, d=28), None, {}), "skip")       # Saturday
        self.assertEqual(en.decide(at(21, 30), None, {}), "skip")
        self.assertEqual(en.decide(at(21, 29), None, {}), "send")


class Calendar(unittest.TestCase):
    def test_picks_the_last_block_ending_today_in_local_time(self):
        d = doc("2026-11-23T15:00:00-05:00", "2026-11-23T21:30:00+00:00", "2026-11-24T10:00:00-05:00")
        self.assertEqual(en.last_busy_end(d, CAL, NY, at(9).date()), at(16, 30))

    def test_no_blocks_is_none_and_every_broken_reply_is_false(self):
        self.assertIsNone(en.last_busy_end(doc(), CAL, NY, at(9).date()))
        self.assertIsNone(en.last_busy_end(doc("garbage"), CAL, NY, at(9).date()))
        for bad in (doc(ok=False), doc(errors=["x"]), {}, {"successful": True}, None, "text",
                    {"successful": True, "data": {"calendars": {}}}):
            self.assertIs(en.last_busy_end(bad, CAL, NY, at(9).date()), False, bad)

    def test_a_failed_calendar_read_falls_back_instead_of_dying(self):
        self.assertIs(en.read_busy(CAL, "America/New_York", at(12), "/nonexistent/composio"), False)
        self.assertIs(en.read_busy("", "America/New_York", at(12), "x"), False)
        self.assertIs(en.read_busy(CAL, "America/New_York", at(12), None), False)
        self.assertEqual(en.target_time(at(12), False), at(18, 0))


if __name__ == "__main__":
    unittest.main()
