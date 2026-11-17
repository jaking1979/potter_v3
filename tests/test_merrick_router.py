import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick import router  # noqa: E402
from merrick.testing import HOUSE  # noqa: E402


def go(text, decide=None, house=HOUSE):
    return router.route(text, house, decide)


class Table(unittest.TestCase):
    def check(self, cases):
        for text, intent in cases.items():
            with self.subTest(text=text):
                r = go(text)
                self.assertEqual(r.intent, intent)
                self.assertEqual((r.path, r.source), ("fast", "table"))

    def test_common_requests_are_recognised_without_a_model(self):
        self.check({
            "set the family room to 70": "set_temperature",
            "Set heat to 68": "set_temperature",
            "please make it 66 in the den": "set_temperature",
            "can you set the thermostat at 68?": "set_temperature",
            "turn off the heat": "turn_off",
            "could you shut the family room off?": "turn_off",
            "put the house in away mode": "hold_away",
            "what's the temperature in the family room": "check_temperature",
            "how warm is it downstairs?": "check_temperature",
            "is the heat on": "check_temperature",
            "are any windows open": "check_perimeter",
            "is the front door locked?": "check_perimeter",
            "is the house ok": "house_status",
            "status": "house_status",
            "how long to heat the house to 68": "heating_estimate",
            "close up the house": "close_up",
            "lock up the house": "close_up",       # still only checks and asks; locking needs "confirm departure"
            "lock up": "close_up",
            "lock the front door": "refuse_protected",
            "unlock the front door": "refuse_protected",
        })

    def test_zone_and_temperature_slots(self):
        r = go("set the family room to 70")
        self.assertEqual((r.slots["zone"], r.slots["zone_named"], r.slots["temperature"]), ("family_room", True, 70.0))
        r = go("set the thermostat to 68")
        self.assertEqual((r.slots["zone"], r.slots["zone_named"]), ("main", False))     # default zone, and it says so
        self.assertEqual(go("heat the den from 62 to 68").slots["temperature"], 68.0)
        self.assertEqual(go("heat the family room to 70").slots["zone"], "family_room")  # "heat" does not win

    def test_out_of_range_numbers_are_passed_through_for_the_clamp_to_report(self):
        # v2 silently used 68 when the number was outside 50-74. v3 keeps the owner's number; the HA layer clamps.
        self.assertEqual(go("turn the heat up to 80 degrees").slots["temperature"], 80.0)
        self.assertEqual(go("make it 45 in the den").slots["temperature"], 45.0)

    def test_a_named_sensor_can_be_read_but_not_controlled(self):
        r = go("what's the temperature in the basement")
        self.assertEqual((r.intent, r.slots), ("check_temperature", {"sensor": "basement"}))
        self.assertEqual(go("set basement to 60").reason, "unknown_zone")

    def test_protected_devices_are_refused_on_the_fast_path_but_questions_are_reads(self):
        for text in ("lock the front door", "unlock the door", "arm the alarm", "disarm the alarm",
                     "open the water valve", "shut off the main valve", "open the garage door", "close the windows"):
            with self.subTest(text=text):
                self.assertEqual(go(text).intent, "refuse_protected")
        self.assertEqual(go("is the alarm armed?").intent, "check_perimeter")


class Fallback(unittest.TestCase):
    def test_each_kind_of_miss_carries_a_countable_reason(self):
        cases = {
            "why is it cold downstairs": "needs_reasoning",
            "investigate the offline sensors": "needs_reasoning",
            "set heat to 68 at 5pm": "scheduled_or_conditional",
            "keep it at 68 for the weekend": "scheduled_or_conditional",
            "turn the heat down once it hits 68": "scheduled_or_conditional",
            "have the house warm when we arrive": "scheduled_or_conditional",
            "set heat to 68 and then turn off the den": "compound",
            "heat the family room and main house to 70": "multi_zone",
            "set the garage to 60": "unknown_zone",
            "set the city flat to 68": "other_location",
            "make the den warmer": "no_temperature",
            "create an automation to turn heat down": "automation_or_schedule",
            "what's for dinner": "unrecognised",
            "": "empty",
        }
        for text, reason in cases.items():
            with self.subTest(text=text):
                r = go(text)
                self.assertEqual((r.intent, r.path, r.reason), ("fallback", "fallback", reason))

    def test_away_hold_needs_a_configured_value(self):
        house = copy.deepcopy(HOUSE)
        del house["away_hold_f"]
        self.assertEqual(go("put the house in away mode", house=house).reason, "not_configured")

    def test_time_words_are_not_mistaken_for_setpoints_and_setpoints_not_for_times(self):
        self.assertEqual(go("set the heat at 68").intent, "set_temperature")
        self.assertEqual(go("set the heat to 68 at 7").reason, "scheduled_or_conditional")
        self.assertEqual(go("set heat to 68 in 2 hours").reason, "scheduled_or_conditional")


class Model(unittest.TestCase):
    def test_the_model_is_not_called_when_the_table_knows_the_answer(self):
        calls = []

        def decide(text):
            calls.append(text)
            return {"intent": "house_status", "confidence": 1.0}
        go("set heat to 68", decide)
        go("why is it cold", decide)                 # understood well enough to go to the fallback: not second-guessed
        go("set heat to 68 at 5pm", decide)
        self.assertEqual(calls, [])

    def test_a_confident_known_answer_is_used(self):
        r = go("everything alright up there", lambda t: {"intent": "house_status", "confidence": 0.9})
        self.assertEqual((r.intent, r.path, r.source), ("house_status", "fast", "model"))
        r = go("thermostat 68", lambda t: {"intent": "set_temperature", "zone": "family_room", "confidence": 0.95})
        self.assertEqual((r.intent, r.slots["zone"], r.slots["temperature"]), ("set_temperature", "family_room", 68.0))

    def test_unsure_unknown_broken_or_inventive_answers_fall_back(self):
        cases = [
            (lambda t: {"intent": "house_status", "confidence": 0.5}, "model_unsure"),
            (lambda t: {"intent": "unlock_everything", "confidence": 1.0}, "model_unsure"),
            (lambda t: {"intent": "house_status"}, "model_unsure"),
            (lambda t: {"intent": "house_status", "confidence": "high"}, "model_unsure"),
            (lambda t: None, "model_no_answer"),
            (lambda t: "set_temperature", "model_no_answer"),
            (lambda t: 1 / 0, "model_error"),
        ]
        for decide, reason in cases:
            r = go("everything alright up there", decide)
            self.assertEqual((r.path, r.source, r.reason), ("fallback", "model", reason))

    def test_the_model_cannot_supply_a_temperature_or_an_unknown_zone(self):
        r = go("cosy please", lambda t: {"intent": "set_temperature", "temperature": 74, "confidence": 1.0})
        self.assertEqual(r.reason, "no_temperature")
        r = go("thermostat 68", lambda t: {"intent": "set_temperature", "zone": "attic", "confidence": 1.0})
        self.assertEqual((r.slots["zone"], r.slots["zone_named"]), ("main", False))


if __name__ == "__main__":
    unittest.main()
