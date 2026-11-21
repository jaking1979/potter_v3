"""Routines (owner's spec, 2026-11-21): bedtime watches the bedroom, the wake ramp is judged by the living area, the
fireplace fan and the front door are triggers, the owner's word closes an unreadable window, and nothing is written
while the routines run in shadow mode."""
import copy
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick import departure, fastpath, routines, router  # noqa: E402
from merrick.ha import SafeHome  # noqa: E402
from merrick.testing import HOUSE, FakeHome, climate, sensor, states  # noqa: E402

NOW = datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)          # 10 pm the night before, New York time

ROUTINES = {
    "mode": "live", "timezone": "America/New_York", "heating_below_f": 55,
    "bedtime": {
        "watch_sensor": "sensor.bedroom_temperature", "watch_label": "bedroom",
        "steps": [{"zone": "main", "mode": "heat", "setpoint": 64, "heating": True},
                  {"zone": "family_room", "mode": "heat", "setpoint": 64, "heating": True},
                  {"zone": "kids_room", "off": True, "unless_home": "person.kid"},
                  {"fan": "fan.master", "percentage": 33}],
        "lights_when_free": {"lights": ["light.family_room_all"], "give_up_hours": 4,
                             "busy": [{"entity": "media_player.family_room_tv"},
                                      {"entity": "binary_sensor.family_room_presence"}]},
    },
    "wake": {"default": "07:30", "zone": "main", "judge": "sensor.living_room_temperature", "target_f": 70,
             "steps": [{"zone": "family_room", "mode": "heat", "setpoint": 66}]},
    "fireplace": {"power_entity": "sensor.fireplace_power", "on_above_w": 15, "zone": "family_room", "setpoint": 64},
    "close_up": {"steps": [{"switch": "switch.fr_outlet", "on": False}, {"switch": "switch.deck_outlet", "on": False},
                           {"switch": "input_boolean.house_closed", "on": True}]},
    "coming_up": {"steps": [{"switch": "switch.deck_outlet", "on": True}], "zone": "main",
                  "judge": "sensor.living_room_temperature", "night_f": 68, "day_f": 70},
    "arrival": {"signals": [{"entity": "lock.front_door", "state": "unlocked", "label": "the front door was unlocked"},
                            {"entity": "person.owner", "state": "home"}],
                "steps": [{"switch": "input_boolean.house_closed", "on": False}, {"switch": "switch.fr_outlet", "on": True}]},
    "comfort": {"zone": "family_room", "humidity_entity": "sensor.fr_humidity", "humidifier": "switch.fr_humidifier",
                "low_pct": 35, "high_pct": 45, "fan": "switch.fr_fan", "fan_on_min": 15, "fan_every_min": 60},
}


def house(mode="live"):
    h = copy.deepcopy(HOUSE)
    h["zones"]["kids_room"] = {"name": "Kid's room", "climate_entity": "climate.kids_room"}
    h["controllable"] = h["controllable"] + ["fan.master", "light.family_room_all", "switch.fr_outlet",
                                             "switch.deck_outlet", "input_boolean.house_closed",
                                             "switch.fr_humidifier", "switch.fr_fan"]
    h["routines"] = {**copy.deepcopy(ROUTINES), "mode": mode}
    return h


def fake(**extra):
    s = states()
    s.update({
        "climate.main_house": climate("heat", 68.0, 68.0, "heating"),
        "climate.family_room": climate("off", None, 70.0, "off"),
        "climate.kids_room": climate("heat", 66.0, 66.0, "idle"),
        "person.kid": {"state": "not_home", "attributes": {}},
        "person.owner": {"state": "home", "attributes": {}},
        "fan.master": {"state": "off", "attributes": {"percentage": 0, "assumed_state": True}},
        "light.family_room_all": {"state": "on", "attributes": {}},
        "media_player.family_room_tv": {"state": "playing", "attributes": {}},
        "binary_sensor.family_room_presence": {"state": "on", "attributes": {}},
        "sensor.bedroom_temperature": sensor(60.0),
        "sensor.living_room_temperature": sensor(64.0),
        "sensor.fireplace_power": sensor(0.4),
        "switch.fr_outlet": {"state": "on", "attributes": {}},
        "switch.deck_outlet": {"state": "on", "attributes": {}},
        "switch.fr_humidifier": {"state": "off", "attributes": {}},
        "switch.fr_fan": {"state": "off", "attributes": {}},
        "sensor.fr_humidity": sensor(30),
        "sun.sun": {"state": "below_horizon", "attributes": {"next_rising": "2026-11-21T11:50:00+00:00",
                                                              "next_setting": "2026-11-21T21:30:00+00:00"}},
    })
    s.update(extra)
    return FakeHome(s)


