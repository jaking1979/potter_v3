import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick import thermal  # noqa: E402

T0 = datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)


def at(hours):
    return (T0 + timedelta(hours=hours)).isoformat()


class Estimate(unittest.TestCase):
    def test_v2_arithmetic_is_preserved(self):
        # 52 -> 68 F at 1.4 F/h with a 20 minute lag, 38 F outdoors (v2's own example).
        e = thermal.heating_estimate(52.0, 68.0, 38.0, 1.4, samples=0, lag_minutes=20)
        self.assertEqual(e["nominal_hours"], 11.76)
        self.assertEqual(e["range_hours"], [9.5, 14.6])
        self.assertEqual((e["label"], e["rate_basis"]), ("estimated", "prior"))
        self.assertIn("starting assumption", e["statement"])

    def test_outdoor_brackets_adjust_the_rate(self):
        self.assertEqual(thermal.heating_estimate(60, 70, 20.0, 2.0)["rate_f_per_hour"], 1.7)
        self.assertEqual(thermal.heating_estimate(60, 70, 38.0, 2.0)["rate_f_per_hour"], 2.0)
        self.assertEqual(thermal.heating_estimate(60, 70, 50.0, 2.0)["rate_f_per_hour"], 2.3)
        unknown = thermal.heating_estimate(60, 70, None, 2.0)
        self.assertEqual(unknown["rate_f_per_hour"], 2.0)
        self.assertIn("outdoor temperature unknown", unknown["statement"])

    def test_a_prediction_is_always_an_estimate_even_on_a_measured_rate(self):
        e = thermal.heating_estimate(60, 70, 38.0, 2.0, samples=16)
        self.assertEqual((e["label"], e["rate_basis"]), ("estimated", "observed"))
        self.assertEqual(thermal.heating_estimate(60, 70, 38.0, 2.0, samples=3)["rate_basis"], "calibrating")

    def test_already_warm_and_no_rate(self):
        self.assertEqual(thermal.heating_estimate(70, 68, 38.0, 2.0)["nominal_hours"], 0.0)
        self.assertEqual(thermal.heating_estimate(60, 68, 38.0, None)["label"], "unknown")
        self.assertEqual(thermal.heating_estimate(60, 68, 38.0, 0)["label"], "unknown")


class Calibration(unittest.TestCase):
    def test_running_mean_and_confidence(self):
        self.assertEqual(thermal.update_rate(1.4, 0, [2.0, 2.4]), (2.2, 2, "calibrating"))   # the prior is replaced
        self.assertEqual(thermal.update_rate(2.2, 2, [1.0]), (1.8, 3, "calibrating"))
        self.assertEqual(thermal.update_rate(2.0, 9, [2.0])[1:], (10, "established"))
        self.assertEqual(thermal.update_rate(1.4, 0, []), (1.4, 0, "prior"))
        self.assertEqual(thermal.update_rate(None, 0, [-0.5]), (0.5, 1, "calibrating"))

    def test_the_collectors_own_episode_types_are_the_ones_learned_from(self):
        eps = [{"type": "active_heating", "duration_hours": 1.0, "rate_f_per_hour": 2.0},
               {"type": "active_heating", "duration_hours": 0.3, "rate_f_per_hour": 6.0},    # too short to trust
               {"type": "active_heating", "duration_hours": 1.0, "rate_f_per_hour": 0.0},
               {"type": "thermal_carryover", "duration_hours": 1.0, "rate_f_per_hour": 0.6},
               {"type": "passive_cooling", "duration_hours": 2.0, "rate_f_per_hour": -0.5},
               {"type": "passive_cooling", "duration_hours": 2.0, "rate_f_per_hour": 0.0}]
        self.assertEqual(thermal.calibration_rates(eps), ([2.0], [0.5]))


class Episodes(unittest.TestCase):
    def test_history_is_split_into_heating_carryover_and_cooling(self):
        climate = [{"last_updated": at(0), "state": "heat", "attributes": {"hvac_action": "heating"}},
                   {"last_updated": at(2), "state": "heat", "attributes": {"hvac_action": "idle"}},
                   {"last_updated": at(3), "state": "heat", "attributes": {"hvac_action": "idle"}},
                   {"last_updated": at(3.1), "state": "heat", "attributes": {"hvac_action": "idle"}}]
        temps = [{"last_updated": at(-0.1), "state": "60.0"}, {"last_updated": at(1.9), "state": "64.0"},
                 {"last_updated": at(2.9), "state": "64.6"}, {"last_updated": at(3.05), "state": "bad"},
                 {"last_updated": at(5.9), "state": "62.0"}]
        eps = thermal.classify_episodes(climate, temps, T0 + timedelta(hours=6))
        self.assertEqual([e["type"] for e in eps], ["active_heating", "thermal_carryover", "passive_cooling"])
        self.assertEqual((eps[0]["duration_hours"], eps[0]["delta_temp"], eps[0]["rate_f_per_hour"]), (2.0, 4.0, 2.0))
        self.assertEqual(eps[1]["delta_temp"], 0.6)
        self.assertEqual((eps[2]["start_time"], eps[2]["rate_f_per_hour"]), (at(3.1), -0.9))   # the 6 minute stretch is skipped

    def test_no_temperatures_or_no_reading_before_the_start_gives_no_episode(self):
        climate = [{"last_updated": at(0), "state": "heat", "attributes": {"hvac_action": "heating"}}]
        self.assertEqual(thermal.classify_episodes(climate, [], T0 + timedelta(hours=2)), [])
        late = [{"last_updated": at(1), "state": "61"}]
        self.assertEqual(thermal.classify_episodes(climate, late, T0 + timedelta(hours=2)), [])
        self.assertEqual(thermal.classify_episodes([], late, T0), [])
        self.assertEqual(thermal.classify_episodes([{"last_updated": "garbage"}], late, T0), [])


class Environment(unittest.TestCase):
    def test_dew_point(self):
        self.assertAlmostEqual(thermal.dew_point_f(68.0, 50.0), 48.7, delta=0.3)
        self.assertAlmostEqual(thermal.dew_point_f(58.5, 48.8), 39.3, delta=0.3)    # a value v2 recorded
        self.assertAlmostEqual(thermal.dew_point_f(70.0, 100.0), 70.0, delta=0.1)
        self.assertIsNone(thermal.dew_point_f(None, 50))
        self.assertIsNone(thermal.dew_point_f(68, 0))

    def test_slope_needs_45_minutes_between_samples(self):
        self.assertIsNone(thermal.slope_f_per_hour(50, 0, 49, 1800))
        self.assertEqual(thermal.slope_f_per_hour(50, 0, 48, 7200), -1.0)

    def test_hours_to_freeze(self):
        self.assertEqual(thermal.hours_to_freeze(42.0, -1.0), 10.0)
        self.assertEqual(thermal.hours_to_freeze(31.0, -1.0), 0.0)
        self.assertIsNone(thermal.hours_to_freeze(42.0, 0.0))
        self.assertIsNone(thermal.hours_to_freeze(42.0, 0.5))
        self.assertIsNone(thermal.hours_to_freeze(None, -1.0))
        self.assertIsNone(thermal.hours_to_freeze(42.0, None))


if __name__ == "__main__":
    unittest.main()
