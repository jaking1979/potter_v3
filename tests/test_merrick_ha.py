import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick import ha  # noqa: E402
from merrick.habridge import BridgeError, BridgeHomeAssistant  # noqa: E402
from merrick.testing import HOUSE, FakeHome, states  # noqa: E402


def make(house=HOUSE, **kw):
    fake = FakeHome(states())
    naps = []
    return fake, ha.SafeHome(fake, house, sleep=naps.append, **kw), naps


class Clamp(unittest.TestCase):
    def test_setpoints_are_clamped_to_50_74_and_the_clamp_is_reported(self):
        self.assertEqual(ha.clamp_setpoint(68), (68.0, False))
        self.assertEqual(ha.clamp_setpoint(50), (50.0, False))
        self.assertEqual(ha.clamp_setpoint(74), (74.0, False))
        self.assertEqual(ha.clamp_setpoint(49.9), (50.0, True))
        self.assertEqual(ha.clamp_setpoint(90), (74.0, True))
        self.assertEqual(ha.clamp_setpoint("72.5"), (72.5, False))
        for bad in ("warm", None, float("nan"), float("inf")):
            with self.assertRaises((TypeError, ValueError)):
                ha.clamp_setpoint(bad)

    def test_a_too_high_request_is_sent_as_74_and_says_so(self):
        fake, home, _ = make()
        res = home.set_temperature("climate.main_house", 85)
        self.assertEqual(fake.calls, [("climate", "set_temperature", {"entity_id": "climate.main_house", "temperature": 74.0})])
        self.assertTrue(res.ok and res.clamped)
        self.assertEqual((res.requested, res.applied, res.observed), (85, 74.0, 74.0))

    def test_a_too_low_request_is_sent_as_50(self):
        fake, home, _ = make()
        res = home.set_temperature("climate.main_house", 40)
        self.assertEqual(fake.calls[0][2]["temperature"], 50.0)
        self.assertTrue(res.ok and res.clamped)

    def test_non_numbers_and_a_non_fahrenheit_house_are_refused_without_a_call(self):
        fake, home, _ = make()
        self.assertEqual(home.set_temperature("climate.main_house", "toasty").outcome, "refused")
        celsius = {**copy.deepcopy(HOUSE), "temperature_unit": "C"}
        fake2, home2, _ = make(celsius)
        self.assertEqual(home2.set_temperature("climate.main_house", 20).outcome, "refused")
        self.assertEqual(fake.calls + fake2.calls, [])


class FailClosed(unittest.TestCase):
    def test_locks_alarms_and_water_valves_are_refused_and_never_called(self):
        fake, home, _ = make()
        house = {**copy.deepcopy(HOUSE), "controllable": ["lock.front_door", "alarm_control_panel.house",
                                                          "switch.main_water_valve", "valve.garden", "switch.main_supply"]}
        home.house = house                       # even if someone lists them as controllable, they stay refused
        for eid in ("lock.front_door", "alarm_control_panel.house", "switch.main_water_valve", "valve.garden",
                    "switch.main_supply", "cover.garage_door", "automation.heat_down", "script.close_up"):
            for attempt in (home.switch(eid, True), home.switch(eid, False), home.set_temperature(eid, 68),
                            home.set_hvac_mode(eid, "off")):
                with self.subTest(entity=eid):
                    self.assertFalse(attempt.ok)
                    self.assertEqual(attempt.outcome, "refused")
        self.assertEqual(fake.calls, [])

    def test_only_listed_devices_in_allowed_domains_can_be_written(self):
        fake, home, _ = make()
        self.assertEqual(home.switch("switch.porch_light", True).outcome, "refused")       # not on the allowlist
        self.assertEqual(home.set_temperature("climate.attic", 68).outcome, "refused")     # not a known zone
        self.assertEqual(home.switch("media_player.tv", True).outcome, "refused")          # domain not allowed
        self.assertEqual(home.switch("not an entity", True).outcome, "refused")
        self.assertEqual(home.switch(None, True).outcome, "refused")
        self.assertEqual(home.set_hvac_mode("climate.main_house", "turbo").outcome, "refused")
        self.assertEqual(home.switch("climate.main_house", False).outcome, "refused")
        self.assertEqual(fake.calls, [])
        self.assertTrue(home.switch("switch.fireplace_blower", True).ok)

    def test_refusal_reasons(self):
        self.assertIn("protected", ha.refusal("lock.front_door", HOUSE))
        self.assertIn("protected", ha.refusal("SWITCH.Main_Water_Valve", HOUSE))
        self.assertIsNone(ha.refusal("climate.main_house", HOUSE))
        self.assertIsNone(ha.refusal("climate.heat_pump", {"zones": {"a": {"climate_entity": "climate.heat_pump"}}}))


