import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay"))
from merrick import intentions as it  # noqa: E402
from merrick.ha import SafeHome  # noqa: E402
from merrick.testing import HOUSE, FakeHome, states  # noqa: E402

T = datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)
SET68 = {"kind": "set_temperature", "entity_id": "climate.main_house", "temperature": 68}


def setup():
    fake = FakeHome(states())
    return fake, SafeHome(fake, HOUSE, sleep=lambda s: None), {"intentions": []}


def mins(n):
    return T + timedelta(minutes=n)


class OneShot(unittest.TestCase):
    def test_it_waits_runs_once_when_due_and_never_again(self):
        fake, home, store = setup()
        item = it.register(store, "heat to 68 at 5 pm", T.isoformat(), SET68, mins(-60))
        self.assertEqual(it.tick(store, home, mins(-1)), [])
        self.assertEqual(fake.calls, [])
        ev = it.tick(store, home, mins(0))
        self.assertEqual([(e["id"], e["status"]) for e in ev], [(item["id"], "completed")])
        self.assertEqual(fake.states["climate.main_house"]["attributes"]["temperature"], 68.0)
        fake.states["climate.main_house"]["attributes"]["temperature"] = 60.0        # someone changes it afterwards
        for later in (1, 5, 600):
            self.assertEqual(it.tick(store, home, mins(later)), [])                   # retired: no second run
        self.assertEqual(len(fake.calls), 1)

    def test_completed_only_when_read_back_matches_else_retried_then_failed(self):
        fake, home, store = setup()
        it.register(store, "heat to 68", T.isoformat(), SET68, mins(-60))
        fake.ignore_writes = True
        self.assertEqual(it.tick(store, home, mins(0)), [])                           # unconfirmed: stays pending
        self.assertEqual(it.tick(store, home, mins(5)), [])
        ev = it.tick(store, home, mins(10))
        self.assertEqual(ev[0]["status"], "failed")
        self.assertIn("NOT confirmed", ev[0]["detail"])
        self.assertIn("NOT done (failed)", it.render(ev))
        self.assertEqual(it.tick(store, home, mins(15)), [])

    def test_a_retry_that_succeeds_completes(self):
        fake, home, store = setup()
        it.register(store, "heat to 68", T.isoformat(), SET68, mins(-60))
        fake.fail_reads = True
        self.assertEqual(it.tick(store, home, mins(0)), [])
        fake.fail_reads = False
        self.assertEqual(it.tick(store, home, mins(5))[0]["status"], "completed")

    def test_a_missed_ordinary_intention_expires_instead_of_firing_late(self):
        fake, home, store = setup()
        it.register(store, "heat to 68 at 5 pm", T.isoformat(), SET68, mins(-60))
        ev = it.tick(store, home, mins(16))
        self.assertEqual(ev[0]["status"], "expired")
        self.assertIn("16 minutes", ev[0]["detail"])
        self.assertEqual(fake.calls, [])
        _, home2, store2 = setup()
        it.register(store2, "heat to 68 at 5 pm", T.isoformat(), SET68, mins(-60))
        self.assertEqual(it.tick(store2, home2, mins(15))[0]["status"], "completed")  # inside the window

    def test_protected_devices_fail_at_once_and_are_never_called(self):
        fake, home, store = setup()
        it.register(store, "lock up", T.isoformat(), {"kind": "switch", "entity_id": "lock.front_door", "on": True}, mins(-5))
        ev = it.tick(store, home, mins(0))
        self.assertEqual(ev[0]["status"], "failed")
        self.assertEqual(fake.calls, [])

    def test_setpoints_in_intentions_are_clamped_too(self):
        fake, home, store = setup()
        it.register(store, "roast", T.isoformat(), {**SET68, "temperature": 90}, mins(-5))
        it.tick(store, home, mins(0))
        self.assertEqual(fake.calls[0][2]["temperature"], 74.0)

    def test_bad_input_is_rejected_at_registration(self):
        _, _, store = setup()
        with self.assertRaises(ValueError):
            it.register(store, "x", T.isoformat(), {"kind": "create_automation", "entity_id": "a.b"}, T)
        with self.assertRaises(ValueError):
            it.register(store, "x", "2026-11-20T17:00:00", SET68, T)                  # no time zone
        with self.assertRaises(ValueError):
            it.register(store, "x", "five o'clock", SET68, T)
        self.assertEqual(store["intentions"], [])

    def test_cancel_only_affects_pending(self):
        fake, home, store = setup()
        a = it.register(store, "x", T.isoformat(), SET68, mins(-5))
        self.assertTrue(it.cancel(store, a["id"]))
        self.assertFalse(it.cancel(store, a["id"]))
        self.assertEqual(it.tick(store, home, mins(0)), [])
        self.assertEqual(fake.calls, [])


class PeriodBound(unittest.TestCase):
    def test_a_period_applies_at_the_start_and_retires_itself_at_the_end(self):
        fake, home, store = setup()
        end = mins(48 * 60)
        a, b = it.register_period(store, "68 for the weekend", T.isoformat(), end.isoformat(), SET68,
                                  {**SET68, "temperature": 62}, mins(-10))
        self.assertEqual((a["group"], b["must_run"]), (b["group"], True))
        self.assertEqual(it.tick(store, home, mins(0))[0]["status"], "completed")
        self.assertEqual(fake.states["climate.main_house"]["attributes"]["temperature"], 68.0)
        self.assertEqual(it.tick(store, home, mins(24 * 60)), [])
        ev = it.tick(store, home, end)
        self.assertEqual(ev[0]["status"], "completed")
        self.assertEqual(fake.states["climate.main_house"]["attributes"]["temperature"], 62.0)
        self.assertFalse(any(i["status"] == "pending" for i in store["intentions"]))  # nothing left armed

    def test_the_return_runs_even_when_the_wake_up_is_hours_late(self):
        fake, home, store = setup()
        it.register_period(store, "68 for the evening", mins(-300).isoformat(), T.isoformat(), SET68,
                           {**SET68, "temperature": 62}, mins(-400))
        ev = it.tick(store, home, mins(600))                                          # the job was down for ten hours
        self.assertEqual([e["status"] for e in ev], ["expired", "completed"])        # the start is skipped, the return is not
        self.assertEqual(fake.states["climate.main_house"]["attributes"]["temperature"], 62.0)

    def test_a_period_must_end_after_it_starts(self):
        _, _, store = setup()
        with self.assertRaises(ValueError):
            it.register_period(store, "x", T.isoformat(), T.isoformat(), SET68, SET68, T)


class Housekeeping(unittest.TestCase):
    def test_render_and_prune(self):
        fake, home, store = setup()
        it.register(store, "heat to 68 at 5 pm", T.isoformat(), SET68, mins(-5))
        it.register(store, "later", mins(90 * 24 * 60).isoformat(), SET68, mins(-5))
        text = it.render(it.tick(store, home, mins(0)))
        self.assertTrue(text.startswith("Done as asked: heat to 68 at 5 pm. Confirmed:"))
        self.assertEqual(it.render([]), "")
        it.prune(store, mins(40 * 24 * 60))
        self.assertEqual([i["description"] for i in store["intentions"]], ["later"])  # old retired ones go, pending stay


if __name__ == "__main__":
    unittest.main()