def home(f, h):
    return SafeHome(f, h, sleep=lambda s: None)


class Config(unittest.TestCase):
    def test_check_finds_unknown_zones_and_uncontrollable_devices(self):
        self.assertEqual(routines.check(house()), [])
        h = house()
        h["routines"]["bedtime"]["steps"].append({"switch": "switch.not_listed", "on": False})
        h["routines"]["wake"]["zone"] = "nowhere"
        probs = routines.check(h)
        self.assertTrue(any("switch.not_listed" in p for p in probs))
        self.assertTrue(any("wake" in p for p in probs))
        self.assertEqual(routines.check({}), ["no 'routines' block in the house file"])


class Bedtime(unittest.TestCase):
    def test_live_bedtime_heat_first_kid_room_by_presence_fan_sent_and_a_wake_plan(self):
        h, f = house(), fake()
        res, lines, add = routines.bedtime(home(f, h), h, {}, "i'm going to bed, up at 7:30", NOW)
        self.assertTrue(all(r["ok"] for r in res), [r["text"] for r in res])
        self.assertEqual(f.states["climate.main_house"]["attributes"]["temperature"], 64)
        fr = [c for c in f.calls if c[2]["entity_id"] == "climate.family_room"]
        self.assertEqual([c[1] for c in fr], ["set_hvac_mode", "set_temperature"])      # off unit: heat first
        self.assertEqual(f.states["climate.kids_room"]["state"], "off")                  # the kid is away
        self.assertIn("cannot report back", res[3]["text"])
        p = add["plans"][0]
        self.assertEqual((p["kind"], p["target_f"], p["judge"]), ("wake", 70.0, "sensor.living_room_temperature"))
        self.assertEqual(datetime.fromisoformat(p["deadline"]), datetime(2026, 11, 21, 12, 30, tzinfo=timezone.utc))
        self.assertEqual({w["kind"] for w in add["watches"]}, {"lights_when_free", "overnight"})
        self.assertTrue(any(l.startswith("Wake: heating starts") for l in lines))

    def test_a_kid_at_home_keeps_their_unit_as_they_left_it(self):
        h, f = house(), fake(**{"person.kid": {"state": "home", "attributes": {}}})
        res, _, _ = routines.bedtime(home(f, h), h, {}, "going to bed", NOW)
        self.assertEqual(f.states["climate.kids_room"]["state"], "heat")
        self.assertIn("left as it is", res[2]["text"])

    def test_shadow_mode_writes_nothing_and_says_would(self):
        h, f = house("shadow"), fake()
        res, _, add = routines.bedtime(home(f, h), h, {}, "going to bed", NOW)
        self.assertEqual(f.calls, [])
        self.assertTrue(all(r["would"] or "left as it is" in r["text"] for r in res))
        self.assertEqual(len(add["plans"]), 1)

    def test_outside_heating_season_no_heat_steps_and_no_wake_ramp(self):
        h, f = house(), fake(**{"weather.home": {"state": "sunny", "attributes": {"temperature": 72}}})
        res, lines, add = routines.bedtime(home(f, h), h, {}, "going to bed", NOW)
        self.assertEqual(add["plans"], [])
        self.assertFalse(any(c[2]["entity_id"] == "climate.main_house" for c in f.calls))
        self.assertTrue(any("Heating rules skipped" in l for l in lines))