class ReadBack(unittest.TestCase):
    def test_success_requires_the_state_read_back_to_match(self):
        fake, home, _ = make()
        res = home.set_temperature("climate.main_house", 68)
        self.assertEqual((res.ok, res.outcome, res.observed), (True, "verified", 68.0))
        self.assertIn("climate.main_house", fake.reads[fake.reads.index("climate.main_house") + 1:])  # read after write

    def test_a_call_home_assistant_accepts_but_does_not_apply_is_not_success(self):
        fake, home, naps = make()
        fake.ignore_writes = True
        res = home.set_temperature("climate.main_house", 68)
        self.assertEqual((res.ok, res.outcome, res.observed), (False, "unverified", 62.0))
        self.assertIn("NOT confirmed", res.message)
        self.assertEqual(len(naps), 2)                                   # three reads, two waits between them
        res = home.set_hvac_mode("climate.main_house", "off")
        self.assertEqual((res.ok, res.outcome, res.observed), (False, "unverified", "heat"))
        self.assertEqual(home.switch("switch.fireplace_blower", True).outcome, "unverified")

    def test_a_slow_home_assistant_is_given_a_few_reads_then_confirmed(self):
        fake, home, naps = make()
        fake.lag_reads = 1
        self.assertTrue(home.set_temperature("climate.main_house", 68).ok)
        fake2, home2, _ = make(attempts=1)
        fake2.lag_reads = 2
        self.assertFalse(home2.set_temperature("climate.main_house", 68).ok)

    def test_a_missing_device_a_failed_call_and_an_unreachable_server_are_errors_not_successes(self):
        house = copy.deepcopy(HOUSE)
        house["zones"]["ghost"] = {"climate_entity": "climate.ghost"}
        fake, home, _ = make(house)
        res = home.set_temperature("climate.ghost", 68)
        self.assertEqual((res.ok, res.outcome), (False, "error"))
        self.assertEqual(fake.calls, [])                                 # nothing is sent to a device that cannot be read
        fake.fail_writes = True
        self.assertEqual(home.set_temperature("climate.main_house", 68).outcome, "error")
        fake.fail_writes, fake.fail_reads = False, True
        self.assertEqual(home.set_temperature("climate.main_house", 68).outcome, "error")
        self.assertIsNone(home.read("climate.main_house"))
        self.assertIsNone(home.value("sensor.basement_temperature"))

    def test_mode_and_switch_read_back(self):
        fake, home, _ = make()
        self.assertTrue(home.set_hvac_mode("climate.main_house", "off").ok)
        self.assertEqual(fake.states["climate.main_house"]["state"], "off")
        self.assertTrue(home.switch("switch.fireplace_blower", True).ok)
        self.assertTrue(home.switch("switch.fireplace_blower", False).ok)


class Reads(unittest.TestCase):
    def test_room_truth_comes_from_the_room_sensor_and_never_falls_back_to_the_thermostat(self):
        fake, home, _ = make()
        z = home.zone_reading("family_room")
        self.assertEqual((z["room_f"], z["room_source"], z["setpoint_f"], z["action"]), (63.2, "room sensor", 64.0, "heating"))
        fake.states["sensor.family_room_wall_temperature"]["state"] = "unavailable"
        self.assertIsNone(home.zone_reading("family_room")["room_f"])    # the gap is reported, not papered over
        self.assertEqual(home.zone_reading("main")["room_source"], "thermostat's own sensor")
        self.assertEqual(home.zone_reading("main")["room_f"], 61.0)

    def test_unavailable_and_non_numeric_values_are_none(self):
        fake, home, _ = make()
        for bad in ("unknown", "unavailable", "", "nan", "warm"):
            fake.states["sensor.basement_temperature"]["state"] = bad
            self.assertIsNone(home.value("sensor.basement_temperature"))
        self.assertIsNone(home.value("sensor.does_not_exist"))


class Bridge(unittest.TestCase):
    def bridge(self, reply, code=0):
        seen = []

        def run(argv, input, capture_output, text, timeout):
            seen.append((argv, json.loads(input), timeout))
            if isinstance(reply, Exception):
                raise reply
            return mock.Mock(returncode=code, stdout=reply)
        return BridgeHomeAssistant("ha-bridge --profile home", timeout=7, run=run), seen

    def test_each_operation_sends_the_agreed_request(self):
        c, seen = self.bridge('{"ok": true, "state": {"state": "heat"}, "rows": [{"state": "61"}]}')
        self.assertEqual(c.get_state("climate.main_house"), {"state": "heat"})
        c.call_service("climate", "set_temperature", {"entity_id": "climate.main_house", "temperature": 68.0})
        self.assertEqual(c.history("sensor.x", "2026-01-01T00:00:00+00:00"), [{"state": "61"}])
        self.assertEqual([s[1] for s in seen], [
            {"op": "get_state", "entity_id": "climate.main_house"},
            {"op": "call_service", "domain": "climate", "service": "set_temperature",
             "data": {"entity_id": "climate.main_house", "temperature": 68.0}},
            {"op": "history", "entity_id": "sensor.x", "start": "2026-01-01T00:00:00+00:00"}])
        self.assertTrue(all(s[0] == ["ha-bridge", "--profile", "home"] and s[2] == 7 for s in seen))

    def test_a_missing_entity_is_none_not_an_error(self):
        c, _ = self.bridge('{"ok": true, "state": null}')
        self.assertIsNone(c.get_state("climate.ghost"))

    def test_every_kind_of_bridge_failure_raises_and_never_repeats_the_bridges_own_text(self):
        for reply, code in (('{"ok": false, "error": "401 token abc123 rejected"}', 0), ("not json", 0),
                            ('{"ok": true}', 2), ("[]", 0), (subprocess.TimeoutExpired("x", 7), 0),
                            (FileNotFoundError("ha-bridge"), 0)):
            c, _ = self.bridge(reply, code)
            with self.assertRaises(BridgeError) as e:
                c.call_service("climate", "set_temperature", {})
            self.assertNotIn("abc123", str(e.exception))

    def test_a_failing_bridge_under_the_safety_layer_is_an_error_result_not_a_success(self):
        c, _ = self.bridge('{"ok": false}')
        home = ha.SafeHome(c, HOUSE, sleep=lambda s: None)
        self.assertIsNone(home.read("climate.main_house"))
        self.assertEqual(home.set_temperature("climate.main_house", 68).outcome, "error")

    def test_no_command_is_refused(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                BridgeHomeAssistant(bad)


if __name__ == "__main__":
    unittest.main()
