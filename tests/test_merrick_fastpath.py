import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick import fastpath, pathlog  # noqa: E402
from merrick.ha import SafeHome  # noqa: E402
from merrick.testing import HOUSE, FakeHome, states  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "path-log.jsonl"
        self.fake = FakeHome(states())
        self.house = copy.deepcopy(HOUSE)
        self.home = SafeHome(self.fake, self.house, sleep=lambda s: None)

    def ask(self, text, **kw):
        return fastpath.handle(text, self.home, self.house, self.log, **kw)

    def lines(self):
        return pathlog.read(self.log)


class Logging(Base):
    def test_every_request_fast_or_fallback_appends_exactly_one_line(self):
        self.ask("set heat to 68")
        self.ask("why is it cold downstairs")
        self.ask("lock the front door")
        recs = self.lines()
        self.assertEqual([(r["intent"], r["path"], r["outcome"]) for r in recs],
                         [("set_temperature", "fast", "ok"), ("fallback", "fallback", "escalated"),
                          ("refuse_protected", "fast", "refused")])
        self.assertEqual(recs[1]["reason"], "needs_reasoning")
        self.assertEqual(recs[1]["request"], "why is it cold downstairs")
        self.assertTrue(all(isinstance(r["ms"], int) and r["ts"].endswith("Z") for r in recs))

    def test_duration_is_measured_and_the_id_links_the_reply_to_the_log(self):
        ticks = iter([10.0, 10.25])
        out = self.ask("status", clock=lambda: next(ticks))
        self.assertEqual(out["ms"], 250)
        self.assertEqual(self.lines()[0]["id"], out["id"])
        self.assertTrue(out["logged"])

    def test_a_fallback_touches_nothing_and_says_why(self):
        out = self.ask("set heat to 68 at 5pm")
        self.assertEqual((out["path"], out["intent"], out["outcome"], out["reason"], out["reply"]),
                         ("fallback", "fallback", "escalated", "scheduled_or_conditional", ""))
        self.assertEqual(self.fake.calls, [])

    def test_a_crashing_handler_is_logged_as_an_error_not_raised(self):
        self.house["zones"]["main"]["climate_entity"] = 42              # a broken house file entry
        out = self.ask("set heat to 68")
        self.assertEqual(out["outcome"], "refused")
        self.house["sensors"] = None
        self.home.read = lambda e: 1 / 0
        out = self.ask("status")
        self.assertEqual(out["outcome"], "error")
        self.assertIn("Nothing is assumed", out["reply"])
        self.assertEqual(self.lines()[-1]["outcome"], "error")


class Actions(Base):
    def test_set_temperature_reports_done_only_after_read_back(self):
        out = self.ask("set the family room to 70")
        self.assertEqual(out["outcome"], "ok")
        self.assertTrue(out["reply"].startswith("Done: Family room setpoint. Confirmed: climate.family_room now reads 70 F"))
        self.assertEqual(self.fake.calls, [("climate", "set_temperature", {"entity_id": "climate.family_room", "temperature": 70.0})])

    def test_no_done_when_home_assistant_does_not_show_the_change(self):
        self.fake.ignore_writes = True
        out = self.ask("set the family room to 70")
        self.assertEqual(out["outcome"], "unverified")
        self.assertNotIn("Done", out["reply"])
        self.assertIn("NOT confirmed", out["reply"])
        self.assertEqual(self.lines()[0]["outcome"], "unverified")

    def test_the_clamp_is_applied_and_explained(self):
        out = self.ask("turn the heat up to 85")
        self.assertEqual(self.fake.calls[0][2]["temperature"], 74.0)
        self.assertIn("you asked for 85 F; the allowed range is 50-74 F", out["reply"])
        self.ask("make it 40 in the den")
        self.assertEqual(self.fake.calls[1][2], {"entity_id": "climate.family_room", "temperature": 50.0})

    def test_default_zone_is_named_as_an_assumption(self):
        out = self.ask("set the thermostat to 66")
        self.assertIn("no zone was named, so the default zone was used", out["reply"])
        self.assertEqual(self.fake.calls[0][2]["entity_id"], "climate.main_house")

    def test_turn_off_and_away_hold(self):
        self.assertEqual(self.ask("turn off the family room")["outcome"], "ok")
        self.assertEqual(self.fake.states["climate.family_room"]["state"], "off")
        self.fake.states["climate.main_house"]["attributes"]["temperature"] = 70.0
        self.assertEqual(self.ask("put the house in away mode")["outcome"], "ok")
        self.assertEqual(self.fake.states["climate.main_house"]["attributes"]["temperature"], 62.0)

    def test_locks_alarms_valves_and_close_up_never_call_a_service(self):
        for text in ("lock the front door", "arm the alarm", "open the water valve", "close up the house"):
            out = self.ask(text)
            self.assertEqual((out["path"], out["outcome"]), ("fast", "refused"))
        self.assertEqual(self.fake.calls, [])

    def test_close_up_runs_the_perimeter_check_and_stops_on_an_open_or_unreadable_entry(self):
        self.assertIn("All 2 doors and windows read closed", self.ask("close up the house")["reply"])
        self.fake.states["binary_sensor.kitchen_window"]["state"] = "on"
        self.fake.states["binary_sensor.front_door"]["state"] = "unavailable"
        reply = self.ask("close up the house")["reply"]
        self.assertTrue(reply.startswith("Departure stopped"))
        self.assertIn("OPEN: kitchen window.", reply)
        self.assertIn("Unknown (no reading): front door.", reply)


