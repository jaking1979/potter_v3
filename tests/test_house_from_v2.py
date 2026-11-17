import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
sys.path.insert(0, str(ROOT / "overlay"))
import house_from_v2 as hv  # noqa: E402

V2 = {"locations": {
    "country": {"name": "Country House", "default": True, "weather_entity": "weather.home",
                "zones": {"ducts": {"name": "Main", "climate_entity": "climate.ducts", "temp_sensor": "sensor.ducts_t",
                                    "temp_source_type": "kumo_internal_return", "aliases": ["house", "heat"]},
                          "den": {"name": "Den", "climate_entity": "climate.den", "temp_sensor": "sensor.den_wall",
                                  "temp_source_type": "independent_shelly_ht", "fireplace_switch": "switch.den_plug",
                                  "aliases": ["den"]},
                          "bad zone": {"climate_entity": "climate.x"}, "noent": {"climate_entity": "not an entity"}},
                "doors_windows": {"front_door": "binary_sensor.front", "all_group": "binary_sensor.group", "junk": "nope"},
                "leak_sensors": {"basement_water": "binary_sensor.leak"},
                "valves_and_pumps": {"main_valve": "valve.main"},
                "security_and_locks": {"front_lock": "lock.front", "panel": "alarm_control_panel.a", "panel2": "alarm_control_panel.b"}},
    "city": {"name": "City Flat", "default": False, "zones": {}, "aliases": ["the flat"]}}}
SCRIPT = 's = state("sensor.basement_h_t_temperature")\nclosed = state("input_boolean.house_closed")'


class Convert(unittest.TestCase):
    def setUp(self):
        self.h = hv.convert(V2, SCRIPT)

    def test_zones_and_the_room_sensor_rule(self):
        self.assertEqual(sorted(self.h["zones"]), ["den", "ducts"])
        self.assertEqual(self.h["zones"]["den"]["room_sensor"], "sensor.den_wall")
        self.assertNotIn("room_sensor", self.h["zones"]["ducts"])      # a thermostat's own thermistor is not a room sensor
        self.assertEqual(self.h["default_zone"], "ducts")

    def test_perimeter_protected_departure_and_controllable(self):
        self.assertEqual(self.h["perimeter"], {"front door": "binary_sensor.front"})
        self.assertEqual(self.h["protected_entities"], ["binary_sensor.leak", "valve.main"])
        self.assertEqual(self.h["departure"], {"locks": ["lock.front"], "alarm": "alarm_control_panel.a", "arm_mode": "arm_away"})
        self.assertEqual(self.h["controllable"], ["switch.den_plug"])

    def test_basement_vacancy_hold_and_other_locations(self):
        self.assertEqual(self.h["sensors"]["basement"]["entity"], "sensor.basement_h_t_temperature")
        self.assertEqual(self.h["vacancy_entity"], "input_boolean.house_closed")
        self.assertEqual((self.h["away_hold"]["normal_f"], self.h["away_hold"]["cold_f"]), (62, 64))
        self.assertIn("city", self.h["unsupported_location_words"])
        self.assertEqual(self.h["_missing"], [])
        self.assertEqual(hv.convert(V2, "")["_missing"], ["basement sensor", "vacancy switch"])

    def test_the_result_works_with_merricks_router_and_safety_layer(self):
        from merrick import router
        from merrick.ha import refusal
        self.assertEqual(router.route("set heat to 68", self.h, None).intent, "set_temperature")
        self.assertEqual(router.route("close up the house", self.h, None).intent, "close_up")
        self.assertIsNone(refusal("climate.den", self.h))
        self.assertIsNotNone(refusal("valve.main", self.h))
        self.assertIsNotNone(refusal("lock.front", self.h))

    def test_unusable_data_is_refused(self):
        for bad in ({}, {"locations": {}}, {"locations": {"a": {"default": True, "zones": {}}}}):
            with self.assertRaises(ValueError):
                hv.convert(bad)


if __name__ == "__main__":
    unittest.main()