class Watcher(unittest.TestCase):
    def setUp(self):
        self.h, self.f = house(), fake()
        _, _, add = routines.bedtime(home(self.f, self.h), self.h, {}, "going to bed, up at 7:30", NOW)
        self.st = {"plans": add["plans"], "watches": add["watches"], "last": {}}
        self.f.calls.clear()

    def tick(self, t, **states):
        self.f.states.update(states)
        self.st, msgs, recs = routines.tick(home(self.f, self.h), self.h, {}, self.st, t)
        return msgs, recs

    def test_the_wake_ramp_starts_in_time_for_the_living_room_and_finishes_with_the_family_room(self):
        # 64 -> 70 at 1.1 F/h + 20 min lag + 15 min margin = about 6 h: start near 1:55 am New York for 7:30
        _, recs = self.tick(NOW + timedelta(hours=3))                                      # 1 am: too early
        self.assertFalse(any(r["kind"] == "plan_start" for r in recs))
        _, recs = self.tick(NOW + timedelta(hours=4))                                      # 2 am: started
        start = [r for r in recs if r["kind"] == "plan_start"]
        self.assertEqual(len(start), 1)
        self.assertEqual(self.f.states["climate.main_house"]["attributes"]["temperature"], 70)
        _, recs = self.tick(NOW + timedelta(hours=9, minutes=31), **{"sensor.living_room_temperature": sensor(69.8)})
        end = [r for r in recs if r["kind"] == "plan_end"][0]
        self.assertEqual(end["judge_f"], 69.8)
        self.assertEqual(self.f.states["climate.family_room"]["attributes"]["temperature"], 66)
        self.assertEqual(self.f.states["fan.master"]["attributes"]["percentage"], 33)      # the fan is left alone

    def test_downstairs_lights_wait_until_the_room_is_free_for_two_checks(self):
        self.tick(NOW + timedelta(minutes=5))
        self.assertEqual(self.f.states["light.family_room_all"]["state"], "on")             # TV playing
        free = {"media_player.family_room_tv": {"state": "off", "attributes": {}},
                "binary_sensor.family_room_presence": {"state": "off", "attributes": {}}}
        self.tick(NOW + timedelta(minutes=10), **free)
        self.assertEqual(self.f.states["light.family_room_all"]["state"], "on")             # one check is not enough
        _, recs = self.tick(NOW + timedelta(minutes=15))
        self.assertEqual(self.f.states["light.family_room_all"]["state"], "off")
        self.assertTrue(any(r["kind"] == "lights_off" for r in recs))

    def test_the_fireplace_fan_coming_on_drops_the_family_room_once(self):
        self.tick(NOW + timedelta(minutes=5))
        self.f.calls.clear()
        self.tick(NOW + timedelta(minutes=10), **{"sensor.fireplace_power": sensor(42)})
        self.assertEqual(self.f.states["climate.family_room"]["attributes"]["temperature"], 64)
        n = len(self.f.calls)
        self.tick(NOW + timedelta(minutes=15))                                              # still on: no repeat
        self.assertEqual(len([c for c in self.f.calls[n:] if c[2]["entity_id"] == "climate.family_room"]), 0)

    def test_arrival_is_a_change_while_the_house_is_closed_never_a_steady_state(self):
        self.tick(NOW)                                                                       # learns: locked, home
        msgs, recs = self.tick(NOW + timedelta(minutes=5), **{"lock.front_door": {"state": "unlocked", "attributes": {}}})
        self.assertTrue(any(r["kind"] == "arrival" for r in recs))
        self.assertEqual(self.f.states["input_boolean.house_closed"]["state"], "off")
        self.assertEqual(self.f.states["switch.fr_outlet"]["state"], "on")
        self.assertIn("front door was unlocked", msgs[0])
        _, recs = self.tick(NOW + timedelta(minutes=10))
        self.assertFalse(any(r["kind"] == "arrival" for r in recs))
        self.f.states["input_boolean.house_closed"] = {"state": "off", "attributes": {}}    # open house: not arrival
        self.tick(NOW + timedelta(minutes=15), **{"lock.front_door": {"state": "locked", "attributes": {}}})
        _, recs = self.tick(NOW + timedelta(minutes=20), **{"lock.front_door": {"state": "unlocked", "attributes": {}}})
        self.assertFalse(any(r["kind"] == "arrival" for r in recs))

    def test_comfort_humidifier_band_and_fan_periods_only_while_heating(self):
        heat = {"climate.family_room": climate("heat", 66.0, 65.0, "heating")}
        _, recs = self.tick(NOW, **heat)
        self.assertEqual(self.f.states["switch.fr_humidifier"]["state"], "on")              # 30% < 35%
        self.assertEqual(self.f.states["switch.fr_fan"]["state"], "on")
        self.tick(NOW + timedelta(minutes=10), **{"sensor.fr_humidity": sensor(40)})
        self.assertEqual(self.f.states["switch.fr_humidifier"]["state"], "on")              # inside the band
        self.tick(NOW + timedelta(minutes=16), **{"sensor.fr_humidity": sensor(47)})
        self.assertEqual(self.f.states["switch.fr_humidifier"]["state"], "off")
        self.assertEqual(self.f.states["switch.fr_fan"]["state"], "off")                     # 15-minute period over
        self.tick(NOW + timedelta(minutes=61))
        self.assertEqual(self.f.states["switch.fr_fan"]["state"], "on")                      # next period
        self.tick(NOW + timedelta(minutes=66), **{"climate.family_room": climate("off", None, 64.0, "off")})
        self.assertEqual(self.f.states["switch.fr_fan"]["state"], "off")                     # heating stopped

    def test_shadow_watcher_records_but_sends_and_writes_nothing(self):
        h = house("shadow")
        f = fake()
        _, _, add = routines.bedtime(home(f, h), h, {}, "going to bed", NOW)
        st = {"plans": add["plans"], "watches": add["watches"], "last": {}}
        st, msgs, recs = routines.tick(home(f, h), h, {}, st, NOW + timedelta(hours=5))
        self.assertEqual((f.calls, msgs), ([], []))
        self.assertTrue(any(r["kind"] == "plan_start" and r["shadow"] for r in recs))


