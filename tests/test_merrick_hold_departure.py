"""The owner's two decisions (2026-11-17): the away hold is chosen from data (62 normally, 64 when the house's
own readings say it is very cold), and locking/arming happens only after the owner confirms."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick import coldwatch, departure, fastpath, hold  # noqa: E402
from merrick.ha import SafeHome  # noqa: E402
from merrick.testing import HOUSE, FakeHome, states  # noqa: E402

HOLD = {"normal_f": 62, "cold_f": 64, "raise_when_basement_below_f": 40, "lower_when_basement_above_f": 43,
        "raise_when_hours_to_freeze_below": 24}
DEP = {"locks": ["lock.front_door"], "alarm": "alarm_control_panel.house", "arm_mode": "arm_away"}


def house(**extra):
    h = copy.deepcopy(HOUSE)
    h.pop("away_hold_f", None)
    h["away_hold"] = dict(HOLD)
    h.update(extra)
    return h


class Hold(unittest.TestCase):
    def d(self, basement=None, hours=None, target=None, h=None):
        return hold.decide(h or house(), basement, hours, target)

    def test_normal_days_hold_62_and_cold_basement_raises_to_64(self):
        self.assertEqual((self.d(52.0)["target_f"], self.d(52.0)["level"]), (62.0, "normal"))
        self.assertEqual(self.d(40.0)["target_f"], 62.0)                 # at the line is not below it
        cold = self.d(39.9)
        self.assertEqual((cold["target_f"], cold["level"], cold["label"]), (64.0, "cold", "observed"))
        self.assertIn("39.9", cold["reason"])

    def test_a_freeze_trajectory_raises_the_hold_even_with_a_warmish_basement(self):
        d = self.d(45.0, hours=20.0)
        self.assertEqual((d["target_f"], d["label"]), (64.0, "estimated"))
        self.assertEqual(self.d(45.0, hours=24.0)["target_f"], 62.0)

    def test_it_stays_raised_until_the_basement_has_properly_recovered(self):
        self.assertEqual(self.d(41.5, target=64.0)["target_f"], 64.0)    # was cold, not yet above 43
        self.assertEqual(self.d(43.0, target=64.0)["target_f"], 62.0)
        self.assertEqual(self.d(41.5, target=62.0)["target_f"], 62.0)    # was normal: no reason to raise

    def test_missing_data_never_changes_the_level(self):
        self.assertEqual((self.d(None, target=64.0)["target_f"], self.d(None, target=64.0)["label"]), (64.0, "unknown"))
        self.assertEqual(self.d(None, target=62.0)["target_f"], 62.0)
        self.assertEqual(self.d(None)["target_f"], 62.0)

    def test_a_single_number_still_works_and_no_config_means_no_decision(self):
        self.assertEqual(hold.decide({"away_hold_f": 61}, 20.0)["target_f"], 61.0)
        self.assertIsNone(hold.decide({}, 20.0))
        self.assertEqual(hold.decide({"away_hold": {"normal_f": 62, "cold_f": 60}}, 20.0)["target_f"], 62.0)

    def test_cold_watch_expects_the_data_driven_hold_not_a_fixed_62(self):
        th = coldwatch.Thresholds.from_house(house())
        base = {"ts": 1.0, "zone_current_f": 63.0, "vacant": True}
        raised = {**base, "basement_f": 39.0, "zone_target_f": 64.0, "expected_hold_f": 64.0, "expected_hold_reason": "x"}
        self.assertEqual([f["kind"] for f in coldwatch.evaluate(raised, th)], ["basement_cold"])   # 64 is not drift
        not_raised = {**raised, "zone_target_f": 62.0}
        drift = [f for f in coldwatch.evaluate(not_raised, th) if f["kind"] == "hold_drift"]
        self.assertIn("below the 64 F away setting", drift[0]["text"])

    def test_the_away_hold_request_sets_the_decided_value_and_says_why(self):
        fake = FakeHome(states())
        fake.states["sensor.basement_temperature"]["state"] = "38.5"
        h = house()
        with tempfile.TemporaryDirectory() as t:
            out = fastpath.handle("set the house to away hold", SafeHome(fake, h, sleep=lambda s: None), h,
                                  Path(t) / "log.jsonl")
        self.assertEqual(out["outcome"], "ok", out["reply"])
        self.assertEqual(fake.calls[-1][2]["temperature"], 64.0)
        self.assertIn("38.5", out["reply"])


class Departure(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHome(states())
        self.fake.states["lock.front_door"] = {"state": "unlocked", "attributes": {}}
        self.fake.states["alarm_control_panel.house"] = {"state": "disarmed", "attributes": {}}
        self.h = house(departure=dict(DEP))
        self.home = SafeHome(self.fake, self.h, sleep=lambda s: None)

    def test_asking_to_close_up_checks_and_asks_but_does_nothing(self):
        status, text, pending = departure.prepare(self.home, self.h, 1000.0)
        self.assertEqual(status, "needs_confirmation")
        self.assertIn("confirm departure", text)
        self.assertEqual(pending["expires"], 1000.0 + departure.PENDING_TTL_S)
        self.assertEqual(self.fake.calls, [])

    def test_no_doors_or_windows_listed_stops_instead_of_claiming_all_closed(self):
        h = dict(self.h, perimeter={})
        home = SafeHome(self.fake, h, sleep=lambda s: None)
        status, text, pending = departure.prepare(home, h, 1000.0)
        self.assertEqual((status, pending), ("stopped", None))
        self.assertIn("none could be checked", text)
        _, _, pending = departure.prepare(self.home, self.h, 1000.0)
        self.assertEqual(departure.confirm(home, h, pending, 1100.0)[0], "stopped")
        self.assertEqual(self.fake.calls, [])

    def test_confirming_locks_and_arms_with_read_back(self):
        _, _, pending = departure.prepare(self.home, self.h, 1000.0)
        status, text = departure.confirm(self.home, self.h, pending, 1200.0)
        self.assertEqual(status, "done", text)
        self.assertEqual([(c[0], c[1]) for c in self.fake.calls],
                         [("lock", "lock"), ("alarm_control_panel", "alarm_arm_away")])
        self.assertEqual(self.fake.states["lock.front_door"]["state"], "locked")
        self.assertEqual(self.fake.states["alarm_control_panel.house"]["state"], "armed_away")

    def test_no_pending_expired_or_reopened_perimeter_all_refuse_without_acting(self):
        self.assertEqual(departure.confirm(self.home, self.h, None, 1.0)[0], "refused")
        self.assertEqual(departure.confirm(self.home, self.h, {"junk": 1}, 1.0)[0], "refused")
        _, _, pending = departure.prepare(self.home, self.h, 1000.0)
        self.assertEqual(departure.confirm(self.home, self.h, pending, 1000.0 + departure.PENDING_TTL_S + 1)[0], "refused")
        self.fake.states["binary_sensor.kitchen_window"]["state"] = "on"
        self.assertEqual(departure.confirm(self.home, self.h, pending, 1100.0)[0], "stopped")
        self.assertEqual(self.fake.calls, [])

    def test_an_open_or_unreadable_perimeter_stops_before_asking(self):
        self.fake.states["binary_sensor.front_door"]["state"] = "unavailable"
        status, text, pending = departure.prepare(self.home, self.h, 1.0)
        self.assertEqual((status, pending), ("stopped", None))
        self.assertIn("could not be read: front door", text)

    def test_a_lock_that_does_not_lock_is_reported_not_confirmed(self):
        _, _, pending = departure.prepare(self.home, self.h, 1000.0)
        self.fake.ignore_writes = True
        status, text = departure.confirm(self.home, self.h, pending, 1001.0)
        self.assertEqual(status, "partial")
        self.assertIn("NOT confirmed", text)

    def test_lock_and_arm_need_confirmation_and_only_work_on_listed_devices(self):
        self.assertEqual(self.home.lock("lock.front_door").outcome, "refused")                 # no confirmation
        self.assertEqual(self.home.arm("alarm_control_panel.house", "arm_away").outcome, "refused")
        self.fake.states["lock.back_door"] = {"state": "unlocked", "attributes": {}}
        self.assertEqual(self.home.lock("lock.back_door", confirmed=True).outcome, "refused")    # not listed
        self.assertEqual(self.home.lock("switch.fireplace_blower", confirmed=True).outcome, "refused")
        self.assertEqual(self.home.arm("alarm_control_panel.house", "disarm", confirmed=True).outcome, "refused")
        self.assertEqual(self.home.lock("lock.front_door", confirmed="yes").outcome, "refused")  # must be True itself
        self.assertEqual(self.fake.calls, [])

    def test_there_is_no_way_to_unlock_or_disarm(self):
        self.assertFalse(hasattr(self.home, "unlock"))
        self.assertFalse(hasattr(self.home, "disarm"))
        src = (ROOT / "overlay/merrick/ha.py").read_text() + (ROOT / "overlay/merrick/departure.py").read_text()
        self.assertNotIn('"unlock"', src)
        self.assertNotIn("alarm_disarm", src)

    def test_without_departure_devices_nothing_is_offered(self):
        h = house()
        status, text, pending = departure.prepare(SafeHome(self.fake, h, sleep=lambda s: None), h, 1.0)
        self.assertEqual((status, pending), ("not_configured", None))

    def test_the_command_line_flow_stores_the_pending_step_and_uses_it_once(self):
        with tempfile.TemporaryDirectory() as t:
            out = fastpath.handle("close up the house", self.home, self.h, Path(t) / "log.jsonl")
        self.assertEqual(out["outcome"], "needs_confirmation")
        self.assertIn("pending_departure", out)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