class Reads(Base):
    def test_temperature_uses_the_room_sensor_and_labels_it_observed(self):
        out = self.ask("how warm is the family room?")
        self.assertIn("Observed: Family room: room 63.2 F (room sensor), set to 64.0 F", out["reply"])
        self.assertNotIn("71.5", out["reply"])                          # the thermostat's own high reading is not used
        self.fake.states["sensor.family_room_wall_temperature"]["state"] = "unavailable"
        self.assertIn("room unknown", self.ask("how warm is the family room?")["reply"])

    def test_named_sensor_and_unreachable_home_assistant(self):
        self.assertEqual(self.ask("what's the temperature in the basement")["reply"], "Observed: basement 52.0 F.")
        self.fake.fail_reads = True
        self.assertTrue(self.ask("what's the temperature in the basement")["reply"].startswith("Unknown:"))
        self.assertTrue(self.ask("how warm is the family room")["reply"].startswith("Unknown:"))
        self.assertIn("Unknown (no reading): front door, kitchen window", self.ask("are any windows open")["reply"])

    def test_perimeter_lists_open_entries_and_read_only_security_state(self):
        self.fake.states["binary_sensor.front_door"]["state"] = "on"
        reply = self.ask("are any doors open")["reply"]
        self.assertIn("OPEN: front door.", reply)
        self.assertIn("front door lock: locked.", reply)
        self.assertEqual(self.fake.calls, [])

    def test_house_status_includes_cold_watch_findings(self):
        calm = self.ask("is the house ok")["reply"]
        self.assertNotIn("Attention", calm)
        self.assertIn("Observed: basement 52.0 F.", calm)
        self.fake.states["sensor.basement_temperature"]["state"] = "38.5"
        self.assertIn("Attention: basement 38.5 F (below the 40 F line) (observed).", self.ask("is the house ok")["reply"])

    def test_heating_estimate_is_labelled_and_uses_learned_rates_when_there_are_any(self):
        out = self.ask("how long to heat the family room to 68")
        self.assertIn("Observed: Family room room 63.2 F.", out["reply"])
        self.assertIn("Estimated: Warming from 63.2 F to 68 F should take 1.9-3.0 hours (nominal 2.4 h); 38 F outdoors",
                      out["reply"])
        self.assertIn("starting assumption", out["reply"])
        model = {"zones": {"family_room": {"heating": {"mean_f_per_hour": 4.8, "samples": 12}}}}
        learned = self.ask("how long to heat the family room to 68", model=model)["reply"]
        self.assertIn("nominal 1.0 h", learned)
        self.assertIn("12 recorded heating episodes", learned)
        self.fake.states["sensor.family_room_wall_temperature"]["state"] = "unknown"
        self.assertIn("no estimate was made", self.ask("how long to heat the family room to 68")["reply"])

    def test_cold_readings_follow_the_house_file(self):
        r = fastpath.cold_readings(self.home, self.house, now_ts=5.0)
        self.assertEqual(r, {"ts": 5.0, "basement_f": 52.0, "zone_target_f": 62.0, "zone_current_f": 61.0,
                             "zone_action": "idle", "vacant": True})
        self.fake.states["input_boolean.house_closed"]["state"] = "off"
        self.assertFalse(fastpath.cold_readings(self.home, self.house)["vacant"])
        self.fake.states["input_boolean.house_closed"]["state"] = "unavailable"
        self.assertIsNone(fastpath.cold_readings(self.home, self.house)["vacant"])


class WithModel(Base):
    def test_an_injected_decision_function_widens_the_fast_path_and_is_logged_as_model(self):
        out = self.ask("everything alright up there", decide=lambda t: {"intent": "house_status", "confidence": 0.9})
        self.assertEqual((out["path"], out["intent"], out["source"]), ("fast", "house_status", "model"))
        self.assertEqual(self.lines()[0]["source"], "model")
        out = self.ask("tell me a joke", decide=lambda t: {"intent": "not_home", "confidence": 0.99})
        self.assertEqual((out["path"], out["outcome"], out["reply"]), ("fast", "not_home", ""))


if __name__ == "__main__":
    unittest.main()