class ComingUpAndCloseUp(unittest.TestCase):
    def test_arrival_time_parsing_asks_when_am_or_pm_is_unclear(self):
        h = house()
        when, q = routines.arrival_time("coming up friday at 7 pm", h, NOW)
        self.assertEqual(when, datetime(2026, 11, 28, 0, 0, tzinfo=timezone.utc))          # Fri 7 pm New York
        self.assertEqual(routines.arrival_time("coming up friday at 7", h, NOW), (None, "Arriving at 7 am or 7 pm?"))
        self.assertIsNotNone(routines.arrival_time("coming up tonight at 11", h, NOW)[0])

    def test_coming_up_turns_the_deck_outlet_on_and_plans_68_at_night_70_by_day(self):
        h, f = house(), fake(**{"switch.deck_outlet": {"state": "off", "attributes": {}}})
        res, lines, add = routines.coming_up(home(f, h), h, {}, datetime(2026, 11, 28, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(f.states["switch.deck_outlet"]["state"], "on")
        self.assertEqual(add["plans"][0]["target_f"], 68)
        _, _, add = routines.coming_up(home(f, h), h, {}, datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(add["plans"][0]["target_f"], 70)

    def test_the_owners_word_closes_a_window_that_cannot_be_read(self):
        h = house()
        h["departure"] = {"locks": ["lock.front_door"], "alarm": "alarm_control_panel.house"}
        f = fake(**{"binary_sensor.kitchen_window": {"state": "unavailable", "attributes": {}}})
        st, text, _ = departure.prepare(home(f, h), h, 0.0)
        self.assertEqual(st, "stopped")
        trusted = departure.assured("close up the house, the kitchen window is closed", h)
        self.assertEqual(trusted, ["kitchen window"])
        st, text, pending = departure.prepare(home(f, h), h, 0.0, trusted)
        self.assertEqual(st, "needs_confirmation")
        self.assertIn("kitchen window on your word", text)
        st, _ = departure.confirm(home(f, h), h, pending, 10.0)
        self.assertEqual(st, "done")
        self.assertEqual(departure.assured("close up the house", h), [])

    def test_close_up_steps_cancel_plans_and_flag_the_house(self):
        h, f = house(), fake(**{"input_boolean.house_closed": {"state": "off", "attributes": {}}})
        st = {"plans": [routines.make_plan("wake", "main", "sensor.living_room_temperature", 70, NOW)]}
        res, lines = routines.close_up(home(f, h), h, st)
        self.assertTrue(all(r["ok"] for r in res))
        self.assertEqual(f.states["switch.deck_outlet"]["state"], "off")
        self.assertEqual(f.states["input_boolean.house_closed"]["state"], "on")
        self.assertEqual(st["plans"][0]["status"], "cancelled")


class Requests(unittest.TestCase):
    def test_routing(self):
        h = house()
        r = lambda t: router.route(t, h).intent   # noqa: E731
        self.assertEqual(r("I'm going to bed, up at 7:30"), "bedtime")
        self.assertEqual(r("we're coming up friday at 7 pm"), "coming_up")
        self.assertEqual(r("we're here"), "arrived")
        self.assertEqual(r("how did the night go?"), "night_report")
        self.assertEqual(r("what happens if I say I'm going to bed?"), "fallback")    # a hypothetical is not run
        self.assertEqual(r("close up the house, the kitchen window is closed"), "close_up")
        self.assertEqual(router.route("I'm going to bed", HOUSE).intent, "fallback")    # no routines configured

    def test_end_to_end_bedtime_request_records_and_the_night_report_reads_it(self):
        h, f = house(), fake()
        with tempfile.TemporaryDirectory() as t:
            out = fastpath.handle("I'm going to bed, up at 7:30", home(f, h), h, Path(t) / "p.jsonl", state_dir=Path(t))
            self.assertEqual(out["intent"], "bedtime")
            self.assertTrue(out["reply"].startswith("Done:"), out["reply"])
            st = routines.load(Path(t) / "routines.json")
            self.assertEqual(len(st["plans"]), 1)
            rep = fastpath.handle("how did the night go", home(f, h), h, Path(t) / "p.jsonl", state_dir=Path(t))
            self.assertIn("Bedtime", rep["reply"])
            q = fastpath.handle("coming up friday at 7", home(f, h), h, Path(t) / "p.jsonl", state_dir=Path(t))
            self.assertEqual((q["outcome"], q["reply"]), ("needs_input", "Arriving at 7 am or 7 pm?"))


if __name__ == "__main__":
    unittest.main()
