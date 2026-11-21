import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick import coldwatch as cw  # noqa: E402

NOW = 1_800_000_000.0


def reading(**kw):
    base = {"ts": NOW, "basement_f": 52.0, "zone_target_f": 62.0, "zone_current_f": 61.5, "zone_action": "idle",
            "vacant": True}
    base.update(kw)
    return base


def kinds(readings, **kw):
    return [(f["kind"], f["severity"]) for f in cw.evaluate(readings, **kw)]


class Evaluate(unittest.TestCase):
    def test_a_healthy_house_produces_no_findings_and_no_text(self):
        self.assertEqual(cw.evaluate(reading()), [])
        self.assertEqual(cw.render([], reading()), "")

    def test_basement_thresholds_at_the_boundaries(self):
        self.assertEqual(kinds(reading(basement_f=40.0)), [])
        self.assertEqual(kinds(reading(basement_f=39.9)), [("basement_cold", "alert")])
        self.assertEqual(kinds(reading(basement_f=36.0)), [("basement_cold", "alert")])
        self.assertEqual(kinds(reading(basement_f=35.9)), [("basement_cold", "urgent")])

    def test_hold_drift_is_checked_when_vacant_or_unknown_and_skipped_when_occupied(self):
        self.assertEqual(kinds(reading(zone_target_f=61.5)), [])                       # within tolerance
        self.assertEqual(kinds(reading(zone_target_f=58.0)), [("hold_drift", "alert")])
        self.assertEqual(kinds(reading(zone_target_f=58.0, vacant=None)), [("hold_drift", "alert")])
        self.assertEqual(kinds(reading(zone_target_f=58.0, vacant=False)), [])         # someone is home
        self.assertEqual(kinds(reading(zone_target_f=58.0), th=cw.Thresholds(vacancy_hold_f=None)), [])

    def test_a_hold_set_warmer_than_needed_is_a_note_never_a_message(self):
        f = cw.evaluate(reading(basement_f=57.4, zone_target_f=64.0, zone_current_f=64.0))
        self.assertEqual([(x["kind"], x["severity"]) for x in f], [("hold_above", "note")])
        self.assertEqual(cw.reportable(f), [])
        self.assertEqual(cw.render(f, reading(zone_target_f=64.0)), "")
        self.assertEqual(cw.new_findings(f, {}, NOW), ([], {}))

    def test_a_main_zone_that_is_not_keeping_up_is_found(self):
        f = cw.evaluate(reading(zone_current_f=57.9, zone_action="heating"))
        self.assertEqual([(x["kind"], x["label"]) for x in f], [("zone_underheat", "observed")])
        self.assertIn("(heat: heating)", f[0]["text"])
        self.assertEqual(kinds(reading(zone_current_f=58.0)), [])

    def test_an_unreadable_basement_is_a_finding_but_not_a_chat_message(self):
        for bad in (None, "unavailable", float("nan")):
            f = cw.evaluate(reading(basement_f=bad))
            self.assertEqual([(x["kind"], x["severity"]) for x in f], [("basement_unreadable", "unreadable")])
            self.assertEqual(cw.reportable(f), [])
            self.assertEqual(cw.render(f, reading(basement_f=bad)), "")
        self.assertEqual(kinds({}), [("basement_unreadable", "unreadable")])
        self.assertEqual(kinds(None), [("basement_unreadable", "unreadable")])

    def test_missing_zone_readings_do_not_invent_findings(self):
        self.assertEqual(kinds(reading(zone_target_f=None, zone_current_f=None)), [])

    def test_several_problems_are_all_reported(self):
        f = kinds(reading(basement_f=35.0, zone_target_f=55.0, zone_current_f=54.0))   # 55 is below the hold
        self.assertEqual(f, [("basement_cold", "urgent"), ("hold_drift", "alert"), ("zone_underheat", "alert")])

    def test_freeze_trajectory_uses_the_previous_sample_and_is_labelled_estimated(self):
        prev = {"ts": NOW - 7200, "basement_f": 50.0}
        f = cw.evaluate(reading(basement_f=46.0), previous=prev)                       # 2 F/h down -> 7 h to 32 F
        self.assertEqual([(x["kind"], x["label"]) for x in f], [("freeze_trajectory", "estimated")])
        self.assertIn("about 7 hours", f[0]["text"])
        self.assertEqual(kinds(reading(basement_f=49.5), previous=prev), [])           # 0.25 F/h -> 70 h: fine
        self.assertEqual(kinds(reading(basement_f=53.0), previous=prev), [])           # warming
        self.assertEqual(kinds(reading(basement_f=46.0), previous={"ts": NOW - 600, "basement_f": 50.0}), [])  # too close
        self.assertEqual(kinds(reading(basement_f=46.0), previous={"ts": NOW - 86400, "basement_f": 50.0}), [])  # too old
        self.assertEqual(kinds(reading(basement_f=46.0), previous={"ts": NOW - 7200, "basement_f": None}), [])

    def test_thresholds_come_from_the_house_file(self):
        th = cw.Thresholds.from_house({"away_hold_f": 60, "cold_watch": {"basement_alert_f": 45, "zone": "main"}})
        self.assertEqual((th.basement_alert_f, th.basement_urgent_f, th.vacancy_hold_f), (45, 36.0, 60))
        self.assertEqual(cw.Thresholds.from_house({}), cw.Thresholds())


class Dedupe(unittest.TestCase):
    def test_reported_once_again_when_worse_and_again_after_the_repeat_interval(self):
        alert = cw.evaluate(reading(basement_f=39.0))
        rep, seen = cw.new_findings(alert, {}, NOW)
        self.assertEqual(len(rep), 1)
        rep, seen = cw.new_findings(alert, seen, NOW + 7200)                           # still cold two hours later
        self.assertEqual(rep, [])
        rep, seen = cw.new_findings(cw.evaluate(reading(basement_f=35.0)), seen, NOW + 14400)   # now urgent
        self.assertEqual([f["severity"] for f in rep], ["urgent"])
        rep, seen = cw.new_findings(cw.evaluate(reading(basement_f=35.0)), seen, NOW + 14400 + 12 * 3600)
        self.assertEqual(len(rep), 1)                                                  # reminder after 12 hours
        rep, seen = cw.new_findings([], seen, NOW + 90000)                             # recovered
        self.assertEqual((rep, seen), ([], {}))
        rep, seen = cw.new_findings(alert, seen, NOW + 90100)                          # cold again
        self.assertEqual(len(rep), 1)

    def test_an_unreadable_sensor_is_never_in_the_message_queue(self):
        rep, seen = cw.new_findings(cw.evaluate(reading(basement_f=None)), {}, NOW)
        self.assertEqual((rep, seen), ([], {}))


class Render(unittest.TestCase):
    def test_message_is_plain_and_labelled(self):
        r = reading(basement_f=35.2, zone_target_f=60.0)
        text = cw.render(cw.evaluate(r), r, "Lake house")
        self.assertEqual(text.splitlines(), ["URGENT house alert, Lake house",
                                             "- URGENT: basement 35.2 F, below 36 F: pipes are at risk",
                                             "- heat is set to 60 F, below the 62 F away setting",
                                             "Now: basement 35 F; heat set to 60 F, main zone 62 F."])
        self.assertNotIn("(observed)", text)
        self.assertTrue(text.isascii())
        r = reading(basement_f=39.0)
        self.assertEqual(cw.render(cw.evaluate(r), r).splitlines()[0], "House alert")


if __name__ == "__main__":
    unittest.main()
