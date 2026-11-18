"""House dry-run: read-only observations of what v3 would do, and the comparison report."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick import shadow  # noqa: E402
from merrick.ha import SafeHome  # noqa: E402
from merrick.testing import HOUSE, FakeHome, states  # noqa: E402


class Shadow(unittest.TestCase):
    def setUp(self):
        self.house = copy.deepcopy(HOUSE)
        self.house.pop("away_hold_f", None)
        self.house["away_hold"] = {"normal_f": 62, "cold_f": 64, "raise_when_basement_below_f": 40, "lower_when_basement_above_f": 43}
        self.fake = FakeHome(states())
        self.home = SafeHome(self.fake, self.house, sleep=lambda s: None)

    def set(self, closed, main, family, basement):
        self.fake.states[self.house["vacancy_entity"]]["state"] = self.house["vacancy_on_state"] if closed else "off"
        self.fake.states["climate.main_house"]["attributes"]["temperature"] = main
        self.fake.states["climate.family_room"]["attributes"]["temperature"] = family
        self.fake.states["sensor.basement_temperature"]["state"] = str(basement)

    def test_observation_never_writes_and_judges_each_zone(self):
        self.set(True, 62, 66, 45)
        rec = shadow.observe(self.home, self.house, 1000.0)
        self.assertEqual(self.fake.calls, [])                                     # read-only
        v = {z["zone"]: (z["verdict"], z["v3_would_hold_f"]) for z in rec["zones"]}
        self.assertEqual(v, {"main": ("agrees", 62.0), "family_room": ("differs", 62.0)})
        self.set(True, 62, 62, 38)                                                # cold basement: v3 would hold 64
        rec = shadow.observe(self.home, self.house, 2000.0)
        self.assertEqual({z["verdict"] for z in rec["zones"]}, {"differs"})
        self.set(False, 68, 70, 45)
        self.assertEqual({z["verdict"] for z in shadow.observe(self.home, self.house, 3000.0)["zones"]}, {"occupied"})

    def test_report_agreement_and_merged_divergence_stretches(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "shadow.jsonl"
            for i, (fam, ts) in enumerate([(66, 0), (66, 1800), (62, 3600), (62, 5400)]):
                self.set(True, 62, fam, 45)
                shadow.append(p, shadow.observe(self.home, self.house, 1_700_000_000 + ts))
            rep = shadow.report(shadow.read(p))
            self.assertEqual(rep["counts"]["agrees"], 6)
            self.assertEqual(rep["counts"]["differs"], 2)
            self.assertEqual(rep["agreement_pct"], 75)
            self.assertEqual(len(rep["divergences"]), 1)                          # two readings, one stretch
            self.assertEqual(rep["divergences"][0]["n"], 2)
            text = shadow.render(rep, 7)
            self.assertIn("agreed with the house 75%", text)
            self.assertIn("family_room: house at 66 F, v3 would hold 62 F", text)

    def test_a_switched_off_zone_is_off_not_unreadable_and_the_report_names_zones(self):
        self.set(True, 62, 62, 45)
        self.fake.states["climate.family_room"]["state"] = "off"
        self.fake.states["climate.family_room"]["attributes"].pop("temperature", None)
        rec = shadow.observe(self.home, self.house, 1000.0)
        self.assertEqual({z["zone"]: z["verdict"] for z in rec["zones"]}, {"main": "agrees", "family_room": "off"})
        del self.fake.states["climate.main_house"]
        rec2 = shadow.observe(self.home, self.house, 2000.0)
        rep = shadow.report([rec, rec2])
        self.assertEqual(rep["off_zones"], {"family_room": 2})
        self.assertEqual(rep["unreadable_zones"], {"main": 1})
        text = shadow.render(rep, 1)
        self.assertIn("Switched off: family_room (2 readings).", text)
        self.assertIn("Could not read: main (1 readings).", text)

    def test_nothing_recorded_says_so(self):
        self.assertIn("no observations", shadow.render(shadow.report([]), 7))


if __name__ == "__main__":
    unittest.main()
